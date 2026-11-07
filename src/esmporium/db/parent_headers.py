"""
Getting a dataset's parent facts out of its files' headers
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from sqlmodel import Session, col, select

from esmporium.db.downloads_to_database import routes_for_file
from esmporium.db.file_access_to_database import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_LIMIT,
    build_result_processor_with_file_access,
)
from esmporium.db.header_health import write_header_read_calls
from esmporium.db.parents_to_database import ParentSimulation
from esmporium.db.results_to_database import flushed_id, save_parent_info
from esmporium.db.schema import (
    Dataset,
    DatasetVersion,
    DatasetVersionParent,
    File,
    ParentInfoSource,
)
from esmporium.downloads import DEFAULT_DATA_NODE_SELECTOR, prepare_routes
from esmporium.headers import (
    DEFAULT_MAX_WANT_BYTES,
    DEFAULT_PER_DATA_NODE,
    DEFAULT_WANT_BYTES,
    HeaderRequest,
    fan_out,
    read_headers,
)
from esmporium.search import (
    ParentInfo,
    build_parent_dataset_request,
    search_single_project,
)
from esmporium.search.search_api_facade.selectors import DEFAULT_SEARCH_API_SELECTOR

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    import httpx

    from esmporium.db.parents_to_database import (
        ParentFactsProvider,
        ParentSearcher,
        ParentSearchPlanItem,
    )
    from esmporium.downloads import DataNodeSelector, Route
    from esmporium.fixes import ParentFixes
    from esmporium.headers import (
        FileHeader,
        HeaderReadCall,
        HeaderReadCallObserver,
    )
    from esmporium.search import SearchAPICallObserver
    from esmporium.search.search_api_facade.selectors import SearchAPIFacadeSelector

logger = logging.getLogger(__name__)

NOT_SAID = frozenset({"", "n/a", "na", "none", "null", "no parent", "not applicable"})
"""
Attribute values which mean "this was not said", rather than being a value

Matched case-insensitively and after stripping whitespace.

Needed because the two projects whose headers we read both use a sentinel, and they use
different ones -- so neither a missing attribute nor a `None` is enough on its own.

- **CMIP5 writes `N/A`.** And it does so in two quite different situations, which is
  why this is a per-field rule rather than a whole-record one. A CMIP5 `piControl` says
  `parent_experiment_id = "N/A"` and carries nothing else, which is how it says it is a
  root. But `CMCC-CM`'s `rcp85` says `parent_experiment_rip = "N/A"` while naming
  `historical` as its parent experiment -- there, one field is unknown and the record
  still declares a parent.
- **CMIP6 writes `no parent`.** A literal `parent_experiment_id` of `"no parent"` is a
  CMIP6 convention, and does not occur in CMIP7, which announces a root by carrying
  none of the keys at all.

Checked live on 2026-11-04 and 2026-11-05.
"""


def _said(attributes: Mapping[str, object], name: str) -> str | None:
    """
    Read one attribute as text, treating a sentinel as not having been said

    Parameters
    ----------
    attributes
        Every global attribute the file declared

    name
        The attribute to read

    Returns
    -------
    :
        Its value, or `None` if it is absent, empty or one of
        [NOT_SAID][(m).NOT_SAID]

    Examples
    --------
    >>> _said({"parent_experiment_id": "piControl"}, "parent_experiment_id")
    'piControl'

    A CMIP5 root run, which says so with a sentinel:

    >>> print(_said({"parent_experiment_id": "N/A"}, "parent_experiment_id"))
    None

    And a CMIP6 one, which says so with a different sentinel:

    >>> print(_said({"parent_experiment_id": "no parent"}, "parent_experiment_id"))
    None

    >>> print(_said({}, "parent_experiment_id"))
    None
    """
    raw = attributes.get(name)
    if raw is None:
        return None

    text = str(raw).strip()
    if text.casefold() in NOT_SAID:
        return None

    return text


def _number(attributes: Mapping[str, object], name: str) -> float | None:
    """
    Read one attribute as a number, if it is one

    Parameters
    ----------
    attributes
        Every global attribute the file declared

    name
        The attribute to read

    Returns
    -------
    :
        Its value as a `float`, or `None` if absent or not a number

        A value that is not a number is read as "not said" rather than raising: this
        is metadata written by dozens of modelling centres over twenty years, and one
        of them putting a word where a number belongs should cost us that field, not
        the whole header.

    Examples
    --------
    >>> _number({"branch_time": 240.0}, "branch_time")
    240.0

    A string that happens to be a number is still a number:

    >>> _number({"branch_time": "240"}, "branch_time")
    240.0

    >>> print(_number({"branch_time": "unknown"}, "branch_time"))
    None
    """
    raw = attributes.get(name)

    # `bool` first: it is an `int` subclass, so `True` would otherwise read as `1.0`.
    if isinstance(raw, bool):
        return None

    if isinstance(raw, (int, float)):
        return float(raw)

    if isinstance(raw, str):
        try:
            return float(raw)
        except ValueError:
            return None

    return None


class ParentHeaderReader(Protocol):
    """
    Turns one file's global attributes into what it says about its parent

    One per project, collected in
    [PARENT_HEADER_READERS_BY_PROJECT][(m).PARENT_HEADER_READERS_BY_PROJECT].
    """

    def __call__(
        self, attributes: Mapping[str, object], *, child: ParentSimulation
    ) -> ParentInfo:
        """
        Read what a file says about its parent

        Parameters
        ----------
        attributes
            Every global attribute the file declared, as
            [read_global_attributes][esmporium.headers.read_global_attributes]
            returned them

        child
            The simulation whose file this is, as the database holds it

            For a project which publishes its parent completely this is unused. It is
            here for CMIP5, which publishes no parent model and so has to take the
            child's -- and which must take it from **here** rather than from the
            file's own `model_id`, for the reason
            [read_cmip5_parent_header][(m).read_cmip5_parent_header] gives.

        Returns
        -------
        :
            What the file said

            An instance whose every field is `None` means the file was read and
            declared **no parent** -- which is how a root of the tree says so, and is
            a different statement from not having looked.
        """


def read_cmip6_parent_header(
    attributes: Mapping[str, object], *, child: ParentSimulation | None = None
) -> ParentInfo:
    """
    Read a CMIP6 or CMIP7 file's parent attributes

    These projects publish the parent completely, so nothing here is inferred: the
    file names the parent's era, activity, model, experiment and variant, and both
    branch times. Confirmed against live CMIP6 files on 2026-11-05.

    Parameters
    ----------
    attributes
        Every global attribute the file declared

    child
        The simulation whose file this is. **Unused**, and accepted only so that this
        satisfies [ParentHeaderReader][(m).ParentHeaderReader] -- a project which
        publishes its parent in full has nothing to infer.

    Returns
    -------
    :
        What the file said about its parent

    Examples
    --------
    >>> parent = read_cmip6_parent_header(
    ...     {
    ...         "parent_mip_era": "CMIP6",
    ...         "parent_activity_id": "CMIP",
    ...         "parent_source_id": "TaiESM1",
    ...         "parent_experiment_id": "piControl",
    ...         "parent_variant_label": "r1i1p1f1",
    ...         "parent_time_units": "days since 0701-01-01",
    ...         "branch_time_in_parent": 438000.0,
    ...         "branch_time_in_child": 0.0,
    ...     }
    ... )
    >>> parent.parent_model, parent.parent_experiment
    ('TaiESM1', 'piControl')
    >>> parent.branch_time_in_parent
    438000.0

    A root run, which CMIP6 announces with a sentinel:

    >>> read_cmip6_parent_header(
    ...     {"parent_experiment_id": "no parent"}
    ... ).declares_a_parent
    False
    """
    return ParentInfo(
        parent_project=_said(attributes, "parent_mip_era"),
        parent_activity=_said(attributes, "parent_activity_id"),
        parent_model=_said(attributes, "parent_source_id"),
        parent_experiment=_said(attributes, "parent_experiment_id"),
        parent_variant_label=_said(attributes, "parent_variant_label"),
        parent_time_units=_said(attributes, "parent_time_units"),
        branch_time_in_parent=_number(attributes, "branch_time_in_parent"),
        branch_time_in_child=_number(attributes, "branch_time_in_child"),
    )


def read_cmip5_parent_header(
    attributes: Mapping[str, object], *, child: ParentSimulation
) -> ParentInfo:
    """
    Read a CMIP5 file's parent attributes, inferring the two it does not publish

    CMIP5 publishes far less than CMIP6 and spells what it does publish differently.
    Measured across 47 models on 2026-11-04, a CMIP5 header carries
    `parent_experiment_id`, `parent_experiment_rip` and `branch_time`, **and nothing
    else about the parent at all**.

    Parameters
    ----------
    attributes
        Every global attribute the file declared

    child
        The simulation whose file this is, as the database holds it. Its project and
        model are what get inferred onto the parent.

    Returns
    -------
    :
        What the file said about its parent, plus the two inferred facts

    Examples
    --------
    >>> from esmporium.db import ParentSimulation
    >>> child = ParentSimulation(
    ...     project="CMIP5",
    ...     model="BCC-CSM1.1(m)",
    ...     experiment="abrupt4xCO2",
    ...     variant_label="r1i1p1",
    ... )
    >>> parent = read_cmip5_parent_header(
    ...     {
    ...         "parent_experiment_id": "piControl",
    ...         "parent_experiment_rip": "r1i1p1",
    ...         "branch_time": 240.0,
    ...         "model_id": "bcc-csm1-1-m",
    ...     },
    ...     child=child,
    ... )
    >>> parent.parent_experiment, parent.parent_variant_label
    ('piControl', 'r1i1p1')

    Note which spelling the parent's model gets: the child's, not the file's own
    `model_id`, because that is the one a search will match.

    >>> parent.parent_model, parent.parent_project
    ('BCC-CSM1.1(m)', 'CMIP5')

    `branch_time` is recorded verbatim, never interpreted:

    >>> parent.branch_time_in_parent
    240.0

    A root run declares no parent, and nothing is inferred onto it:

    >>> root = read_cmip5_parent_header({"parent_experiment_id": "N/A"}, child=child)
    >>> root.declares_a_parent
    False
    >>> print(root.parent_model)
    None

    A model which publishes no usable variant label still declares its parent, and the
    gap is left as a gap for a fix to close:

    >>> partial = read_cmip5_parent_header(
    ...     {"parent_experiment_id": "historical", "parent_experiment_rip": "N/A"},
    ...     child=child,
    ... )
    >>> partial.parent_experiment
    'historical'
    >>> print(partial.parent_variant_label)
    None
    """
    experiment = _said(attributes, "parent_experiment_id")
    variant_label = _said(attributes, "parent_experiment_rip")
    branch_time = _number(attributes, "branch_time")

    declares = experiment is not None or variant_label is not None

    return ParentInfo(
        # Inferred, and only once the file has declared a parent -- see above.
        parent_project=child.project if declares else None,
        parent_activity=None,
        parent_model=child.model if declares else None,
        parent_experiment=experiment,
        parent_variant_label=variant_label,
        # CMIP5 publishes no parent time units, so `branch_time` has no stated origin.
        parent_time_units=None,
        branch_time_in_parent=branch_time if declares else None,
        branch_time_in_child=None,
    )


PARENT_HEADER_READERS_BY_PROJECT: Mapping[str, ParentHeaderReader] = {
    "CMIP5": read_cmip5_parent_header,
    "CMIP6": read_cmip6_parent_header,
    # The same attributes as CMIP6, and reading them is **not needed**: CMIP7 publishes
    # its parent in the search index, so a CMIP7 walk never asks for a header. Here so
    # that a header read against CMIP7 is answered correctly rather than refused --
    # useful for checking what a file says against what the index says, which is the
    # one place the two could be found to disagree.
    "CMIP7": read_cmip6_parent_header,
}
"""
Which reader turns each project's global attributes into a
[ParentInfo][esmporium.search.ParentInfo]

Keyed by [Dataset.project][esmporium.db.schema.Dataset.project]. **Keyed by project
and not by file format**, which is worth stating because the formats vary within a
project: CMIP5 is mostly classic netCDF but `EC-EARTH`'s rcp85 output is netCDF-4. The
format decides who reads the bytes ([esmporium.headers][] settles that from the bytes
themselves); the project decides what the attributes mean.

Pass a mapping of your own to override one, e.g.
`{**PARENT_HEADER_READERS_BY_PROJECT, "CMIP5": my_reader}`.
"""


class UnknownProjectHeaderReaderError(KeyError):
    """
    Raised when a project's header has no reader
    """

    def __init__(self, project: str, known: Collection[str]) -> None:
        self.project = project
        """The project we have no reader for"""

        self.known = tuple(sorted(known))
        """The projects we do have one for"""

        super().__init__(
            f"No parent header reader is registered for project {project!r}. "
            f"Known projects: {', '.join(repr(one) for one in self.known)}. "
            "Which global attributes carry a parent differs by project -- CMIP5 "
            "calls the parent's variant label `parent_experiment_rip` where CMIP6 "
            "calls it `parent_variant_label` -- so there is no sensible default to "
            "fall back on. Pass a reader of your own in `readers`."
        )


def header_reader_for_project(
    project: str,
    readers: Mapping[str, ParentHeaderReader] = PARENT_HEADER_READERS_BY_PROJECT,
) -> ParentHeaderReader:
    """
    Get the header reader for one project

    Parameters
    ----------
    project
        The project to read for, as
        [Dataset.project][esmporium.db.schema.Dataset.project] spells it

    readers
        The readers to choose from

    Returns
    -------
    :
        The reader to use

    Raises
    ------
    UnknownProjectHeaderReaderError
        `project` has no reader.
    """
    found = readers.get(project)
    if found is None:
        raise UnknownProjectHeaderReaderError(project, readers)

    return found


@dataclass(frozen=True)
class FileCandidate:
    """
    One file whose header could be read, and the routes it could be read over
    """

    file_id: int
    """Which [File][esmporium.db.schema.File] this is"""

    filename: str
    """The file's name, for log lines"""

    published_size: int | None
    """How big the search index says it is, as a fallback for the node's figure"""

    routes: tuple[Route, ...]
    """Every fetchable route to it, for the selector to choose between"""

    def to_header_request(self) -> HeaderRequest:
        """
        Build the request that reads this file's header

        Returns
        -------
        :
            The request
        """
        return HeaderRequest(
            routes=self.routes,
            file_id=self.file_id,
            filename=self.filename,
            published_size=self.published_size,
        )


DEFAULT_MAX_FILE_CANDIDATES = 3
"""
How many of a simulation's files to try before giving up on it

Every file of a simulation carries the same parent, so any one of them answers the
question. A file whose data nodes do not answer is therefore bad luck rather than a
dead end -- the next file is tried instead, and the simulation is only given up on
once this many have failed.

One would be too few: a simulation whose other files are perfectly readable would be
lost to a single unlucky pick. Every file would be too many, because each unanswered
one costs a timeout, and that time is taken from the simulations still waiting. Three
files of about two routes each is at most six attempts.
"""


@dataclass(frozen=True)
class HeaderReadPlanItem:
    """
    One simulation's parent to read, and every dataset version its answer serves

    The unit the saving lives in. A simulation is published as many datasets -- one
    per variable, reporting interval and grid -- and every one of them has the same
    parent, because a parent belongs to the run and not to the variable. So one read
    serves all of them, and a plan is a list of *simulations* rather than of datasets.
    """

    simulation: ParentSimulation
    """Which simulation this reads the parent of"""

    dataset_version_ids: tuple[int, ...]
    """
    Every version this item's answer will be written against

    **The assumption this rests on, stated plainly**: that every dataset of one
    simulation has the same parent, across variables *and* across versions. The first
    half is certain -- a parent is a property of the run. The second is a judgement: a
    republication could in principle correct what a dataset says about its own parent,
    and this would not notice. It is taken because the alternative is a header read per
    version of per dataset, which is the cost that made this unusable before.

    If that assumption is ever found to break, this is the field to narrow.
    """

    candidates: tuple[FileCandidate, ...]
    """
    The files worth trying, best first

    More than one, because a simulation's best candidate can be unreadable for reasons
    that have nothing to do with the simulation -- see
    [DEFAULT_MAX_FILE_CANDIDATES][(m).DEFAULT_MAX_FILE_CANDIDATES]. Bounded for the
    same reason it is not one: an unbounded list is the death spiral.

    Ordered smallest first. Any file of a simulation has the same global attributes, so
    the smallest is as good an answer as any -- and it is the one that costs least if a
    data node ignores our range request and starts sending the whole thing.
    """

    @property
    def file_id(self) -> int:
        """
        The file that will be tried first

        Returns
        -------
        :
            Its id
        """
        return self.candidates[0].file_id

    @property
    def filename(self) -> str:
        """
        The name of the file that will be tried first

        Returns
        -------
        :
            Its name
        """
        return self.candidates[0].filename


@dataclass(frozen=True)
class HeaderReadPlan:
    """
    What reading some versions' parent facts will take
    """

    to_read: tuple[HeaderReadPlanItem, ...]
    """The headers that have to be fetched, one per simulation"""

    reusable: Mapping[int, int]
    """
    Versions whose facts can be copied from another version of the same simulation

    Keyed by the version needing facts, valued by the `DatasetVersionParent` row id to
    copy from. **The fast path, and it costs nothing at all**: if the parent of a
    simulation's `tas` is already stored, its `tos` has the same parent, so there is
    no reason to read a second header. This is what makes attaching ancestry to a
    whole archive affordable.
    """

    unreadable: tuple[ParentSimulation, ...]
    """
    Simulations whose parent cannot be read, because nothing fetchable was published

    Either no file is recorded for any of their datasets, or the only routes published
    are ones that are not bytes over HTTPS -- an OPeNDAP landing page, a Globus web
    app. Reported rather than raised, so that one unreachable simulation does not stop
    the rest, and named so a caller can say *which*.
    """


def _versions_needing_facts(
    session: Session, dataset_version_ids: Collection[int]
) -> dict[int, Dataset]:
    """
    Find which of these versions has no parent assertion yet, and its dataset

    Parameters
    ----------
    session
        The session to read with

    dataset_version_ids
        The versions to consider

    Returns
    -------
    :
        The dataset of each version with no assertion, keyed by version id

        A version which already has one is left out: re-reading a header costs a
        request for an answer we hold, which is what
        [ParentFactsProvider][esmporium.db.ParentFactsProvider] asks of an
        implementation.
    """
    wanted = tuple(dataset_version_ids)
    if not wanted:
        return {}

    already = set(
        session.exec(
            select(DatasetVersionParent.dataset_version_id).where(
                col(DatasetVersionParent.dataset_version_id).in_(wanted)
            )
        ).all()
    )

    rows = session.exec(
        select(DatasetVersion.id, Dataset)
        .join(Dataset, col(DatasetVersion.dataset_id) == col(Dataset.id))
        .where(col(DatasetVersion.id).in_(wanted))
    ).all()

    return {
        version_id: dataset
        for version_id, dataset in rows
        if version_id is not None and version_id not in already
    }


def _stored_assertion_for(
    session: Session, simulation: ParentSimulation
) -> DatasetVersionParent | None:
    """
    Find a header-derived assertion already stored for any version of a simulation

    The cross-dataset fast path. Looks across every dataset of the simulation, not just
    the ones asked about, because the point is that a parent found while chasing one
    variable is the parent of all of them.

    Parameters
    ----------
    session
        The session to read with

    simulation
        The simulation to look for

    Returns
    -------
    :
        An assertion to copy, or `None` if none is stored

        Only a [FILE_HEADER][esmporium.db.schema.ParentInfoSource.FILE_HEADER] one. A
        `SEARCH_METADATA` row is not copied, because a project whose index publishes
        parents does not need this path at all; a `FIX` is not copied, because a fix is
        aimed at the version it names and spreading it silently is not a fix's job.
    """
    return session.exec(
        select(DatasetVersionParent)
        .join(
            DatasetVersion,
            col(DatasetVersionParent.dataset_version_id) == col(DatasetVersion.id),
        )
        .join(Dataset, col(DatasetVersion.dataset_id) == col(Dataset.id))
        .where(col(DatasetVersionParent.source) == ParentInfoSource.FILE_HEADER.value)
        .where(col(Dataset.project) == simulation.project)
        .where(col(Dataset.model) == simulation.model)
        .where(col(Dataset.experiment) == simulation.experiment)
        .where(col(Dataset.variant_label) == simulation.variant_label)
        .order_by(col(DatasetVersionParent.id))
    ).first()


def _file_candidates(
    session: Session,
    dataset_version_ids: Collection[int],
    *,
    most: int = DEFAULT_MAX_FILE_CANDIDATES,
) -> tuple[FileCandidate, ...]:
    """
    Pick the few files of a simulation worth trying, smallest first

    Parameters
    ----------
    session
        The session to read with

    dataset_version_ids
        Every version of the simulation that needs facts

    most
        The most candidates to return. See
        [DEFAULT_MAX_FILE_CANDIDATES][(m).DEFAULT_MAX_FILE_CANDIDATES].

    Returns
    -------
    :
        The files to try, best first, and empty if none can be read at all

        Empty when the simulation has no files recorded, or none of them has a route
        that is bytes over HTTPS.
    """
    wanted = tuple(dataset_version_ids)
    if not wanted:
        return ()

    files = session.exec(
        select(File)
        .where(col(File.dataset_version_id).in_(wanted))
        # Smallest first, then by name so the choice is deterministic rather than
        # whatever order the database happened to return. A file with no recorded
        # size sorts last: it might be enormous.
        .order_by(
            col(File.size).is_(None),
            col(File.size),
            col(File.filename),
        )
    ).all()

    candidates: list[FileCandidate] = []
    seen_nodes: set[str] = set()
    for each in files:
        if len(candidates) >= most:
            break

        routes = prepare_routes(routes_for_file(session, flushed_id(each)))
        if not routes:
            continue

        # Prefer a file on a data node we have not already lined up. A second file on
        # the same node adds nothing a route fallback would not already have tried,
        # and the point of a second candidate is to reach a *different* node.
        nodes = {route.data_node for route in routes}
        if candidates and nodes <= seen_nodes:
            continue

        seen_nodes |= nodes
        candidates.append(
            FileCandidate(
                file_id=flushed_id(each),
                filename=each.filename,
                published_size=each.size,
                routes=routes,
            )
        )

    if candidates:
        return tuple(candidates)

    # Nothing reached a new node, which happens when every file is on one node. Fall
    # back to the smallest readable file rather than reporting the simulation
    # unreadable: one node is still a node.
    for each in files:
        routes = prepare_routes(routes_for_file(session, flushed_id(each)))
        if routes:
            return (
                FileCandidate(
                    file_id=flushed_id(each),
                    filename=each.filename,
                    published_size=each.size,
                    routes=routes,
                ),
            )

    return ()


def plan_header_reads(
    session: Session,
    dataset_version_ids: Collection[int],
    *,
    most_candidates: int = DEFAULT_MAX_FILE_CANDIDATES,
) -> HeaderReadPlan:
    """
    Work out what reading these versions' parent facts will take, without doing it

    Parameters
    ----------
    session
        The session to read with

    dataset_version_ids
        The versions to see to

    most_candidates
        The most files to be willing to try per simulation. See
        [DEFAULT_MAX_FILE_CANDIDATES][(m).DEFAULT_MAX_FILE_CANDIDATES].

    Returns
    -------
    :
        The plan. See [HeaderReadPlan][(m).HeaderReadPlan].
    """
    needing = _versions_needing_facts(session, dataset_version_ids)

    by_simulation: dict[ParentSimulation, list[int]] = {}
    for version_id, dataset in needing.items():
        simulation = ParentSimulation(
            project=dataset.project,
            model=dataset.model,
            experiment=dataset.experiment,
            variant_label=dataset.variant_label,
        )
        by_simulation.setdefault(simulation, []).append(version_id)

    to_read: list[HeaderReadPlanItem] = []
    reusable: dict[int, int] = {}
    unreadable: list[ParentSimulation] = []

    for simulation in sorted(by_simulation):
        version_ids = tuple(sorted(by_simulation[simulation]))

        stored = _stored_assertion_for(session, simulation)
        if stored is not None:
            # Already read, for another variable or another version of this run.
            for version_id in version_ids:
                reusable[version_id] = flushed_id(stored)
            continue

        candidates = _file_candidates(session, version_ids, most=most_candidates)
        if not candidates:
            unreadable.append(simulation)
            continue

        to_read.append(
            HeaderReadPlanItem(
                simulation=simulation,
                dataset_version_ids=version_ids,
                candidates=candidates,
            )
        )

    return HeaderReadPlan(
        to_read=tuple(to_read),
        reusable=reusable,
        unreadable=tuple(unreadable),
    )


def record_header_parent_facts(
    session: Session,
    item: HeaderReadPlanItem,
    header: FileHeader,
    *,
    readers: Mapping[str, ParentHeaderReader] = PARENT_HEADER_READERS_BY_PROJECT,
    file_id: int | None = None,
) -> tuple[DatasetVersionParent, ...]:
    """
    Write what one header said, against every dataset version it answers for

    Parameters
    ----------
    session
        The session to write into. Does not commit; the walk owns the boundary.

    item
        The planned read this header came from

    header
        What the file said

    readers
        Which reader to use per project. See
        [PARENT_HEADER_READERS_BY_PROJECT][(m).PARENT_HEADER_READERS_BY_PROJECT].

    file_id
        The file whose header this is, or `None` for the item's first candidate

        Worth passing whenever a later candidate answered, so that the provenance
        names the file the bytes really came from rather than the one we tried first.

    Returns
    -------
    :
        The rows written or reused, one per version

    Raises
    ------
    UnknownProjectHeaderReaderError
        The simulation's project has no reader registered.
    """
    reader = header_reader_for_project(item.simulation.project, readers)
    info = reader(header.attributes, child=item.simulation)
    read_from = file_id if file_id is not None else item.file_id

    logger.debug(
        "read the parent of %s out of %s",
        item.simulation,
        item.filename,
        extra={
            "simulation": str(item.simulation),
            "file_name": item.filename,
            "file_id": read_from,
            "data_node": header.route.data_node,
            "declares_a_parent": info.declares_a_parent,
            "n_versions_answered": len(item.dataset_version_ids),
        },
    )

    return tuple(
        save_parent_info(
            session,
            version_id,
            info,
            ParentInfoSource.FILE_HEADER,
            # Every row points at the file the bytes really came from, including the
            # sibling datasets whose own files were never touched. That is the honest
            # record: the fact came from this file.
            file_id=read_from,
        )
        for version_id in item.dataset_version_ids
    )


def copy_parent_facts(
    session: Session, *, from_row_id: int, to_dataset_version_ids: Collection[int]
) -> tuple[DatasetVersionParent, ...]:
    """
    Copy a stored header assertion onto other versions of the same simulation

    The fast path, and the one that costs nothing: a parent found while chasing `tas`
    is the parent of `tos` too, so the second variable needs no request at all.

    Parameters
    ----------
    session
        The session to write into. Does not commit.

    from_row_id
        The [DatasetVersionParent][esmporium.db.schema.DatasetVersionParent] row to
        copy

    to_dataset_version_ids
        The versions to copy it onto

    Returns
    -------
    :
        The rows written or reused
    """
    stored = session.get(DatasetVersionParent, from_row_id)
    if stored is None:  # pragma: no cover - the caller just read this row's id
        msg = f"no DatasetVersionParent row with id {from_row_id}"
        raise ValueError(msg)

    info = ParentInfo(
        parent_project=stored.parent_project,
        parent_activity=stored.parent_activity,
        parent_model=stored.parent_model,
        parent_experiment=stored.parent_experiment,
        parent_variant_label=stored.parent_variant_label,
        parent_time_units=stored.parent_time_units,
        branch_time_in_parent=stored.branch_time_in_parent,
        branch_time_in_child=stored.branch_time_in_child,
    )

    return tuple(
        save_parent_info(
            session,
            version_id,
            info,
            ParentInfoSource.FILE_HEADER,
            # The file the bytes originally came from, carried across. The fact is
            # still traceable to the bytes it was read out of, which is the whole
            # reason this column exists.
            file_id=stored.file_id,
        )
        for version_id in to_dataset_version_ids
    )


def _read_and_record(  # noqa: PLR0913 - one argument per injection seam
    session: Session,
    items: Sequence[HeaderReadPlanItem],
    *,
    readers: Mapping[str, ParentHeaderReader],
    client: httpx.Client | None,
    selector: DataNodeSelector,
    observer: HeaderReadCallObserver | None,
    record_health: bool,
    want_bytes: int,
    max_want_bytes: int,
    max_workers: int | None,
    per_data_node: int | None,
) -> tuple[HeaderReadPlanItem, ...]:
    """
    Read each simulation's header and record what it said, trying its files in turn

    **One pass per candidate file, not one request per candidate.** The first pass asks
    every simulation's best file at once; only the simulations that pass could not
    answer come back for their second. So a run where everything works costs exactly
    one request per simulation -- which is the whole saving -- and a simulation whose
    best file happens to live on a dead node costs a bounded handful rather than being
    given up on.

    Parameters
    ----------
    session
        The session to write through. Does not commit.

    items
        The simulations to read, each with its candidate files

    readers
        Which reader turns each project's attributes into a parent

    client
        The client to request with, or `None` to build one per pass

    selector
        Which route to try next for each file

    observer
        Told about each attempt, as well as the health recorder

    record_health
        Whether to record the attempts into the database

    want_bytes
        How many bytes to ask for first

    max_want_bytes
        The most to ask for before giving up on a file

    max_workers
        How many headers to read at once

    per_data_node
        How many reads may be in flight against one data node

    Returns
    -------
    :
        The simulations no file answered for
    """
    # Buffered rather than written as they happen. A `Session` must not be shared
    # between threads, and writing through a second connection would block on this
    # one's write lock -- so the attempts are collected here and written through the
    # caller's session once reading is finished and single-threaded again.
    collected: list[HeaderReadCall] = []
    guard = threading.Lock()

    def collect(call: HeaderReadCall) -> None:
        with guard:
            collected.append(call)

    watching: list[HeaderReadCallObserver] = []
    if record_health:
        watching.append(collect)
    if observer is not None:
        watching.append(observer)

    # Keyed by simulation, which is unique by construction -- `plan_header_reads`
    # groups the versions into one item per simulation. An answered simulation is
    # removed, so what is left at the end is exactly what nothing served.
    pending = {item.simulation: item for item in items}
    try:
        for attempt in range(max(len(item.candidates) for item in items)):
            asking = [
                item for item in pending.values() if attempt < len(item.candidates)
            ]
            if not asking:
                break

            if attempt:
                logger.info(
                    "trying another file for %d simulation(s) whose first did not "
                    "answer",
                    len(asking),
                    extra={
                        "n_simulations": len(asking),
                        "candidate_number": attempt + 1,
                    },
                )

            results = read_headers(
                [item.candidates[attempt].to_header_request() for item in asking],
                client=client,
                selector=selector,
                observer=fan_out(*watching) if watching else None,
                want_bytes=want_bytes,
                max_want_bytes=max_want_bytes,
                max_workers=max_workers,
                per_data_node=per_data_node,
            )

            for item, result in zip(asking, results):
                if result.header is None:
                    continue

                record_header_parent_facts(
                    session,
                    item,
                    result.header,
                    readers=readers,
                    file_id=item.candidates[attempt].file_id,
                )
                del pending[item.simulation]
    finally:
        # In a `finally` so that a read which raises still leaves behind what the data
        # nodes did, which is the evidence needed to work out why.
        if collected:
            write_header_read_calls(session, collected)

    return tuple(pending.values())


def build_header_facts_provider(  # noqa: PLR0913 - one argument per injection seam
    *,
    client: httpx.Client | None = None,
    selector: DataNodeSelector = DEFAULT_DATA_NODE_SELECTOR,
    readers: Mapping[str, ParentHeaderReader] = PARENT_HEADER_READERS_BY_PROJECT,
    fixes: ParentFixes | None = None,
    observer: HeaderReadCallObserver | None = None,
    record_health: bool = True,
    want_bytes: int = DEFAULT_WANT_BYTES,
    max_want_bytes: int = DEFAULT_MAX_WANT_BYTES,
    max_workers: int | None = None,
    per_data_node: int | None = DEFAULT_PER_DATA_NODE,
) -> ParentFactsProvider:
    """
    Build the [ParentFactsProvider][esmporium.db.ParentFactsProvider] that reads headers

    Parameters
    ----------
    client
        The client to request with, or `None` to build one per hop

        Worth passing for a walk of several hops, which otherwise builds and discards
        a client each time. Also the credentials seam, though no ESGF node has been
        seen to ask for any.

    selector
        Which route to try next for each file. See
        [build_header_read_health_selector][esmporium.db.build_header_read_health_selector]
        for one that prefers the nodes that have answered quickly before.

    readers
        Which reader turns each project's attributes into a parent. See
        [PARENT_HEADER_READERS_BY_PROJECT][(m).PARENT_HEADER_READERS_BY_PROJECT].

    fixes
        Corrections for simulations whose published parent is wrong, or `None` for none

        Pass `fixes_from_mapping(PARENT_FIXES)` from [esmporium.fixes][] to apply the
        ones we have evidence for. Applied **as well as** reading the header, never
        instead of it: both rows are kept, which is the whole point of holding one per
        source, and
        [SOURCE_PRECEDENCE][esmporium.db.SOURCE_PRECEDENCE] makes the reader believe
        the fix. So what a file said stays visible beside the correction to it, and a
        fix can be reviewed later against metadata that may since have been
        republished.

        A fix is applied to a simulation whether or not its header could be read, since
        a simulation nothing serves a header for is exactly one a fix may be the only
        answer for.

    observer
        Told about each HTTP attempt, *in addition to* the health recorder

    record_health
        Whether to record every attempt into
        [HeaderReadCallRecord][esmporium.db.schema.HeaderReadCallRecord]

        On by default: a walk that reads headers is the only thing that generates this
        evidence, so not recording it would leave the node ranking with nothing to go
        on next time.

        Buffered while reading and written through the caller's session once reading
        is done -- see the note above on why there is no engine here to write with
        instead. `False` leaves
        [HeaderReadCallRecord][esmporium.db.schema.HeaderReadCallRecord] untouched,
        which is worth having in a test but loses the only evidence a later run could
        rank data nodes by.

    want_bytes
        How many bytes of each file to ask for first

    max_want_bytes
        The most to ask for before giving up on a file

    max_workers
        How many headers to read at once, or `None` to read them one at a time

    per_data_node
        How many reads may be in flight against one data node

    Returns
    -------
    :
        A provider, suitable for `find_parents(..., facts_provider=...)`
    """

    def provider(session: Session, dataset_version_ids: Collection[int]) -> None:
        plan = plan_header_reads(session, dataset_version_ids)

        if plan.reusable:
            logger.debug(
                "reusing stored parent facts for %d version(s), no request needed",
                len(plan.reusable),
                extra={"n_versions": len(plan.reusable)},
            )
            by_row: dict[int, list[int]] = {}
            for version_id, row_id in plan.reusable.items():
                by_row.setdefault(row_id, []).append(version_id)
            for row_id, version_ids in by_row.items():
                copy_parent_facts(
                    session, from_row_id=row_id, to_dataset_version_ids=version_ids
                )

        if plan.unreadable:
            logger.warning(
                "cannot read a header for %d simulation(s): no fetchable route is "
                "published for any of their files",
                len(plan.unreadable),
                extra={
                    "n_simulations": len(plan.unreadable),
                    "simulations": [str(each) for each in plan.unreadable],
                },
            )

        if not plan.to_read:
            return

        logger.info(
            "reading %d header(s) to get the parents of %d dataset version(s)",
            len(plan.to_read),
            sum(len(item.dataset_version_ids) for item in plan.to_read),
            extra={
                "n_headers": len(plan.to_read),
                "n_versions": sum(
                    len(item.dataset_version_ids) for item in plan.to_read
                ),
            },
        )

        unanswered = _read_and_record(
            session,
            plan.to_read,
            readers=readers,
            client=client,
            selector=selector,
            observer=observer,
            record_health=record_health,
            want_bytes=want_bytes,
            max_want_bytes=max_want_bytes,
            max_workers=max_workers,
            per_data_node=per_data_node,
        )

        for item in unanswered:
            # Left unrecorded rather than raised. The walk checks afterwards that
            # every version it asked about got facts, and reports what is missing per
            # project -- so an unreadable simulation surfaces there with every other
            # simulation's facts already stored, instead of taking them down with it.
            logger.warning(
                "no data node served a header for %s, after trying %d file(s)",
                item.simulation,
                len(item.candidates),
                extra={
                    "simulation": str(item.simulation),
                    "n_files_tried": len(item.candidates),
                    "file_names": [each.filename for each in item.candidates],
                },
            )

    def provider_with_fixes(
        session: Session, dataset_version_ids: Collection[int]
    ) -> None:
        # The versions needing anything, worked out *before* the headers are read, so
        # that a fix still lands for a simulation whose header nothing served -- which
        # is precisely the case a fix may be the only answer for.
        needing = _versions_needing_facts(session, dataset_version_ids)

        provider(session, dataset_version_ids)

        if fixes is None:
            return

        applied = 0
        for version_id, dataset in needing.items():
            simulation = ParentSimulation(
                project=dataset.project,
                model=dataset.model,
                experiment=dataset.experiment,
                variant_label=dataset.variant_label,
            )
            fix = fixes(simulation)
            if fix is None:
                continue

            save_parent_info(session, version_id, fix.info, ParentInfoSource.FIX)
            applied += 1

            logger.info(
                "applied a parent fix to %s: %s",
                simulation,
                fix.reason,
                extra={
                    "simulation": str(simulation),
                    "dataset_version_id": version_id,
                    "fix_reason": fix.reason,
                    "fix_evidence": fix.evidence,
                },
            )

        if applied:
            session.flush()

    return provider_with_fixes


def build_parent_searcher_with_file_access(
    *,
    selector: SearchAPIFacadeSelector = DEFAULT_SEARCH_API_SELECTOR,
    client: httpx.Client | None = None,
    api_call_observer: SearchAPICallObserver | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit: int = DEFAULT_LIMIT,
) -> ParentSearcher:
    """
    Build the [ParentSearcher][esmporium.db.ParentSearcher] that a header read needs

    Parameters
    ----------
    selector
        Which search API to use, and what to fall back to

    client
        The client to search with, or `None` to build one per request

    api_call_observer
        Told about each search API call, dataset and file requests alike

    batch_size
        How many datasets to ask about per `type=File` request

    limit
        Page size for the file requests

    Returns
    -------
    :
        A searcher which finds one planned parent's datasets *and* their files
    """

    def searcher(session: Session, item: ParentSearchPlanItem) -> None:
        search_single_project(
            item.to_canonical_query(),
            selector,
            client=client,
            api_call_observer=api_call_observer,
            processor=build_result_processor_with_file_access(
                session,
                client=client,
                batch_size=batch_size,
                limit=limit,
                api_call_observer=api_call_observer,
            ),
            build_request=build_parent_dataset_request,
        )

    return searcher
