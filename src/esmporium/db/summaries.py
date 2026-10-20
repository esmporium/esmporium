"""
Summaries of what a tracked search returned, and of what changed

[esmporium.db.search_tracking][] records a search's answers run by run, and works out
the difference between one run and the last complete run before it. What it records is
keyed by database ids, though: a
[DatasetChangeInfo][esmporium.db.DatasetChangeInfo] says that dataset version 417 was
retracted, which is true and unreadable.

This module joins that record back to the facets which name a dataset, folds a window
of several runs down to its net effect, and hands the answer over as either a
[pandas.DataFrame][] or, via [esmporium.db.summary_formatting][], as text. Nothing
here writes to the database.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pandas as pd
from sqlmodel import Session, col, select

from esmporium.datasets import DATASET_FACET_COLUMNS
from esmporium.db.schema import (
    Dataset,
    DatasetChangeType,
    DatasetVersion,
    SearchRunVersion,
)
from esmporium.db.search_tracking import (
    RequirementNotFoundError,
    SearchRunLeafInfo,
    find_requirement,
    get_changes,
    get_run_leaves,
    get_search_runs,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from sqlalchemy.engine import Engine

    from esmporium.db.search_tracking import DatasetChangeInfo, SearchRunInfo


MovedValues = Mapping[str, Mapping[str, object]]
"""
What moved about a dataset version, keyed by whichever of
`retracted`, `is_latest` and `data_nodes` moved

Each entry holds the old and the new value, e.g.
`{"retracted": {"old": False, "new": True}}`. The same shape as
[DatasetChangeInfo.detail][esmporium.db.DatasetChangeInfo.detail], because that is
where it comes from.
"""

_CHANGE_TYPE_ORDER: dict[DatasetChangeType, int] = {
    DatasetChangeType.ADDED: 0,
    DatasetChangeType.REMOVED: 1,
    DatasetChangeType.MODIFIED: 2,
}
"""
The order we report the change types in
"""


class ConflictingWindowError(ValueError):
    """Raised when a summary is asked to cover two different windows at once"""

    def __init__(self, since: datetime.datetime | int, n_runs: int) -> None:
        """
        Initialise

        Parameters
        ----------
        since
            The `since` the caller passed

        n_runs
            The `n_runs` the caller passed
        """
        self.since = since
        """The `since` the caller passed"""

        self.n_runs = n_runs
        """The `n_runs` the caller passed"""

        msg = (
            f"A summary covers one window, but both since={since!r} and "
            f"n_runs={n_runs!r} were given. "
            "Pass since to count from a time or from a run id, "
            "or n_runs to count back a number of runs, not both."
        )
        super().__init__(msg)


class NoRunsInWindowError(ValueError):
    """Raised when a tracked search has no run to summarise"""

    def __init__(
        self,
        name_or_id: str | int,
        *,
        n_stored_runs: int,
        since: datetime.datetime | int | None,
        n_runs: int | None,
    ) -> None:
        """
        Initialise

        Parameters
        ----------
        name_or_id
            The requirement which was asked about

        n_stored_runs
            How many runs of it are stored, in total

        since
            The `since` the caller passed, if any

        n_runs
            The `n_runs` the caller passed, if any
        """
        self.name_or_id = name_or_id
        """The requirement which was asked about"""

        self.n_stored_runs = n_stored_runs
        """How many runs of it are stored, in total"""

        if n_stored_runs == 0:
            msg = (
                f"The requirement {name_or_id!r} has never been run, "
                "so there is nothing to summarise. "
                "Run it with search_and_track or rerun_search_esgf first."
            )
        else:
            msg = (
                f"No run of the requirement {name_or_id!r} falls in the window asked "
                f"for (since={since!r}, n_runs={n_runs!r}), "
                f"although {n_stored_runs} runs are stored. "
                "Use get_search_runs to see which runs there are."
            )

        super().__init__(msg)


@dataclass(frozen=True)
class VersionRecord:
    """
    One dataset version a run returned, with the facets which name it

    What a run was told about the version, rather than what the
    [DatasetVersion][esmporium.db.schema.DatasetVersion] row says now: those columns
    are refreshed on every ingest, and a summary of last Tuesday has to report last
    Tuesday's answer.
    """

    dataset_version_id: int
    """The version's id"""

    dataset_id: int
    """The id of the dataset this is a version of"""

    id_project_specific: str
    """The dataset's native id, as the project spells it"""

    facets: Mapping[str, str | None]
    """
    The dataset's facets, keyed by
    [DATASET_FACET_COLUMNS][esmporium.datasets.DATASET_FACET_COLUMNS]

    A mapping rather than one field per facet on purpose:
    [Dataset][esmporium.db.schema.Dataset] and
    [CatalogueEntry][esmporium.requirements.CatalogueEntry] already spell that list
    out twice, and a third copy would be a third thing to keep in step.
    """

    version: str
    """See [DatasetVersion.version][esmporium.db.schema.DatasetVersion.version]."""

    is_latest: bool
    """
    See [SearchRunVersion.is_latest][esmporium.db.schema.SearchRunVersion.is_latest].
    """

    retracted: bool
    """
    See [SearchRunVersion.retracted][esmporium.db.schema.SearchRunVersion.retracted].
    """

    data_nodes: tuple[str, ...]
    """The data nodes the run saw serving this version, sorted"""

    seen_in_run_id: int
    """
    The run whose answer this record holds

    The newest run of the window, for a version the window ends with. For a version
    which was removed, the last run which still saw it, because a removed version has
    no answer in the run that removed it.
    """


@dataclass(frozen=True)
class NetChange:
    """
    What happened to one dataset version across a window of runs, net of churn

    The output of [collapse_changes][(m).], and so the thing to start from when
    checking how a window is folded: it names a version by its id only, and carries no
    facets, because folding needs nothing from the database.
    """

    dataset_version_id: int
    """The version which changed"""

    change_type: DatasetChangeType
    """What happened to it, net of everything in between"""

    detail: MovedValues | None
    """
    What moved, for a
    [modified][esmporium.db.schema.DatasetChangeType.MODIFIED] change

    `None` for an addition or a removal, where the change is the presence of the
    version rather than anything about it.
    """

    first_run_id: int
    """The first run in the window which reported a change to this version"""

    last_run_id: int
    """The last run in the window which reported a change to this version"""


@dataclass(frozen=True)
class ChangeRecord:
    """One net change, and the version it happened to"""

    change_type: DatasetChangeType
    """See [NetChange.change_type][esmporium.db.summaries.NetChange.change_type]."""

    version: VersionRecord
    """The version it happened to, which is what [NetChange][(m).] does not carry"""

    detail: MovedValues | None
    """See [NetChange.detail][esmporium.db.summaries.NetChange.detail]."""

    first_run_id: int
    """See [NetChange.first_run_id][esmporium.db.summaries.NetChange.first_run_id]."""

    last_run_id: int
    """See [NetChange.last_run_id][esmporium.db.summaries.NetChange.last_run_id]."""

    @property
    def added_but_retracted(self) -> bool:
        """
        Whether this version arrived already retracted

        Worth saying separately, because "there is new data" and "there is new data
        you must not use" are different pieces of news.
        """
        return self.change_type is DatasetChangeType.ADDED and self.version.retracted


@dataclass(frozen=True)
class RunHealth:
    """
    How a run went, i.e. whether its answer can be believed

    A removal means "the search no longer returns this", which is the same thing a
    broken endpoint and a truncated answer both look like. So a summary of what
    changed is not readable without this beside it.
    """

    run_id: int
    """The run's id"""

    started_at: datetime.datetime
    """See [SearchRun.started_at][esmporium.db.schema.SearchRun.started_at]."""

    finished_at: datetime.datetime | None
    """See [SearchRun.finished_at][esmporium.db.schema.SearchRun.finished_at]."""

    complete: bool
    """See [SearchRun.complete][esmporium.db.schema.SearchRun.complete]."""

    leaves: tuple[SearchRunLeafInfo, ...]
    """How each of the run's sub-searches went, in the order they were planned"""

    @property
    def n_leaves(self) -> int:
        """How many sub-searches the run fanned out into"""
        return len(self.leaves)

    @property
    def n_answered(self) -> int:
        """How many of the sub-searches answered"""
        return sum(1 for leaf in self.leaves if leaf.answered)

    @property
    def hosts(self) -> tuple[str, ...]:
        """The hosts whose answers the run kept, sorted and deduplicated"""
        return tuple(sorted({leaf.host for leaf in self.leaves if leaf.host}))

    @property
    def n_documents(self) -> int:
        """How many documents the run collected, across every sub-search"""
        return sum(leaf.n_documents for leaf in self.leaves)

    @property
    def n_matches(self) -> int | None:
        """
        How many documents the hosts said matched, across every sub-search

        `None` when any sub-search's host reported no total, because then the run's
        total is not known rather than being short by the missing part.
        """
        if any(leaf.n_matches is None for leaf in self.leaves):
            return None

        return sum(leaf.n_matches or 0 for leaf in self.leaves)

    @property
    def failures(self) -> tuple[SearchRunLeafInfo, ...]:
        """The sub-searches which failed"""
        return tuple(leaf for leaf in self.leaves if not leaf.answered)

    @property
    def truncated(self) -> tuple[SearchRunLeafInfo, ...]:
        """The sub-searches which collected fewer documents than matched"""
        return tuple(leaf for leaf in self.leaves if leaf.truncated)


@dataclass(frozen=True)
class SearchSummary:
    """
    What a tracked search returned, and what changed, ready to read

    Built by [summarise_search][(m).]. Holds no database connection, so it can be
    rendered, picked apart or passed around long after the session which read it has
    closed.
    """

    requirement_name: str
    """The name of the search this summarises"""

    stored_requirement_id: int
    """The id of the requirement this summarises"""

    run: RunHealth
    """The newest run in the window, and how it went"""

    baseline_run_id: int | None
    """
    The run the window is measured against

    `None` when there was nothing to compare with, i.e. the window starts at the
    requirement's first run. Not simply "the run before": incomplete runs are skipped
    as baselines, so this can be several days earlier than the window's start. See
    [DatasetChange.baseline_run_id][esmporium.db.schema.DatasetChange.baseline_run_id],
    which is where this is read from.
    """

    window_runs: tuple[int, ...]
    """Every run this summary covers, oldest first"""

    changes: tuple[ChangeRecord, ...]
    """
    What changed across the window, one entry per version

    Net of everything in between, so a version which was added and then modified
    appears once, as an addition.
    """

    churned: int
    """
    How many versions moved inside the window and ended up back where they started

    Added and then removed again, or modified and then modified back. Counted rather
    than listed, because the net news about them is that there is no news.
    """

    current: tuple[VersionRecord, ...]
    """Every dataset version the newest run in the window returned"""

    @property
    def any_changes(self) -> bool:
        """Whether anything changed at all"""
        return bool(self.changes)

    @property
    def counts(self) -> Mapping[DatasetChangeType, int]:
        """How many changes of each type there were, including the types with none"""
        return {
            change_type: sum(
                1 for change in self.changes if change.change_type is change_type
            )
            for change_type in _CHANGE_TYPE_ORDER
        }

    @property
    def headline(self) -> str:
        """
        One line saying what happened, for a subject line or a log

        Examples
        --------
        >>> from esmporium.db.summaries import SearchSummary
        >>> # Built by hand here; summarise_search is the usual way.
        >>> summary = SearchSummary(
        ...     requirement_name="pattern-effect",
        ...     stored_requirement_id=1,
        ...     run=RunHealth(
        ...         run_id=7,
        ...         started_at=datetime.datetime(2026, 10, 20),
        ...         finished_at=datetime.datetime(2026, 10, 20),
        ...         complete=True,
        ...         leaves=(),
        ...     ),
        ...     baseline_run_id=6,
        ...     window_runs=(7,),
        ...     changes=(),
        ...     churned=0,
        ...     current=(),
        ... )
        >>> summary.headline
        'pattern-effect: no changes'
        """
        counts = self.counts
        parts = [
            f"{counts[change_type]} {change_type.value}"
            for change_type in _CHANGE_TYPE_ORDER
            if counts[change_type]
        ]

        said = ", ".join(parts) if parts else "no changes"
        if not self.run.complete:
            said = f"{said} (run incomplete)"

        return f"{self.requirement_name}: {said}"

    def changes_frame(self) -> pd.DataFrame:
        """
        Get what changed, as a frame with one row per changed version

        Returns
        -------
        :
            One row per entry in `changes`, with the dataset's facets as columns.
            Empty, but with the same columns, when nothing changed.
        """
        return pd.DataFrame(
            [_change_row(change) for change in self.changes],
            columns=list(_CHANGE_FRAME_COLUMNS),
        )

    def current_frame(self) -> pd.DataFrame:
        """
        Get what the search returns now, as a frame with one row per dataset version

        Returns
        -------
        :
            One row per entry in `current`, with the dataset's facets as columns.
            Empty, but with the same columns, when the run returned nothing.
        """
        return pd.DataFrame(
            [_version_row(version) for version in self.current],
            columns=list(_CURRENT_FRAME_COLUMNS),
        )

    def render(self, *, max_rows: int = 20, facets: Sequence[str] | None = None) -> str:
        """
        Render this summary as text

        Parameters
        ----------
        max_rows
            How many rows of each table to show before summarising the rest

        facets
            Which dataset facets to put in the tables, or `None` for the default.
            See [format_summary][esmporium.db.summary_formatting.format_summary].

        Returns
        -------
        :
            The summary, as lines of text with no trailing whitespace
        """
        # Deferred: summary_formatting imports our types, so importing it up here
        # would be a cycle. The renderer is the leaf of the pair, not us.
        from esmporium.db.summary_formatting import (  # noqa: PLC0415
            format_summary,
        )

        return format_summary(self, max_rows=max_rows, facets=facets)


_CHANGE_FRAME_COLUMNS: tuple[str, ...] = (
    "change",
    "dataset_version_id",
    *DATASET_FACET_COLUMNS,
    "version",
    "is_latest",
    "retracted",
    "data_nodes",
    "moved",
    "first_run_id",
    "last_run_id",
)
"""The columns of [SearchSummary.changes_frame][(m).SearchSummary.changes_frame]"""

_CURRENT_FRAME_COLUMNS: tuple[str, ...] = (
    "dataset_version_id",
    "id_project_specific",
    *DATASET_FACET_COLUMNS,
    "version",
    "is_latest",
    "retracted",
    "data_nodes",
)
"""The columns of [SearchSummary.current_frame][(m).SearchSummary.current_frame]"""


def _version_row(version: VersionRecord) -> dict[str, object]:
    """Turn one version record into a row of the current-state frame."""
    return {
        "dataset_version_id": version.dataset_version_id,
        "id_project_specific": version.id_project_specific,
        **{name: version.facets.get(name) for name in DATASET_FACET_COLUMNS},
        "version": version.version,
        "is_latest": version.is_latest,
        "retracted": version.retracted,
        "data_nodes": ", ".join(version.data_nodes),
    }


def _change_row(change: ChangeRecord) -> dict[str, object]:
    """Turn one change record into a row of the changes frame."""
    version = change.version

    return {
        "change": change.change_type.value,
        "dataset_version_id": version.dataset_version_id,
        **{name: version.facets.get(name) for name in DATASET_FACET_COLUMNS},
        "version": version.version,
        "is_latest": version.is_latest,
        "retracted": version.retracted,
        "data_nodes": ", ".join(version.data_nodes),
        "moved": describe_moved(change.detail),
        "first_run_id": change.first_run_id,
        "last_run_id": change.last_run_id,
    }


def _readable(value: object) -> str:
    """
    Render one recorded value for a person

    Booleans become `yes`/`no`, because that is how every table here spells
    `retracted`, and `retracted: False -> True` makes a reader translate.
    """
    if isinstance(value, bool):
        return "yes" if value else "no"

    return str(value)


def describe_moved(detail: MovedValues | None) -> str:
    """
    Describe what moved about a version, in one line

    Parameters
    ----------
    detail
        What moved, as
        [DatasetChangeInfo.detail][esmporium.db.DatasetChangeInfo.detail] records it

    Returns
    -------
    :
        One `name: old -> new` clause per thing which moved, in a stable order, or an
        empty string when nothing moved

    Examples
    --------
    >>> describe_moved({"retracted": {"old": False, "new": True}})
    'retracted: no -> yes'

    Data nodes are reported as what joined and what left, rather than as two lists:

    >>> describe_moved(
    ...     {
    ...         "data_nodes": {
    ...             "old": ["esgf.nci.org.au"],
    ...             "new": ["esgf.nci.org.au", "aims3.llnl.gov"],
    ...         }
    ...     }
    ... )
    'data_nodes: +aims3.llnl.gov'
    """
    if not detail:
        return ""

    clauses = []
    for name in sorted(detail):
        moved = detail[name]
        old = moved.get("old")
        new = moved.get("new")

        if isinstance(old, list) and isinstance(new, list):
            joined = sorted(set(new) - set(old))
            left = sorted(set(old) - set(new))
            movement = ", ".join(
                [*(f"+{node}" for node in joined), *(f"-{node}" for node in left)]
            )
            clauses.append(f"{name}: {movement}")
        else:
            clauses.append(f"{name}: {_readable(old)} -> {_readable(new)}")

    return "; ".join(clauses)


def _merge_moved(
    merged: MovedValues, incoming: MovedValues
) -> dict[str, dict[str, object]]:
    """
    Fold a later change's detail into what we have so far

    The oldest `old` and the newest `new` win, so that a value which moved twice reads
    as having gone from where it started to where it ended up.
    """
    out: dict[str, dict[str, object]] = {
        name: dict(moved) for name, moved in merged.items()
    }

    for name, moved in incoming.items():
        if name in out:
            out[name] = {"old": out[name]["old"], "new": moved["new"]}
        else:
            out[name] = dict(moved)

    return out


def _settled(moved: Mapping[str, Mapping[str, object]]) -> dict[str, dict[str, object]]:
    """Drop whatever moved and then moved back, so that only real movement is left."""
    return {
        name: dict(values)
        for name, values in moved.items()
        if values.get("old") != values.get("new")
    }


@dataclass
class _Fold:
    """What we know so far about one version's journey through a window"""

    first_run_id: int
    """The first run which reported a change to it"""

    last_run_id: int
    """The last run which reported a change to it"""

    absent_at_start: bool
    """Whether it was absent when the window opened, i.e. its first change added it"""

    absent_now: bool
    """Whether it is absent now, i.e. its last change removed it"""

    moved: dict[str, dict[str, object]]
    """What has moved about it, merged across the window"""


def collapse_changes(
    rows: Iterable[tuple[int, DatasetChangeInfo]],
) -> tuple[tuple[NetChange, ...], int]:
    """
    Collapse a window's recorded changes down to one net change per version

    A window of several runs can say three things about the same version: that it
    arrived on Tuesday, that it was retracted on Thursday, and that its data nodes
    moved on Friday. What a reader wants is one line saying where it ended up, which
    is what this works out.

    A version is taken to have been absent when the window opened if its first change
    in the window added it, and to be absent now if its last change removed it. From
    those two facts, plus what moved in between:

    - absent, then present: an addition
    - present, then absent: a removal
    - present throughout, with something still moved: a modification
    - present throughout with nothing left moved, or absent at both ends: churn, which
      is counted and not reported, because the net news is that there is no news

    Parameters
    ----------
    rows
        The recorded changes, as `(run id, change)` pairs, **oldest first**. Order
        matters: it is what decides which `old` and which `new` survive.

    Returns
    -------
    :
        The net changes, and how many versions churned

    Examples
    --------
    A version which was added and then retracted inside the window is still news, and
    it is news of an addition:

    >>> from esmporium.db import DatasetChangeInfo
    >>> from esmporium.db.schema import DatasetChangeType
    >>> added = DatasetChangeInfo(
    ...     dataset_version_id=417,
    ...     change_type=DatasetChangeType.ADDED,
    ...     baseline_run_id=2,
    ...     detail=None,
    ... )
    >>> retracted = DatasetChangeInfo(
    ...     dataset_version_id=417,
    ...     change_type=DatasetChangeType.MODIFIED,
    ...     baseline_run_id=3,
    ...     detail={"retracted": {"old": False, "new": True}},
    ... )
    >>> net, churned = collapse_changes([(3, added), (4, retracted)])
    >>> net[0].change_type.value, net[0].first_run_id, net[0].last_run_id
    ('added', 3, 4)
    >>> churned
    0

    A version which arrived and went again inside the window churned:

    >>> removed = DatasetChangeInfo(
    ...     dataset_version_id=417,
    ...     change_type=DatasetChangeType.REMOVED,
    ...     baseline_run_id=3,
    ...     detail=None,
    ... )
    >>> net, churned = collapse_changes([(3, added), (4, removed)])
    >>> net, churned
    ((), 1)
    """
    folds: dict[int, _Fold] = {}

    for run_id, change in rows:
        fold = folds.get(change.dataset_version_id)
        if fold is None:
            folds[change.dataset_version_id] = _Fold(
                first_run_id=run_id,
                last_run_id=run_id,
                absent_at_start=change.change_type is DatasetChangeType.ADDED,
                absent_now=change.change_type is DatasetChangeType.REMOVED,
                moved={
                    name: dict(moved) for name, moved in (change.detail or {}).items()
                },
            )
            continue

        fold.last_run_id = run_id
        fold.absent_now = change.change_type is DatasetChangeType.REMOVED
        if change.detail:
            fold.moved = _merge_moved(fold.moved, change.detail)

    net = []
    churned = 0
    for dataset_version_id, fold in folds.items():
        if fold.absent_at_start and not fold.absent_now:
            change_type = DatasetChangeType.ADDED
            detail = None
        elif not fold.absent_at_start and fold.absent_now:
            change_type = DatasetChangeType.REMOVED
            detail = None
        else:
            settled = _settled(fold.moved)
            if fold.absent_at_start or not settled:
                churned += 1
                continue

            change_type = DatasetChangeType.MODIFIED
            detail = settled

        net.append(
            NetChange(
                dataset_version_id=dataset_version_id,
                change_type=change_type,
                detail=detail,
                first_run_id=fold.first_run_id,
                last_run_id=fold.last_run_id,
            )
        )

    return tuple(net), churned


def _as_utc(value: datetime.datetime) -> datetime.datetime:
    """
    Read a datetime as UTC, whether or not it says so

    Recorded-at columns are written timezone-aware (see `_utcnow` in
    [esmporium.db.schema][]), but SQLite has nowhere to keep the offset and hands them
    back naive. Both sides of a comparison have to agree, so a naive value is taken at
    its word: UTC, because UTC is what was written.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)

    return value


def _check_window(since: datetime.datetime | int | None, n_runs: int | None) -> None:
    """
    Check that the window asked for is one window

    Raises
    ------
    ConflictingWindowError
        Both `since` and `n_runs` were given

    ValueError
        `n_runs` is not at least one
    """
    if since is not None and n_runs is not None:
        raise ConflictingWindowError(since, n_runs)

    if n_runs is not None and n_runs < 1:
        msg = f"n_runs must be at least one, received n_runs={n_runs!r}"
        raise ValueError(msg)


def _window(
    runs: Sequence[SearchRunInfo],
    since: datetime.datetime | int | None,
    n_runs: int | None,
) -> tuple[SearchRunInfo, ...]:
    """
    Pick the runs a summary covers, oldest first

    `runs` arrives newest first, as [get_search_runs][esmporium.db.get_search_runs]
    returns it.
    """
    oldest_first = tuple(reversed(runs))

    if n_runs is not None:
        return oldest_first[-n_runs:]

    if since is None:
        return oldest_first[-1:]

    if isinstance(since, datetime.datetime):
        wanted = _as_utc(since)

        return tuple(run for run in oldest_first if _as_utc(run.started_at) >= wanted)

    return tuple(run for run in oldest_first if run.id >= since)


def _version_records(
    session: Session, run_ids: Sequence[int], *, latest_run_id: int
) -> tuple[dict[int, VersionRecord], tuple[int, ...]]:
    """
    Read what each run was told about each version, and which the newest run returned

    One query, joining a run's answers to the facets which name the dataset. The rows
    come back oldest run first and later rows overwrite earlier ones, so each version
    ends up holding the answer of the last run which saw it. For a version which was
    removed that is the last run which still had it, which is the only answer anyone
    ever wrote down about it.

    Returns
    -------
    :
        Every version any of these runs saw, keyed by id, and the ids of the versions
        the newest run returned
    """
    rows = session.exec(
        select(SearchRunVersion, DatasetVersion, Dataset)
        .join(
            DatasetVersion,
            col(SearchRunVersion.dataset_version_id) == col(DatasetVersion.id),
        )
        .join(Dataset, col(DatasetVersion.dataset_id) == col(Dataset.id))
        .where(col(SearchRunVersion.search_run_id).in_(run_ids))
        .order_by(col(SearchRunVersion.search_run_id))
    ).all()

    records: dict[int, VersionRecord] = {}
    in_latest_run: list[int] = []

    for seen, version, dataset in rows:
        dataset_version_id = seen.dataset_version_id

        records[dataset_version_id] = VersionRecord(
            dataset_version_id=dataset_version_id,
            dataset_id=version.dataset_id,
            id_project_specific=dataset.id_project_specific,
            facets={
                name: cast("str | None", getattr(dataset, name))
                for name in DATASET_FACET_COLUMNS
            },
            version=version.version,
            is_latest=seen.is_latest,
            retracted=seen.retracted,
            data_nodes=tuple(json.loads(seen.data_nodes_json)),
            seen_in_run_id=seen.search_run_id,
        )

        if seen.search_run_id == latest_run_id:
            in_latest_run.append(dataset_version_id)

    return records, tuple(in_latest_run)


def _sort_key(version: VersionRecord) -> tuple[str, ...]:
    """Sort versions the way a reader scans them: by facet, then by version."""
    return (
        *(version.facets.get(name) or "" for name in DATASET_FACET_COLUMNS),
        version.version,
    )


def _missing_version_record(net: NetChange, run_id: int) -> VersionRecord:
    """
    Stand in for a version no run in the window recorded an answer about

    This should not happen: a change is written about a version a run saw, so the
    answer is there to be read. It can happen if the history rows were pruned and the
    change log was not, and losing the whole summary over one unreadable row would be
    the wrong trade, so the row is reported with the facets left empty.
    """
    return VersionRecord(
        dataset_version_id=net.dataset_version_id,
        dataset_id=-1,
        id_project_specific="",
        facets={name: None for name in DATASET_FACET_COLUMNS},
        version="",
        is_latest=False,
        retracted=False,
        data_nodes=(),
        seen_in_run_id=run_id,
    )


def summarise_search(
    engine: Engine,
    name_or_id: str | int,
    *,
    since: datetime.datetime | int | None = None,
    n_runs: int | None = None,
) -> SearchSummary:
    """
    Summarise what a tracked search returned, and what changed

    Reads what [search_and_track][esmporium.db.search_and_track] recorded. By default
    that is the newest run and the baseline its own changes were measured against,
    which is the question a watched search asks every morning: what is different
    today?

    Parameters
    ----------
    engine
        The database to read from

    name_or_id
        Which tracked search to summarise. See
        [find_requirement][esmporium.db.find_requirement].

    since
        Cover every run from this point onwards.

        A [datetime.datetime][] selects the runs which started at or after it; an
        [int][] is a run id and selects that run and every run after it. Naive
        datetimes are read as UTC, because UTC is what is recorded.

        Cannot be combined with `n_runs`.

    n_runs
        Cover this many runs, counting back from the newest.

        Cannot be combined with `since`.

    Returns
    -------
    :
        The summary, holding no database connection, so it outlives the session which
        read it

    Raises
    ------
    RequirementNotFoundError
        No requirement is stored with that name or id

    ConflictingWindowError
        Both `since` and `n_runs` were given

    NoRunsInWindowError
        The requirement has never been run, or no run falls in the window asked for
    """
    _check_window(since, n_runs)

    runs = get_search_runs(engine, name_or_id)
    window = _window(runs, since, n_runs)
    if not window:
        raise NoRunsInWindowError(
            name_or_id, n_stored_runs=len(runs), since=since, n_runs=n_runs
        )

    newest = window[-1]
    window_ids = tuple(run.id for run in window)

    recorded: list[tuple[int, DatasetChangeInfo]] = [
        (run.id, change) for run in window for change in get_changes(engine, run.id)
    ]
    net_changes, churned = collapse_changes(recorded)

    # The window is measured against whatever the oldest run in it was measured
    # against, which is recorded on that run's own changes rather than being "the run
    # before": incomplete runs are skipped as baselines.
    baseline_run_id = next(
        (
            change.baseline_run_id
            for run_id, change in recorded
            if run_id == window[0].id
        ),
        None,
    )

    # The baseline is read too, so that a version removed by the window's first run
    # still has an answer to report.
    to_read = (*window_ids, *((baseline_run_id,) if baseline_run_id else ()))

    with Session(engine) as session:
        stored = find_requirement(session, name_or_id)
        if stored is None:  # pragma: no cover - get_search_runs has already checked
            raise RequirementNotFoundError(name_or_id)

        requirement_name = stored.name
        stored_requirement_id = newest.stored_requirement_id

        records, in_latest_run = _version_records(
            session, to_read, latest_run_id=newest.id
        )

    changes = tuple(
        sorted(
            (
                ChangeRecord(
                    change_type=net.change_type,
                    version=records.get(net.dataset_version_id)
                    or _missing_version_record(net, newest.id),
                    detail=net.detail,
                    first_run_id=net.first_run_id,
                    last_run_id=net.last_run_id,
                )
                for net in net_changes
            ),
            key=lambda change: (
                _CHANGE_TYPE_ORDER[change.change_type],
                _sort_key(change.version),
            ),
        )
    )

    current = tuple(
        sorted((records[version_id] for version_id in in_latest_run), key=_sort_key)
    )

    return SearchSummary(
        requirement_name=requirement_name,
        stored_requirement_id=stored_requirement_id,
        run=RunHealth(
            run_id=newest.id,
            started_at=newest.started_at,
            finished_at=newest.finished_at,
            complete=newest.complete,
            leaves=get_run_leaves(engine, newest.id),
        ),
        baseline_run_id=baseline_run_id,
        window_runs=window_ids,
        changes=changes,
        churned=churned,
        current=current,
    )
