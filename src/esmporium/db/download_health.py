"""
Recording ESGF data node health into the database, and ranking nodes by it
"""

from __future__ import annotations

import datetime
import logging
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from statistics import median
from typing import TYPE_CHECKING, Any

from sqlmodel import Session, col, select

from esmporium.datasets import DownloadOutcome
from esmporium.db.results_to_database import flushed_id
from esmporium.db.schema import DataNode, DataNodeCallRecord
from esmporium.downloads import DEFAULT_DATA_NODE_SELECTOR, DataNodeSelector, Route
from esmporium.downloads.selectors import prepare_routes

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from esmporium.downloads import DataNodeCall, DataNodeCallObserver

logger = logging.getLogger(__name__)

DEFAULT_REFRESH_AFTER = datetime.timedelta(hours=3)
"""
How often a refreshing selector re-reads health by default

Three hours is a judgement, not a measurement: long enough that a multi-day download
does not spend its time querying, short enough that a node going bad in the morning is
noticed before the evening. Pass your own if your downloads are shorter or your nodes
flakier.
"""

DEFAULT_HEALTH_WINDOW = datetime.timedelta(days=7)
"""
How far back [aggregate_data_node_health][(m).] looks by default

A week, so that a node's recent behaviour decides its rank and a bad patch is
eventually forgiven. `None` means all of history, which is available but is not the
default for the reason in this module's docstring.
"""

Clock = Callable[[], datetime.datetime]
"""
Tells the time, as a timezone-aware UTC datetime

An injection seam purely so that
[build_refreshing_data_node_health_selector][(m).] can be tested without waiting three
hours.
"""


def _utcnow() -> datetime.datetime:
    """
    Get the current time, as a timezone-aware UTC datetime

    Returns
    -------
    :
        Now, in UTC
    """
    return datetime.datetime.now(datetime.timezone.utc)


def record_data_node_calls(engine: Engine) -> DataNodeCallObserver:
    """
    Build an observer which records each data node fetch attempt into the database

    Parameters
    ----------
    engine
        The database engine to record into.

    Returns
    -------
    :
        An observer which writes one row per attempt it is told about

        An attempt against a data node we have never seen creates its
        [DataNode][esmporium.db.schema.DataNode] row. In practice there is nothing to
        create -- a route we fetched came from a `DataAccess` row, which already has
        one -- but a derived route (an `https://` twin) can name a node reached by a
        path we did not store, and dropping the observation would be worse than
        inserting a row.
    """

    def observer(call: DataNodeCall) -> None:
        # This function needs to be thread safe to handle parallelism.
        with Session(engine) as session:
            session.add(
                DataNodeCallRecord(
                    data_node_id=data_node_id(session, call.data_node),
                    file_id=call.file_id,
                    data_access_id=call.data_access_id,
                    url=call.url,
                    http_method=call.http_method,
                    response_code=call.response_code,
                    outcome=call.outcome,
                    error=(
                        f"{type(call.error).__name__}: {call.error}"
                        if call.error is not None
                        else None
                    ),
                    bytes_transferred=call.bytes_transferred,
                    seconds=call.seconds,
                    attempt_number=call.attempt_number,
                    resumed=call.resumed,
                )
            )
            session.commit()

    return observer


def data_node_id(session: Session, data_node: str) -> int:
    """
    Get the id of a data node's row, creating the row if it is new

    Public, and shared with [esmporium.db.header_health][], because a
    [DataNode][esmporium.db.schema.DataNode] row is the one thing the two kinds of
    health genuinely have in common: whether a node answers at all is the same
    question whether we wanted a whole file or four kilobytes of one. Their *records*
    are deliberately separate tables -- see
    [HeaderReadCallRecord][esmporium.db.schema.HeaderReadCallRecord] -- but the node
    they both point at must be the same row, or the two could never be compared.

    Parameters
    ----------
    session
        The session to look in

    data_node
        The data node's published name

    Returns
    -------
    :
        The row's surrogate id
    """
    found = session.exec(
        select(DataNode).where(col(DataNode.data_node) == data_node)
    ).first()
    if found is not None and found.id is not None:
        return found.id

    row = DataNode(data_node=data_node)
    session.add(row)
    session.flush()

    return flushed_id(row)


@dataclass(frozen=True)
class DataNodeHealth:
    """
    A summary of one data node's download health

    Built from the [DataNodeCallRecord][esmporium.db.schema.DataNodeCallRecord] rows
    recorded for every fetch attempt.
    """

    data_node: str
    """The data node these numbers are for, e.g. `esgf.nci.org.au`"""

    n_calls: int
    """How many attempts against this node were recorded, of any outcome"""

    n_success: int
    """How many of those attempts got the bytes"""

    n_judged: int
    """
    How many attempts count towards [failure_rate][(c).failure_rate]

    Excludes the outcomes which say nothing about the node -- a 404 or a 401 -- so this
    is usually, but not always, `n_calls`. See
    [DownloadOutcome.counts_against_data_node][esmporium.datasets.DownloadOutcome.counts_against_data_node].
    """

    failure_rate: float
    """
    The fraction of judged attempts that failed

    `0.0` when there is nothing to judge, which reads as "no evidence against it"
    rather than "perfect". That is the right way round: a node with no record should
    not be *penalised*, and `_rank_pool` handles "no evidence at all" separately
    anyway.
    """

    median_throughput_mb_per_s: float | None
    """
    The median throughput over the successful attempts, in megabytes per second

    `None` if nothing succeeded. Median rather than mean, so that one enormous file or
    one tiny one does not decide a node's reputation.
    """

    total_bytes: int
    """How many bytes this node has successfully served us"""

    n_blocked: int
    """How many attempts were refused (403, 429 or 503)"""

    n_timeout: int
    """How many attempts stalled or never connected in time"""

    n_host_fault: int
    """How many attempts never reached the node at all"""

    n_checksum_failed: int
    """
    How many attempts delivered bytes that failed their checksum

    Worth its own number however rare, because it is the only one that says the node
    served *wrong* data rather than no data.
    """

    n_missing: int
    """
    How many attempts were told there is nothing there (404 or 410)

    Counted and reported, but excluded from [failure_rate][(c).failure_rate]: a route
    going away is not the node misbehaving. Useful on its own, though -- a node
    answering 404 for everything is one whose published routes have gone stale.
    """

    n_needs_credentials: int
    """
    How many attempts were refused for want of credentials (401)

    Also excluded from [failure_rate][(c).failure_rate]. A high number here means the
    data needs an authenticated client, not that the node is bad.
    """


def aggregate_data_node_health(
    engine: Engine,
    data_nodes: Iterable[str] | None = None,
    *,
    since: datetime.timedelta | datetime.datetime | None = DEFAULT_HEALTH_WINDOW,
) -> dict[str, DataNodeHealth]:
    """
    Combine the recorded attempts into one [DataNodeHealth][(m).] per data node

    **Call this with no session of your own open on `engine`.** It opens one, and on
    SQLite a session takes the database's single write lock the moment its transaction
    begins -- see
    [configure_sqlite_for_concurrency][esmporium.db.configure_sqlite_for_concurrency],
    which issues `BEGIN IMMEDIATE` -- so asking for this from inside another waits on
    that session's lock rather than reading past it. Gather what you need from your
    session, close it, then ask.

    Parameters
    ----------
    engine
        The database to read from.

    data_nodes
        Only aggregate these data nodes (the pool the caller cares about).

        If not provided, we return information for every data node in the database.
        An *empty* iterable returns nothing, not everything.

    since
        How far back to look.

        A `timedelta` is taken as "this long ago"; a `datetime` as an absolute cut-off;
        `None` means all of history.

        Defaults to [DEFAULT_HEALTH_WINDOW][(m).].

    Returns
    -------
    :
        A [DataNodeHealth][(m).] per data node with at least one recorded attempt in
        the window, keyed by the node's name

        A node with no attempts in the window is absent rather than present-and-empty,
        so "we have nothing to go on" stays distinguishable from "we tried and it
        failed".
    """
    statement = select(DataNodeCallRecord, DataNode.data_node).join(
        DataNode, col(DataNodeCallRecord.data_node_id) == col(DataNode.id)
    )

    if data_nodes is not None:
        wanted = list(data_nodes)
        # Make sure that if we get empty data nodes, we return nothing, not
        # everything.
        if not wanted:
            return {}

        statement = statement.where(col(DataNode.data_node).in_(wanted))

    cutoff = _cutoff(since)
    if cutoff is not None:
        # Filtered in SQL rather than in Python on purpose. `created_at` is stored
        # timezone-aware but SQLite hands it back naive, so comparing it to an aware
        # `cutoff` in Python raises TypeError. Binding the parameter lets SQLAlchemy
        # serialise both sides the same way.
        statement = statement.where(col(DataNodeCallRecord.created_at) >= cutoff)

    with Session(engine) as session:
        rows = session.exec(statement).all()

    by_node: dict[str, list[DataNodeCallRecord]] = {}
    for record, data_node in rows:
        by_node.setdefault(data_node, []).append(record)

    return {
        data_node: _summarise(data_node, records)
        for data_node, records in by_node.items()
    }


def _cutoff(
    since: datetime.timedelta | datetime.datetime | None,
) -> datetime.datetime | None:
    """
    Turn a `since` argument into an absolute cut-off

    Parameters
    ----------
    since
        How far back to look: a `timedelta`, an absolute `datetime`, or `None`

    Returns
    -------
    :
        The cut-off, or `None` to look at all of history
    """
    if since is None:
        return None

    if isinstance(since, datetime.timedelta):
        return _utcnow() - since

    return since


def _summarise(data_node: str, records: Sequence[DataNodeCallRecord]) -> DataNodeHealth:
    """
    Summarise one data node's recorded attempts

    Parameters
    ----------
    data_node
        The node's name

    records
        Its recorded attempts

    Returns
    -------
    :
        The summary
    """

    def count(outcome: DownloadOutcome) -> int:
        return sum(1 for record in records if record.outcome == outcome)

    successes = [
        record for record in records if record.outcome == DownloadOutcome.SUCCESS
    ]
    # Compare on the enum's *value*: a row read back from the database carries a plain
    # `str`, so `DownloadOutcome(record.outcome)` is what gets us the member back.
    #
    # A success has to be in here too, because this is the *denominator* of a failure
    # rate. `counts_against_data_node` answers a narrower question -- "is this a
    # failure worth charging to the node" -- and it excludes `SUCCESS`, so using it
    # alone made every node with a single counting failure report a failure rate of
    # 1.0: `n_failed` was then the whole of `judged`. A node succeeding nine times in
    # ten came out indistinguishable from a dead one, which is exactly the
    # "retire a working replica" outcome the ranking is supposed to avoid.
    judged = [
        record
        for record in records
        if record.outcome == DownloadOutcome.SUCCESS
        or DownloadOutcome(record.outcome).counts_against_data_node
    ]
    n_failed = sum(1 for record in judged if record.outcome != DownloadOutcome.SUCCESS)

    throughputs = [
        record.bytes_transferred / 1_000_000 / record.seconds
        for record in successes
        if record.seconds > 0 and record.bytes_transferred > 0
    ]

    return DataNodeHealth(
        data_node=data_node,
        n_calls=len(records),
        n_success=len(successes),
        n_judged=len(judged),
        failure_rate=n_failed / len(judged) if judged else 0.0,
        median_throughput_mb_per_s=median(throughputs) if throughputs else None,
        total_bytes=sum(record.bytes_transferred for record in successes),
        n_blocked=count(DownloadOutcome.BLOCKED),
        n_timeout=count(DownloadOutcome.TIMEOUT),
        n_host_fault=count(DownloadOutcome.HOST_FAULT),
        n_checksum_failed=count(DownloadOutcome.CHECKSUM_FAILED),
        n_missing=count(DownloadOutcome.MISSING),
        n_needs_credentials=count(DownloadOutcome.NEEDS_CREDENTIALS),
    )


DataNodeRanker = Callable[[DataNodeHealth], Any]
"""
Turns one data node's health into a sort key

This should return keys such that, when nodes are sorted ascending, the best is first.
"""


def rank_by_reliability_then_throughput(
    health: DataNodeHealth,
) -> tuple[float, float]:
    """
    Rank a data node on whether it works first, and how fast it is second

    **The default**, and deliberately not throughput alone. A node that serves at
    80 MB/s one time in five is worse than one that serves at 20 MB/s every time,
    because every failure costs a timeout and a retry before we find out. Search ranks
    on speed alone, which is the right call there -- a failed search costs a second.

    Parameters
    ----------
    health
        The node's health

    Returns
    -------
    :
        `(failure_rate, -throughput)`, so low failure wins and speed breaks ties

        A node with no successful transfer gets `-0.0` for throughput, which sorts it
        behind every node that has actually moved bytes at the same failure rate.
    """
    throughput = health.median_throughput_mb_per_s or 0.0

    return (health.failure_rate, -throughput)


def rank_by_throughput(health: DataNodeHealth) -> float:
    """
    Rank a data node on how fast it has served us, ignoring whether it works

    Here because it is occasionally what you want -- filling a big archive off nodes
    you already know are reliable -- and because it is the direct analogue of
    [get_median_response_time_for_ranking][esmporium.db.get_median_response_time_for_ranking].
    Prefer [rank_by_reliability_then_throughput][(m).] unless you have a reason.

    Parameters
    ----------
    health
        The node's health

    Returns
    -------
    :
        Negated throughput, so that faster sorts first

        A node with no successful transfer gets `math.inf`, so it sorts last rather
        than appearing to be infinitely fast.
    """
    if health.median_throughput_mb_per_s is None:
        return math.inf

    return -health.median_throughput_mb_per_s


def _rank_pool(
    routes: Sequence[Route],
    health: Mapping[str, DataNodeHealth],
    ranker: DataNodeRanker,
) -> tuple[Route, ...] | None:
    """
    Reorder a file's routes by their data nodes' health, or report there is none

    Parameters
    ----------
    routes
        The routes to order

    health
        What we know, keyed by data node

    ranker
        How to turn a node's health into a sort key

    Returns
    -------
    :
        The routes, best first, or `None` when *no* route's node has any health at all

        `None` is the signal to fall back to another selector rather than invent a
        ranking from nothing.
    """
    have_data = [route for route in routes if route.data_node in health]
    if not have_data:
        return None

    no_data = [route for route in routes if route.data_node not in health]
    # `sorted` is stable, so routes that tie on the key keep their given order.
    ranked = sorted(have_data, key=lambda route: ranker(health[route.data_node]))

    return tuple(ranked + no_data)


def build_data_node_health_selector(
    engine: Engine,
    *,
    ranker: DataNodeRanker = rank_by_reliability_then_throughput,
    since: datetime.timedelta | datetime.datetime | None = DEFAULT_HEALTH_WINDOW,
    fallback: DataNodeSelector = DEFAULT_DATA_NODE_SELECTOR,
) -> DataNodeSelector:
    """
    Build a selector that orders a file's routes by their data nodes' health

    The health is read **once**, when the selector is built, which keeps it cheap and
    makes a run deterministic. That is the right trade for a download of minutes; for
    one of days, use [build_refreshing_data_node_health_selector][(m).] instead.

    Parameters
    ----------
    engine
        The database to read health from.

    ranker
        How to rank data nodes we have health for. See [DataNodeRanker][(m).].

    since
        How far back to look. See [aggregate_data_node_health][(m).].

    fallback
        The selector to defer to for a file none of whose data nodes we know anything
        about.

    Returns
    -------
    :
        A health-based selector, falling back where there is no information
    """
    health = aggregate_data_node_health(engine, since=since)

    return _selector_from(health, ranker=ranker, fallback=fallback)


def _selector_from(
    health: Mapping[str, DataNodeHealth],
    *,
    ranker: DataNodeRanker,
    fallback: DataNodeSelector,
) -> DataNodeSelector:
    """
    Build a selector over an already-read snapshot of health

    Shared by the plain and the refreshing selector, so the two cannot drift in how
    they apply a snapshot -- only in when they take one.

    Parameters
    ----------
    health
        What we know, keyed by data node

    ranker
        How to rank nodes we have health for

    fallback
        The selector to defer to when nothing is known

    Returns
    -------
    :
        The selector
    """

    def select(candidates: Sequence[Route], attempt: int) -> Route | None:
        prepared = prepare_routes(candidates)
        ranked = _rank_pool(prepared, health, ranker)
        if ranked is None:
            return fallback(candidates, attempt)

        return ranked[attempt] if attempt < len(ranked) else None

    return select


def build_refreshing_data_node_health_selector(  # noqa: PLR0913 - one parameter per injection seam
    engine: Engine,
    *,
    refresh_after: datetime.timedelta = DEFAULT_REFRESH_AFTER,
    ranker: DataNodeRanker = rank_by_reliability_then_throughput,
    since: datetime.timedelta | datetime.datetime | None = DEFAULT_HEALTH_WINDOW,
    fallback: DataNodeSelector = DEFAULT_DATA_NODE_SELECTOR,
    clock: Clock = _utcnow,
) -> DataNodeSelector:
    """
    Build a selector which re-reads data node health as a download goes on

    Parameters
    ----------
    engine
        The database to read health from.

    refresh_after
        How long a snapshot is used before another is taken.
        Defaults to [DEFAULT_REFRESH_AFTER][(m).].

    ranker
        How to rank data nodes we have health for. See [DataNodeRanker][(m).].

    since
        How far back each read looks. See [aggregate_data_node_health][(m).].

        Note this is a *window*, not an origin: each refresh looks back `since` from
        the moment it runs, so what the selector knows moves forward with the
        download.

    fallback
        The selector to defer to for a file none of whose data nodes we know anything
        about.

    clock
        How to tell the time. See [Clock][(m).]; the default is the real one.

        This exists so the refresh can be tested in milliseconds rather than hours.

    Returns
    -------
    :
        A selector which keeps its health up to date

        Not thread safe by construction: when downloads run in parallel, two workers
        can decide to refresh at once and both will read. That wastes a query and is
        otherwise harmless -- they read the same rows and arrive at the same ordering
        -- so it is not worth a lock on the hot path.
    """
    health: dict[str, DataNodeHealth] = {}
    read_at: datetime.datetime | None = None

    def current() -> Mapping[str, DataNodeHealth]:
        nonlocal health, read_at

        now = clock()
        if read_at is None or now - read_at >= refresh_after:
            health = aggregate_data_node_health(engine, since=since)
            read_at = now
            logger.debug(
                "re-read data node health: %d node(s) known",
                len(health),
                extra={"n_data_nodes": len(health), "refresh_after": refresh_after},
            )

        return health

    def select(candidates: Sequence[Route], attempt: int) -> Route | None:
        snapshot = current()
        prepared = prepare_routes(candidates)
        ranked = _rank_pool(prepared, snapshot, ranker)
        if ranked is None:
            return fallback(candidates, attempt)

        return ranked[attempt] if attempt < len(ranked) else None

    return select
