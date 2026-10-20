"""
Tracking searches over time
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from sqlmodel import Session, col, select

from esmporium.db.results_to_database import (
    IngestedDocument,
    check_engine_is_configured,
    flushed_id,
    get_or_create,
    ingest_parsed_documents,
)
from esmporium.db.schema import (
    DatasetChange,
    DatasetChangeType,
    SearchRun,
    SearchRunLeaf,
    SearchRunVersion,
    StoredRequirement,
)
from esmporium.requirements import Requirement
from esmporium.search import DEFAULT_SELECTOR, search
from esmporium.search.result_normalisation import DEFAULT_NORMALISERS

if TYPE_CHECKING:
    import datetime
    from collections.abc import Iterable, Iterator, Mapping

    import httpx
    from sqlalchemy.engine import Engine

    from esmporium.query import QueryProtocol
    from esmporium.search import (
        LeafSearchOutcome,
        ProcessorFactory,
        SearchAPIFacade,
        SearchAPIFacadeSelector,
    )
    from esmporium.search.health import SearchAPICallObserver
    from esmporium.search.result_normalisation import NormaliseFunc
    from esmporium.search.result_parsing import ParsedDocument, ResultProcessor


@dataclass(frozen=True)
class _VersionState:
    """What one run was told about one dataset version, i.e. what a diff compares"""

    retracted: bool
    is_latest: bool
    data_nodes_json: str


class OnNameClash(str, Enum):
    """
    What to do when a requirement arrives under a name we have not got it stored under
    """

    RAISE = "raise"
    """
    Refuse, and say which name the requirement is already stored under

    The default. A stored name is how a person finds their search again, so losing one
    by accident is worse than being made to say which name you meant. It is also the
    only option which cannot lose information.
    """

    OVERWRITE_NAME = "overwrite_name"
    """
    Rename the stored requirement to the incoming name

    The old name is gone afterwards and nothing records it, so this is the option to
    reach for when you know the old name was wrong.
    """

    KEEP_OLD_NAME = "keep_old_name"
    """
    Carry on under the stored name, ignoring the incoming one

    The search runs and is recorded against the requirement as already named. Useful
    when the name is incidental to the caller -- a script which builds requirements
    afresh each time, say -- and the stored one is the name people know.
    """


class RequirementNameClashError(ValueError):
    """
    Raised when a stored requirement arrives under a different name

    See [OnNameClash][(m).] for the ways to not get this.
    """

    def __init__(self, stored_name: str, incoming_name: str) -> None:
        """
        Initialise the error

        Parameters
        ----------
        stored_name
            The name the requirement is stored under

        incoming_name
            The name it has just arrived under
        """
        self.stored_name = stored_name
        self.incoming_name = incoming_name
        msg = (
            f"This requirement is already stored as {stored_name!r}, "
            f"so storing it as {incoming_name!r} would give one search two names. "
            f"Use the name {stored_name!r}, "
            f"or pass on_name_clash=OnNameClash.OVERWRITE_NAME to rename it "
            f"to {incoming_name!r}, "
            "or OnNameClash.KEEP_OLD_NAME to carry on under the stored name."
        )
        super().__init__(msg)


class RequirementNameInUseError(ValueError):
    """
    Raised when a name is already taken by a different requirement

    One name means one search, so this is refused rather than offered as a choice. See
    [StoredRequirement.name][esmporium.db.schema.StoredRequirement.name].
    """

    def __init__(self, name: str, stored_hash: str, incoming_hash: str) -> None:
        """
        Initialise the error

        Parameters
        ----------
        name
            The name both requirements want

        stored_hash
            Fingerprint of the requirement already stored under `name`

        incoming_hash
            Fingerprint of the requirement which has just arrived
        """
        self.name = name
        self.stored_hash = stored_hash
        self.incoming_hash = incoming_hash
        msg = (
            f"The name {name!r} is already used by a different requirement "
            f"({stored_hash[:12]}..., where this one is {incoming_hash[:12]}...). "
            "One name means one search, so that a search can be re-run by name and "
            "its runs compared with each other. "
            "Give this requirement a name of its own, or rename the stored one "
            f"first if {name!r} is meant to be this search now."
        )
        super().__init__(msg)


class RequirementNotFoundError(ValueError):
    """Raised when no stored requirement matches the name or id asked for"""

    def __init__(self, name_or_id: str | int) -> None:
        """
        Initialise the error

        Parameters
        ----------
        name_or_id
            What was looked up
        """
        self.name_or_id = name_or_id
        looked_up = "id" if isinstance(name_or_id, int) else "name"
        msg = (
            f"No requirement is stored with {looked_up} {name_or_id!r}. "
            "Store one by searching for it with esmporium.db.search_and_track."
        )
        super().__init__(msg)


def resolve_name_clash(
    *,
    stored_name: str | None,
    incoming_name: str,
    on_clash: OnNameClash = OnNameClash.RAISE,
) -> str:
    """
    Work out which name a requirement should be stored under

    Deliberately a plain function of three values, with no database in sight, because
    this is the whole of the naming policy and it is worth being able to read and test
    on its own.

    Parameters
    ----------
    stored_name
        The name this requirement is already stored under, or `None` if we have not got
        it yet

    incoming_name
        The name it has arrived under now

    on_clash
        What to do if the two names differ. See [OnNameClash][(m).].

    Returns
    -------
    :
        The name to store. The same string as `stored_name` or as `incoming_name`; which
        of the two is the only thing this decides.

    Raises
    ------
    RequirementNameClashError
        The names differ and `on_clash` is [OnNameClash.RAISE][(m).OnNameClash.RAISE]

    Examples
    --------
    >>> resolve_name_clash(stored_name=None, incoming_name="gregory-method")
    'gregory-method'

    >>> resolve_name_clash(
    ...     stored_name="gregory-method",
    ...     incoming_name="ecs-method",
    ...     on_clash=OnNameClash.KEEP_OLD_NAME,
    ... )
    'gregory-method'

    >>> resolve_name_clash(
    ...     stored_name="gregory-method",
    ...     incoming_name="ecs-method",
    ...     on_clash=OnNameClash.OVERWRITE_NAME,
    ... )
    'ecs-method'
    """
    if stored_name is None or stored_name == incoming_name:
        return incoming_name

    if on_clash == OnNameClash.OVERWRITE_NAME:
        return incoming_name

    if on_clash == OnNameClash.KEEP_OLD_NAME:
        return stored_name

    raise RequirementNameClashError(stored_name, incoming_name)


def _get_by_hash(session: Session, requirement_hash: str) -> StoredRequirement | None:
    """Get the stored requirement with this fingerprint, if we have it."""
    return session.exec(
        select(StoredRequirement).where(
            StoredRequirement.requirement_hash == requirement_hash
        )
    ).one_or_none()


def _get_by_name(session: Session, name: str) -> StoredRequirement | None:
    """Get the stored requirement with this name, if we have it."""
    return session.exec(
        select(StoredRequirement).where(StoredRequirement.name == name)
    ).one_or_none()


def store_requirement(
    session: Session,
    requirement: Requirement,
    *,
    on_name_clash: OnNameClash = OnNameClash.RAISE,
) -> StoredRequirement:
    """
    Store a requirement, or find the one we have already stored

    The requirement is found by its fingerprint, not by its name, so one which has been
    renamed is recognised as the one we already hold rather than stored a second time.

    This does not commit; the caller controls the transaction boundary.

    Parameters
    ----------
    session
        The session to write into

    requirement
        The requirement to store

    on_name_clash
        What to do if this requirement is stored under a different name already. See
        [OnNameClash][(m).].

    Returns
    -------
    :
        The stored row, flushed, so its `id` is set

    Raises
    ------
    RequirementNameClashError
        The requirement is stored under another name and `on_name_clash` is
        [OnNameClash.RAISE][(m).OnNameClash.RAISE]

    RequirementNameInUseError
        A *different* requirement is already stored under this requirement's name

    UnconfiguredSQLiteEngineError
        `session` is bound to a SQLite engine which has not been configured with
        [esmporium.db.configure_sqlite_for_concurrency][]
    """
    check_engine_is_configured(session)

    requirement_hash = requirement.requirement_hash()
    stored = _get_by_hash(session, requirement_hash)

    name = resolve_name_clash(
        stored_name=None if stored is None else stored.name,
        incoming_name=requirement.name,
        on_clash=on_name_clash,
    )

    # Checked before inserting or renaming so that the error names both requirements,
    # which the database's own unique-constraint failure could not. The constraint is
    # still there, and is what makes this safe under a race.
    holder = _get_by_name(session, name)
    if holder is not None and holder.requirement_hash != requirement_hash:
        raise RequirementNameInUseError(name, holder.requirement_hash, requirement_hash)

    if stored is None:
        stored = StoredRequirement(
            requirement_hash=requirement_hash,
            name=name,
            identity_json=requirement.identity_json(),
        )
        session.add(stored)
    elif stored.name != name:
        stored.name = name
        session.add(stored)

    session.flush()

    return stored


def to_requirement(stored: StoredRequirement) -> Requirement:
    """
    Rebuild the requirement a stored row describes

    The row holds the question without its name (see
    [StoredRequirement.identity_json][esmporium.db.schema.StoredRequirement.identity_json]),
    so the name is put back from the column which owns it.

    Parameters
    ----------
    stored
        The stored row

    Returns
    -------
    :
        The requirement
    """
    return Requirement.model_validate(
        {**json.loads(stored.identity_json), "name": stored.name}
    )


def find_requirement(
    session: Session, name_or_id: str | int
) -> StoredRequirement | None:
    """
    Find a stored requirement by its name or its id

    Parameters
    ----------
    session
        The session to read from

    name_or_id
        The requirement's
        [name][esmporium.db.schema.StoredRequirement.name] if a `str`, or its
        [id][esmporium.db.schema.StoredRequirement.id] if an `int`. The two cannot be
        confused for each other, so one argument carries both.

    Returns
    -------
    :
        The stored row, or `None` if there is no such requirement
    """
    if isinstance(name_or_id, int):
        return session.get(StoredRequirement, name_or_id)

    return _get_by_name(session, name_or_id)


def load_requirement(session: Session, name_or_id: str | int) -> Requirement:
    """
    Load a stored requirement by its name or its id

    This is what makes a search repeatable from the name a person chose for it, which is
    all [rerun_search_esgf][(m).] needs to be given.

    Parameters
    ----------
    session
        The session to read from

    name_or_id
        See [find_requirement][(m).]

    Returns
    -------
    :
        The requirement, as it was stored

    Raises
    ------
    RequirementNotFoundError
        No requirement is stored with that name or id
    """
    stored = find_requirement(session, name_or_id)
    if stored is None:
        raise RequirementNotFoundError(name_or_id)

    return to_requirement(stored)


@dataclass(frozen=True)
class StoredRequirementInfo:
    """
    A stored requirement, read back out of the database

    The row itself is a [StoredRequirement][esmporium.db.schema.StoredRequirement],
    which is tied to the session it was read in. This is the plain, detached answer.
    """

    id: int
    """The requirement's id"""

    name: str
    """The name it is stored under"""

    requirement_hash: str
    """Its fingerprint"""

    requirement: Requirement
    """The requirement itself"""


def get_stored_requirements(engine: Engine) -> tuple[StoredRequirementInfo, ...]:
    """
    Get every requirement we are tracking

    Parameters
    ----------
    engine
        The database to read from

    Returns
    -------
    :
        One entry per stored requirement, oldest first
    """
    with Session(engine) as session:
        rows = session.exec(
            select(StoredRequirement).order_by(col(StoredRequirement.id))
        ).all()

        return tuple(
            StoredRequirementInfo(
                id=flushed_id(row),
                name=row.name,
                requirement_hash=row.requirement_hash,
                requirement=to_requirement(row),
            )
            for row in rows
        )


def _encode_data_nodes(data_nodes: Iterable[str]) -> str:
    """Encode a set of data node names, sorted so two runs can be compared as text."""
    return json.dumps(sorted(set(data_nodes)))


def _decode_data_nodes(encoded: str) -> tuple[str, ...]:
    """Read back what [_encode_data_nodes][(m).] wrote."""
    return tuple(json.loads(encoded))


def _upsert_run_version(  # noqa: PLR0913 - retracted, is_latest and the nodes
    session: Session,
    search_run_id: int,
    dataset_version_id: int,
    *,
    retracted: bool,
    is_latest: bool,
    data_nodes: Iterable[str],
) -> SearchRunVersion:
    """
    Record that this run saw this version, or fold another document's word into the row

    One run can be told about a version more than once, because a version can be
    described by several documents. Three things are recorded -- `retracted`,
    `is_latest` and the set of data nodes -- and they are not folded in the same way:
    the first two take the last document's word, the set of nodes takes the union of
    every document's. The difference is deliberate, and
    [SearchRunVersion][esmporium.db.schema.SearchRunVersion] says why.
    """
    row = get_or_create(
        session,
        lambda: session.exec(
            select(SearchRunVersion).where(
                SearchRunVersion.search_run_id == search_run_id,
                SearchRunVersion.dataset_version_id == dataset_version_id,
            )
        ).one_or_none(),
        lambda: SearchRunVersion(
            search_run_id=search_run_id,
            dataset_version_id=dataset_version_id,
            retracted=retracted,
            is_latest=is_latest,
            data_nodes_json=_encode_data_nodes(data_nodes),
        ),
    )

    # Last-write-wins: every document asserts both of these, so there is no "first" to
    # prefer. This matches how the version row itself is refreshed.
    row.retracted = retracted
    row.is_latest = is_latest
    # Unioned: a document reports only the nodes it knows about, so the last to arrive
    # is not the most complete and overwriting would throw nodes away.
    #
    # Reading before writing is safe because `BEGIN IMMEDIATE` takes SQLite's write
    # lock before the read, so parallel workers take turns -- see
    # [esmporium.db.configure_sqlite_for_concurrency][]. A backend with no single write
    # lock would need this union computed in SQL.
    row.data_nodes_json = _encode_data_nodes(
        (*_decode_data_nodes(row.data_nodes_json), *data_nodes)
    )
    session.flush()

    return row


def record_run_versions(
    session: Session, search_run_id: int, ingested: Iterable[IngestedDocument]
) -> None:
    """
    Record what a run saw, from the documents it has just written

    Parameters
    ----------
    session
        The session to write into. This does not commit.

    search_run_id
        The run which saw them

    ingested
        What [ingest_parsed_documents][esmporium.db.ingest_parsed_documents] returned
    """
    for document in ingested:
        data_nodes = tuple(node.data_node for node in document.parsed.nodes)
        for version in document.versions:
            _upsert_run_version(
                session,
                search_run_id,
                flushed_id(version),
                retracted=document.parsed.retracted,
                is_latest=document.parsed.is_latest,
                data_nodes=data_nodes,
            )


def build_tracking_result_processor_factory(
    engine: Engine,
    search_run_id: int,
    normalisers: Mapping[str, NormaliseFunc] = DEFAULT_NORMALISERS,
) -> ProcessorFactory:
    """
    Build the processor factory a tracked search hands to a search

    The same as
    [build_result_processor_factory][esmporium.db.build_result_processor_factory] -- a
    fresh session per sub-search, a commit per page -- except that each page also
    records what this run saw, in the same transaction as the writes it is about. So a
    page is still saved all or nothing, results and record together.

    Parameters
    ----------
    engine
        The database engine each sub-search's session is opened on

    search_run_id
        The run to record against. It has to exist already, which is why
        [search_and_track][(m).] opens the run before it searches.

    normalisers
        Passed through to
        [ingest_parsed_documents][esmporium.db.ingest_parsed_documents]

    Returns
    -------
    :
        A factory of the shape [esmporium.search.ProcessorFactory][]
    """

    @contextmanager
    def factory() -> Iterator[ResultProcessor]:
        with Session(engine) as session:

            def processor(
                facade: SearchAPIFacade, parsed_documents: tuple[ParsedDocument, ...]
            ) -> None:
                # `facade` is part of the ResultProcessor callback contract.
                ingested = ingest_parsed_documents(
                    session, parsed_documents, normalisers
                )
                record_run_versions(session, search_run_id, ingested)
                session.commit()

            yield processor

    return factory


def _leaf_row(search_run_id: int, outcome: LeafSearchOutcome) -> SearchRunLeaf:
    """Turn one sub-search's outcome into the row which records it."""
    answering = tuple(outcome.outcome.parsed_docs)
    # The facades are keyed in the order they were tried, so the first to answer is the
    # one whose results we kept. With the default `stop_at_first_result=True` it is also
    # the only one.
    kept = answering[0] if answering else None

    return SearchRunLeaf(
        search_run_id=search_run_id,
        roles_json=json.dumps(list(outcome.roles)),
        project=outcome.project,
        answered=outcome.answered,
        host=None if kept is None else kept[0],
        n_matches=None if kept is None else outcome.outcome.n_matches.get(kept),
        n_documents=0 if kept is None else len(outcome.outcome.parsed_docs[kept]),
        failure=(
            None
            if not outcome.outcome.failures
            else "; ".join(
                f"{key[0]}: {error}" for key, error in outcome.outcome.failures.items()
            )
        ),
    )


def run_is_complete(leaves: Iterable[SearchRunLeaf]) -> bool:
    """
    Work out whether a run answered the whole requirement

    Parameters
    ----------
    leaves
        The run's sub-searches

    Returns
    -------
    :
        Whether this run may be used as a baseline for comparison
    """
    return all(
        leaf.answered and (leaf.n_matches is None or leaf.n_documents >= leaf.n_matches)
        for leaf in leaves
    )


def _run_state(session: Session, search_run_id: int) -> dict[int, _VersionState]:
    """Get what one run was told, keyed by dataset version."""
    rows = session.exec(
        select(SearchRunVersion).where(SearchRunVersion.search_run_id == search_run_id)
    ).all()

    return {
        row.dataset_version_id: _VersionState(
            retracted=row.retracted,
            is_latest=row.is_latest,
            data_nodes_json=row.data_nodes_json,
        )
        for row in rows
    }


def _latest_complete_run(
    session: Session, stored_requirement_id: int, *, before_run_id: int
) -> SearchRun | None:
    """
    Get the run to compare against: the newest complete run of this requirement
    """
    return session.exec(
        select(SearchRun)
        .where(
            SearchRun.stored_requirement_id == stored_requirement_id,
            SearchRun.complete == True,  # noqa: E712 - SQL comparison, not a bool test
            col(SearchRun.id) < before_run_id,
        )
        .order_by(col(SearchRun.id).desc())
    ).first()


def _changed_values(
    before: _VersionState, after: _VersionState
) -> dict[str, dict[str, object]]:
    """
    Say what moved between two runs' answers about one version
    """
    changed: dict[str, dict[str, object]] = {}

    if before.retracted != after.retracted:
        changed["retracted"] = {"old": before.retracted, "new": after.retracted}

    if before.is_latest != after.is_latest:
        changed["is_latest"] = {"old": before.is_latest, "new": after.is_latest}

    if before.data_nodes_json != after.data_nodes_json:
        changed["data_nodes"] = {
            "old": list(_decode_data_nodes(before.data_nodes_json)),
            "new": list(_decode_data_nodes(after.data_nodes_json)),
        }

    return changed


@dataclass(frozen=True)
class RunChanges:
    """
    What a run changed, relative to the run it was compared against

    The ids are
    [DatasetVersion.id][esmporium.db.schema.DatasetVersion.id]s, sorted, so that two
    readings of the same run give the same answer.
    """

    baseline_run_id: int | None
    """
    The run this was compared against, or `None` if there was no complete earlier run

    `None` means everything found is reported as `added`, because there was nothing to
    compare with -- so "added" reads as "first seen in this series".
    """

    added: tuple[int, ...]
    """Versions this run returned and the baseline did not"""

    removed: tuple[int, ...]
    """Versions the baseline returned and this run did not"""

    modified: tuple[int, ...]
    """
    Versions both runs returned, but were told different things about
    """

    @property
    def any_changes(self) -> bool:
        """Whether anything changed at all"""
        return bool(self.added or self.removed or self.modified)


def _compare(
    session: Session, search_run: SearchRun, stored_requirement_id: int
) -> tuple[RunChanges, dict[int, dict[str, dict[str, object]]]]:
    """
    Compare a run with the last complete one, and say what moved in each version
    """
    run_id = flushed_id(search_run)
    baseline = _latest_complete_run(
        session, stored_requirement_id, before_run_id=run_id
    )

    current = _run_state(session, run_id)
    previous = {} if baseline is None else _run_state(session, flushed_id(baseline))

    moved = {
        version_id: changed
        for version_id in set(current) & set(previous)
        if (changed := _changed_values(previous[version_id], current[version_id]))
    }

    changes = RunChanges(
        baseline_run_id=None if baseline is None else flushed_id(baseline),
        added=tuple(sorted(set(current) - set(previous))),
        removed=tuple(sorted(set(previous) - set(current))),
        modified=tuple(sorted(moved)),
    )

    return changes, moved


def compute_changes(
    session: Session, search_run: SearchRun, *, stored_requirement_id: int
) -> RunChanges:
    """
    Compare a run with the last complete run of the same requirement

    Parameters
    ----------
    session
        The session to read from

    search_run
        The run to compare. It must already have its version rows written.

    stored_requirement_id
        The requirement the run belongs to

    Returns
    -------
    :
        What changed
    """
    changes, _ = _compare(session, search_run, stored_requirement_id)

    return changes


def record_changes(
    session: Session, search_run: SearchRun, *, stored_requirement_id: int
) -> RunChanges:
    """
    Compare a run with the last complete run of the same requirement, and write the log

    Parameters
    ----------
    session
        The session to write into. This does not commit.

    search_run
        The run to compare

    stored_requirement_id
        The requirement the run belongs to

    Returns
    -------
    :
        What changed. Empty, with no baseline, for an incomplete run.
    """
    if not search_run.complete:
        return RunChanges(baseline_run_id=None, added=(), removed=(), modified=())

    changes, moved = _compare(session, search_run, stored_requirement_id)
    run_id = flushed_id(search_run)

    for version_id in changes.added:
        session.add(
            DatasetChange(
                search_run_id=run_id,
                baseline_run_id=changes.baseline_run_id,
                dataset_version_id=version_id,
                change_type=DatasetChangeType.ADDED,
            )
        )

    for version_id in changes.removed:
        session.add(
            DatasetChange(
                search_run_id=run_id,
                baseline_run_id=changes.baseline_run_id,
                dataset_version_id=version_id,
                change_type=DatasetChangeType.REMOVED,
            )
        )

    for version_id in changes.modified:
        session.add(
            DatasetChange(
                search_run_id=run_id,
                baseline_run_id=changes.baseline_run_id,
                dataset_version_id=version_id,
                change_type=DatasetChangeType.MODIFIED,
                detail_json=json.dumps(moved[version_id], sort_keys=True),
            )
        )

    session.flush()

    return changes


@dataclass(frozen=True)
class TrackedSearchResult:
    """
    What came of a tracked search: the search itself, and what it changed
    """

    stored_requirement_id: int
    """The requirement which was searched for"""

    search_run_id: int
    """This run of it"""

    requirement: Requirement
    """
    The requirement as it was run

    Not necessarily the one which was passed in: under
    [OnNameClash.KEEP_OLD_NAME][(m).OnNameClash.KEEP_OLD_NAME] the name is the stored
    one.
    """

    complete: bool
    """
    Whether every sub-search answered in full

    `False` means the run is not a usable baseline and recorded no changes. See
    [run_is_complete][(m).].
    """

    changes: RunChanges
    """What changed since the last complete run"""

    outcomes: tuple[LeafSearchOutcome, ...]
    """
    Exactly what [esmporium.search.search][] returned

    Passed straight back, so tracking a search costs a caller nothing.
    """


def search_and_track(  # noqa: PLR0913 - one argument per thing a search has to be told
    engine: Engine,
    requirement: Requirement,
    selector: SearchAPIFacadeSelector = DEFAULT_SELECTOR,
    *,
    on_name_clash: OnNameClash = OnNameClash.RAISE,
    stop_at_first_result: bool = True,
    limit: int = 10_000,
    max_results: int | None = None,
    warn_on_pagination: bool = True,
    client: httpx.Client | None = None,
    api_call_observer: SearchAPICallObserver | None = None,
    project_query_map: Mapping[str, type[QueryProtocol]] | None = None,
    max_workers: int | None = None,
    normalisers: Mapping[str, NormaliseFunc] = DEFAULT_NORMALISERS,
) -> TrackedSearchResult:
    """
    Search for a requirement, saving the results and recording the search itself

    The tracked counterpart to [esmporium.search.search][]. It does what that does,
    and additionally remembers having done it: the requirement is stored, the run is
    recorded, what the run saw is written down, and the difference from the last
    complete run is computed and logged.

    Parameters
    ----------
    engine
        The database to store and record in

    requirement
        The requirement to search for

    selector
        Passed through to [esmporium.search.search][]

    on_name_clash
        What to do if this requirement is already stored under a different name. See
        [OnNameClash][(m).].

    stop_at_first_result
        Passed through to [esmporium.search.search][]

    limit
        Passed through to [esmporium.search.search][]

    max_results
        Passed through to [esmporium.search.search][]. Note that cutting a search short
        this way makes the run incomplete, because a truncated answer cannot be told
        apart from datasets having gone away.

    warn_on_pagination
        Passed through to [esmporium.search.search][]

    client
        Passed through to [esmporium.search.search][]

    api_call_observer
        Passed through to [esmporium.search.search][]

    project_query_map
        Passed through to [esmporium.search.search][]

    max_workers
        Passed through to [esmporium.search.search][]

    normalisers
        Passed through to
        [ingest_parsed_documents][esmporium.db.ingest_parsed_documents]

    Returns
    -------
    :
        The run, and what it changed

    Raises
    ------
    RequirementNameClashError
        The requirement is stored under another name and `on_name_clash` is
        [OnNameClash.RAISE][(m).OnNameClash.RAISE]

    RequirementNameInUseError
        A different requirement is already stored under this one's name

    UnconfiguredSQLiteEngineError
        `engine` is a SQLite engine which has not been configured with
        [esmporium.db.configure_sqlite_for_concurrency][]. Raised before anything is
        searched for, rather than after the network has been used.
    """
    with Session(engine) as session:
        stored = store_requirement(session, requirement, on_name_clash=on_name_clash)
        stored_requirement_id = flushed_id(stored)
        # The name may be the stored one rather than the incoming one, so this is the
        # requirement as it is actually tracked.
        tracked = to_requirement(stored)
        session.commit()

    with Session(engine) as session:
        search_run = SearchRun(stored_requirement_id=stored_requirement_id)
        session.add(search_run)
        session.commit()
        search_run_id = flushed_id(search_run)

    try:
        outcomes = search(
            tracked,
            selector,
            stop_at_first_result=stop_at_first_result,
            limit=limit,
            max_results=max_results,
            warn_on_pagination=warn_on_pagination,
            client=client,
            api_call_observer=api_call_observer,
            processor_factory=build_tracking_result_processor_factory(
                engine, search_run_id, normalisers
            ),
            project_query_map=project_query_map,
            max_workers=max_workers,
        )
    except Exception:
        # The run stays incomplete and unfinished, which is the truth about it, and
        # keeps it from being used as a baseline.
        _close_run(engine, search_run_id, complete=False)
        raise

    with Session(engine) as session:
        run = session.get(SearchRun, search_run_id)
        if run is None:  # pragma: no cover - we have just written it
            msg = f"search run {search_run_id} vanished mid-search"
            raise RuntimeError(msg)

        leaves = [_leaf_row(search_run_id, outcome) for outcome in outcomes]
        for leaf in leaves:
            session.add(leaf)

        run.finish(complete=run_is_complete(leaves))
        session.add(run)
        session.flush()

        changes = record_changes(
            session, run, stored_requirement_id=stored_requirement_id
        )
        complete = run.complete
        session.commit()

    return TrackedSearchResult(
        stored_requirement_id=stored_requirement_id,
        search_run_id=search_run_id,
        requirement=tracked,
        complete=complete,
        changes=changes,
        outcomes=outcomes,
    )


def _close_run(engine: Engine, search_run_id: int, *, complete: bool) -> None:
    """Stamp a run as finished, without comparing it with anything."""
    with Session(engine) as session:
        run = session.get(SearchRun, search_run_id)
        if run is None:  # pragma: no cover - we have just written it
            return

        run.finish(complete=complete)
        session.add(run)
        session.commit()


def rerun_search_esgf(  # noqa: PLR0913 - one argument per thing a search has to be told
    engine: Engine,
    name_or_id: str | int,
    selector: SearchAPIFacadeSelector = DEFAULT_SELECTOR,
    *,
    stop_at_first_result: bool = True,
    limit: int = 10_000,
    max_results: int | None = None,
    warn_on_pagination: bool = True,
    client: httpx.Client | None = None,
    api_call_observer: SearchAPICallObserver | None = None,
    project_query_map: Mapping[str, type[QueryProtocol]] | None = None,
    max_workers: int | None = None,
    normalisers: Mapping[str, NormaliseFunc] = DEFAULT_NORMALISERS,
) -> TrackedSearchResult:
    """
    Run a stored search again, given only its name or its id

    This is [load_requirement][(m).] followed by [search_and_track][(m).], and that is
    all it is: re-running is the same path as searching, so there is no second
    implementation of tracking to keep in step with the first. Calling
    `search_and_track` again with the same requirement does exactly the same thing;
    this exists because a person watching a search knows its name, not the Python which
    built it.

    There is no `on_name_clash` to pass. The requirement is rebuilt from the row it was
    found in, so its name is the stored name and there is nothing to clash with.

    Parameters
    ----------
    engine
        The database to read the requirement from, and to record the run in

    name_or_id
        The requirement's name, or its id. See [find_requirement][(m).].

    selector
        Passed through to [search_and_track][(m).]

    stop_at_first_result
        Passed through to [search_and_track][(m).]

    limit
        Passed through to [search_and_track][(m).]

    max_results
        Passed through to [search_and_track][(m).]

    warn_on_pagination
        Passed through to [search_and_track][(m).]

    client
        Passed through to [search_and_track][(m).]

    api_call_observer
        Passed through to [search_and_track][(m).]

    project_query_map
        Passed through to [search_and_track][(m).]

    max_workers
        Passed through to [search_and_track][(m).]

    normalisers
        Passed through to [search_and_track][(m).]

    Returns
    -------
    :
        The run, and what it changed since the last complete one

    Raises
    ------
    RequirementNotFoundError
        No requirement is stored with that name or id
    """
    with Session(engine) as session:
        requirement = load_requirement(session, name_or_id)

    return search_and_track(
        engine,
        requirement,
        selector,
        stop_at_first_result=stop_at_first_result,
        limit=limit,
        max_results=max_results,
        warn_on_pagination=warn_on_pagination,
        client=client,
        api_call_observer=api_call_observer,
        project_query_map=project_query_map,
        max_workers=max_workers,
        normalisers=normalisers,
    )


@dataclass(frozen=True)
class SearchRunInfo:
    """One run of a tracked search, read back out of the database"""

    id: int
    """The run's id"""

    stored_requirement_id: int
    """The requirement it was a run of"""

    started_at: datetime.datetime
    """When it began (UTC)"""

    finished_at: datetime.datetime | None
    """When it finished (UTC), or `None` if it never did"""

    complete: bool
    """Whether every sub-search answered in full"""


def get_search_runs(engine: Engine, name_or_id: str | int) -> tuple[SearchRunInfo, ...]:
    """
    Get every run of one tracked search, newest first

    Parameters
    ----------
    engine
        The database to read from

    name_or_id
        Which requirement's runs to get. See [find_requirement][(m).].

    Returns
    -------
    :
        The runs, newest first

    Raises
    ------
    RequirementNotFoundError
        No requirement is stored with that name or id
    """
    with Session(engine) as session:
        stored = find_requirement(session, name_or_id)
        if stored is None:
            raise RequirementNotFoundError(name_or_id)

        rows = session.exec(
            select(SearchRun)
            .where(SearchRun.stored_requirement_id == flushed_id(stored))
            .order_by(col(SearchRun.id).desc())
        ).all()

        return tuple(
            SearchRunInfo(
                id=flushed_id(row),
                stored_requirement_id=row.stored_requirement_id,
                started_at=row.started_at,
                finished_at=row.finished_at,
                complete=row.complete,
            )
            for row in rows
        )


@dataclass(frozen=True)
class DatasetChangeInfo:
    """One recorded change, read back out of the database"""

    dataset_version_id: int
    """The version which changed"""

    change_type: DatasetChangeType
    """What happened to it"""

    baseline_run_id: int | None
    """The run it was compared against, or `None` on a requirement's first run"""

    detail: Mapping[str, Mapping[str, object]] | None
    """
    What moved, for a
    [modified][esmporium.db.schema.DatasetChangeType.MODIFIED] change

    Keyed by whichever of `retracted`, `is_latest` and `data_nodes` moved, each with
    its old and new value, e.g. `{"retracted": {"old": False, "new": True}}`.

    `None` for an addition or a removal, where the change is the presence of the
    version rather than anything about it.
    """


def get_changes(engine: Engine, search_run_id: int) -> tuple[DatasetChangeInfo, ...]:
    """
    Get what one run changed

    Parameters
    ----------
    engine
        The database to read from

    search_run_id
        The run to read the log of

    Returns
    -------
    :
        The changes, in the order they were recorded: additions, then removals, then
        modifications
    """
    with Session(engine) as session:
        rows = session.exec(
            select(DatasetChange)
            .where(DatasetChange.search_run_id == search_run_id)
            .order_by(col(DatasetChange.id))
        ).all()

        return tuple(
            DatasetChangeInfo(
                dataset_version_id=row.dataset_version_id,
                change_type=DatasetChangeType(row.change_type),
                baseline_run_id=row.baseline_run_id,
                detail=(
                    None if row.detail_json is None else json.loads(row.detail_json)
                ),
            )
            for row in rows
        )
