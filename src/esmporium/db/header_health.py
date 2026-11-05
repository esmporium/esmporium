"""
Recording how the ESGF data nodes behave when asked for a header, and ranking them by it

The twin of [esmporium.db.download_health][], and separate from it for the reason
[HeaderReadCallRecord][esmporium.db.schema.HeaderReadCallRecord] gives: pooling a
64 KiB read with a whole-file transfer would drag every node's measured throughput down
to the speed of its handshake.

So the two differ in what they rank on, and that difference is the point. A download
wants the node that moves bytes fastest. A header read moves almost no bytes, and is
almost entirely connection setup plus one round trip -- so what decides whether a node
is a good one to ask is **latency**, and the summary here measures that instead.

The other thing carried over from the prototype is how a node gets written off. It
evicted a node after three consecutive failures, which killed one that was serving 228
of 244 reads successfully -- 93% healthy -- and stranded 214 simulations, because work
is grouped by model and so one model with a gap on one mirror produced a run of
consecutive failures there. [unreliable_data_nodes][(m).unreliable_data_nodes] therefore
judges a **rate over a fair sample**, never a streak.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from dataclasses import dataclass
from statistics import median
from typing import TYPE_CHECKING, Any

from sqlmodel import Session, col, select

from esmporium.datasets import HeaderReadOutcome
from esmporium.db.download_health import (
    DEFAULT_HEALTH_WINDOW,
    data_node_id,
)
from esmporium.db.schema import DataNode, HeaderReadCallRecord
from esmporium.downloads import (
    DEFAULT_DATA_NODE_SELECTOR,
    prepare_routes,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from sqlalchemy import Engine

    from esmporium.downloads import DataNodeSelector, Route
    from esmporium.headers import HeaderReadCall, HeaderReadCallObserver

DEFAULT_MIN_CALLS_TO_JUDGE = 6
"""
How many reads a node must have had before [unreliable_data_nodes][(m).] will write it off

A fair sample, so that a short run of failures cannot condemn a working node. Six is
the prototype's figure, arrived at after a streak-based rule evicted a node that was
93% healthy; it is enough that a node failing every time is dropped quickly and that
one failing occasionally is not.
"""  # noqa: E501

DEFAULT_MAX_SUCCESS_RATE_TO_JUDGE = 0.2
"""
How badly a node has to be doing before it is written off

Kept low on purpose: a node succeeding even a fifth of the time is worth keeping,
because its failures only send their files to another mirror while its successes are
free. This is for nodes that are effectively dead, not for nodes that are merely
slower than the best.
"""


def _as_record(session: Session, call: HeaderReadCall) -> HeaderReadCallRecord:
    """
    Turn one observed attempt into a row, resolving its data node

    Parameters
    ----------
    session
        The session to resolve the data node in

    call
        The attempt

    Returns
    -------
    :
        The row, not yet added to the session
    """
    return HeaderReadCallRecord(
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
        bytes_requested=call.bytes_requested,
        bytes_transferred=call.bytes_transferred,
        seconds=call.seconds,
        attempt_number=call.attempt_number,
        range_honoured=call.range_honoured,
    )


def write_header_read_calls(
    session: Session, calls: Iterable[HeaderReadCall]
) -> tuple[HeaderReadCallRecord, ...]:
    """
    Write some observed attempts through a session the caller already has

    **The one to use when the reads happen inside somebody else's transaction**, which
    is the case that matters: a parent walk holds a session and calls its facts
    provider from inside it, and
    [record_header_read_calls][(m).record_header_read_calls] would block against that
    for five minutes per attempt. See that function for why.

    Does not commit: the caller owns the transaction boundary, as everywhere else in
    this layer.

    Parameters
    ----------
    session
        The session to write through

    calls
        The attempts to record

    Returns
    -------
    :
        The rows written, flushed
    """
    rows = [_as_record(session, call) for call in calls]
    for row in rows:
        session.add(row)
    session.flush()

    return tuple(rows)


def record_header_read_calls(engine: Engine) -> HeaderReadCallObserver:
    """
    Build an observer which records each header-read attempt as it happens

    **Only safe when nothing else is holding a transaction on `engine`.** On SQLite --
    the backend this is built for --
    [configure_sqlite_for_concurrency][esmporium.db.configure_sqlite_for_concurrency]
    issues `BEGIN IMMEDIATE`, so a session takes the database's single write lock the
    moment its transaction starts, and holds it until it commits. A second connection
    that tries to write then waits for the `busy_timeout`, which that function sets to
    five minutes. So an observer opening its own session *inside* a transaction does
    not race -- it stops, for five minutes, per attempt.

    That is exactly the situation a parent walk is in: it holds a session and calls its
    facts provider from inside it. So the provider buffers its attempts and writes them
    through the walk's own session with
    [write_header_read_calls][(m).write_header_read_calls] instead, which costs the
    "health survives a walk that later fails" property and buys never hanging. The
    walk commits per hop, so the attempts are still saved as the run goes.

    Use this one for reads driven from outside a transaction -- a standalone
    [read_headers][esmporium.headers.read_headers], a script, a demo.

    Parameters
    ----------
    engine
        The database to write to

    Returns
    -------
    :
        An observer which writes and commits one row per attempt it is told about

        An attempt against a data node we have never seen creates its
        [DataNode][esmporium.db.schema.DataNode] row, through the same
        [data_node_id][esmporium.db.download_health.data_node_id] the download
        recorder uses -- so a node's header reads and its downloads point at one row
        and can be compared.

        Opens and commits its own session per call, which is what makes it safe to
        hand to a run reading several headers at once, and what makes a run that is
        killed keep the reads it had already done.
    """

    def observer(call: HeaderReadCall) -> None:
        # This function needs to be thread safe to handle parallelism, which is why it
        # opens its own session rather than closing over one: a `Session` must not be
        # shared between threads.
        with Session(engine) as session:
            session.add(_as_record(session, call))
            session.commit()

    return observer


@dataclass(frozen=True)
class HeaderReadHealth:
    """
    A summary of one data node's header-read health

    Built from the
    [HeaderReadCallRecord][esmporium.db.schema.HeaderReadCallRecord] rows recorded for
    every attempt.
    """

    data_node: str
    """The data node these numbers are for, e.g. `esgf.nci.org.au`"""

    n_calls: int
    """How many attempts against this node were recorded, of any outcome"""

    n_success: int
    """How many of those attempts yielded a header"""

    n_judged: int
    """
    How many attempts count towards [failure_rate][(c).failure_rate]

    Excludes the outcomes which say nothing about the node: a 404, a 401, and the two
    that are facts about the *file* rather than the node -- a header that needed more
    bytes than we asked for, and a format we cannot read. See
    [HeaderReadOutcome.counts_against_data_node][esmporium.datasets.HeaderReadOutcome.counts_against_data_node].
    """

    failure_rate: float
    """
    The fraction of judged attempts that failed

    `0.0` when there is nothing to judge, which reads as "no evidence against it"
    rather than "perfect" -- a node with no record should not be penalised.
    """

    success_rate: float
    """
    The fraction of *all* attempts that yielded a header

    Over `n_calls` rather than `n_judged`, unlike
    [failure_rate][(c).failure_rate], because this is what
    [unreliable_data_nodes][(m).unreliable_data_nodes] writes a node off on and the
    question there is the blunt one: how often does asking this node actually work.

    `0.0` when nothing was tried.
    """

    median_seconds: float | None
    """
    The median time a successful read took

    **What a node should be ranked on here**, where a download ranks on throughput.
    Reading a few kilobytes is almost all connection setup and one round trip, so this
    is latency.

    Median rather than mean, so one unlucky read does not decide a node's reputation.
    `None` if nothing succeeded.
    """

    n_blocked: int
    """How many attempts were refused (403, 429 or 503)"""

    n_timeout: int
    """How many attempts connected and then stalled"""

    n_host_fault: int
    """How many attempts never reached the node at all"""

    n_unreadable: int
    """
    How many attempts served bytes that did not parse

    Worth its own number however rare, because it is the only one that says the node
    served *wrong* data rather than no data -- and the only parse failure another
    mirror might fix.
    """

    n_missing: int
    """
    How many attempts were told there is nothing there (404, 410 or 416)

    Counted but excluded from [failure_rate][(c).failure_rate]: a file going away is
    not the node misbehaving. Useful on its own, though -- a node answering 404 for
    everything has stale published routes.
    """

    n_range_ignored: int
    """
    How many *successful* reads were answered with the whole file instead of a range

    The one number here with no counterpart in download health. Such a node still
    gives us a usable header, so it is not a failure -- but it made us open a
    connection it began pouring a file down, and a CMIP5 file can be most of a
    gigabyte. A node with a high count here is one to ask last even if it is fast.
    """


def aggregate_header_read_health(
    engine: Engine,
    data_nodes: Iterable[str] | None = None,
    *,
    since: datetime.timedelta | datetime.datetime | None = DEFAULT_HEALTH_WINDOW,
) -> dict[str, HeaderReadHealth]:
    """
    Combine the recorded attempts into one [HeaderReadHealth][(m).] per data node

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
        The database to read from

    data_nodes
        Only aggregate these data nodes, i.e. the pool the caller cares about

        If not given, every data node in the database is returned. An *empty* iterable
        returns nothing, not everything.

    since
        How far back to look

        A `timedelta` is taken as "this long ago", a `datetime` as an absolute
        cut-off, and `None` means all of history.

    Returns
    -------
    :
        A [HeaderReadHealth][(m).] per data node with at least one recorded attempt in
        the window, keyed by the node's name

        A node with no attempts in the window is absent rather than
        present-and-empty, so "we have nothing to go on" stays distinguishable from
        "we tried and it failed".
    """
    statement = select(HeaderReadCallRecord, DataNode.data_node).join(
        DataNode, col(HeaderReadCallRecord.data_node_id) == col(DataNode.id)
    )

    if data_nodes is not None:
        wanted = list(data_nodes)
        # An empty pool means nothing, not everything.
        if not wanted:
            return {}

        statement = statement.where(col(DataNode.data_node).in_(wanted))

    cutoff = _cutoff(since)
    if cutoff is not None:
        # Filtered in SQL rather than in Python, for the reason
        # `aggregate_data_node_health` gives: `created_at` is stored timezone-aware
        # but SQLite hands it back naive, so comparing it in Python raises.
        statement = statement.where(col(HeaderReadCallRecord.created_at) >= cutoff)

    with Session(engine) as session:
        rows = session.exec(statement).all()

    by_node: dict[str, list[HeaderReadCallRecord]] = {}
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
        return datetime.datetime.now(datetime.timezone.utc) - since

    return since


def _summarise(
    data_node: str, records: Sequence[HeaderReadCallRecord]
) -> HeaderReadHealth:
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

    def count(outcome: HeaderReadOutcome) -> int:
        return sum(1 for record in records if record.outcome == outcome)

    successes = [
        record for record in records if record.outcome == HeaderReadOutcome.SUCCESS
    ]
    # A row read back from the database carries a plain `str`, so the member has to be
    # rebuilt to reach the behaviour on it.
    #
    # A success belongs in here as well, because this is the *denominator* of a
    # failure rate. `counts_against_data_node` answers the narrower question "is this
    # a failure worth charging to the node", and excludes `SUCCESS`.
    judged = [
        record
        for record in records
        if record.outcome == HeaderReadOutcome.SUCCESS
        or HeaderReadOutcome(record.outcome).counts_against_data_node
    ]
    n_failed = sum(
        1 for record in judged if record.outcome != HeaderReadOutcome.SUCCESS
    )

    latencies = [record.seconds for record in successes if record.seconds > 0]

    return HeaderReadHealth(
        data_node=data_node,
        n_calls=len(records),
        n_success=len(successes),
        n_judged=len(judged),
        failure_rate=n_failed / len(judged) if judged else 0.0,
        success_rate=len(successes) / len(records) if records else 0.0,
        median_seconds=median(latencies) if latencies else None,
        n_blocked=count(HeaderReadOutcome.BLOCKED),
        n_timeout=count(HeaderReadOutcome.TIMEOUT),
        n_host_fault=count(HeaderReadOutcome.HOST_FAULT),
        n_unreadable=count(HeaderReadOutcome.UNREADABLE_BYTES),
        n_missing=count(HeaderReadOutcome.MISSING),
        n_range_ignored=sum(1 for record in successes if not record.range_honoured),
    )


HeaderReadRanker = Callable[[HeaderReadHealth], Any]
"""
Turns one data node's header-read health into a sort key

Should return keys such that sorting nodes ascending puts the best first.
"""


def rank_by_reliability_then_latency(
    health: HeaderReadHealth,
) -> tuple[float, float]:
    """
    Rank a data node on whether it works first, and how quickly it answers second

    **The default**, and deliberately not latency alone. A node that answers in 200 ms
    one time in five is worse than one that answers in two seconds every time, because
    the cost of a failure is not the failed request -- it is the wait before another
    mirror is tried.

    Parameters
    ----------
    health
        The node's health

    Returns
    -------
    :
        `(failure_rate, median_seconds)`, so fewer failures wins and a quicker answer
        breaks the tie

        A node with no successful read at all gets `inf` for latency, which sorts it
        last among nodes with the same failure rate -- it has given us no evidence
        that it can answer.
    """
    latency = (
        health.median_seconds if health.median_seconds is not None else float("inf")
    )

    return (health.failure_rate, latency)


def rank_by_latency(health: HeaderReadHealth) -> float:
    """
    Rank a data node on how quickly it answers, ignoring whether it usually does

    Prefer [rank_by_reliability_then_latency][(m).] unless you have a reason.

    Parameters
    ----------
    health
        The node's health

    Returns
    -------
    :
        The median seconds a successful read took, or `inf` if none has
    """
    if health.median_seconds is None:
        return float("inf")

    return health.median_seconds


def unreliable_data_nodes(
    health: Mapping[str, HeaderReadHealth],
    *,
    min_calls: int = DEFAULT_MIN_CALLS_TO_JUDGE,
    max_success_rate: float = DEFAULT_MAX_SUCCESS_RATE_TO_JUDGE,
) -> frozenset[str]:
    """
    Name the data nodes which are failing often enough to be worth skipping

    **Judged on a rate over a fair sample, never on a streak**, and that is the whole
    design. The prototype evicted a node after three consecutive failures and so
    dropped one that was succeeding 228 times out of 244, because reads are grouped by
    model and a single model with a gap on one mirror makes its failures consecutive.
    Dropping that node stranded 214 simulations whose files it was the best source for.

    Parameters
    ----------
    health
        What we know, keyed by data node. See [aggregate_header_read_health][(m).].

    min_calls
        How many attempts a node must have had before it can be written off

    max_success_rate
        How low a node's success rate has to be to be written off

    Returns
    -------
    :
        The nodes worth skipping

        A node with fewer than `min_calls` attempts is **never** named, however badly
        it has done: with no sample there is no evidence, and a node excluded on two
        bad reads is a node never given the chance to prove otherwise.

    Examples
    --------
    >>> dead = HeaderReadHealth(
    ...     data_node="dead.example.org",
    ...     n_calls=12,
    ...     n_success=0,
    ...     n_judged=12,
    ...     failure_rate=1.0,
    ...     success_rate=0.0,
    ...     median_seconds=None,
    ...     n_blocked=0,
    ...     n_timeout=12,
    ...     n_host_fault=0,
    ...     n_unreadable=0,
    ...     n_missing=0,
    ...     n_range_ignored=0,
    ... )
    >>> busy = HeaderReadHealth(
    ...     data_node="busy.example.org",
    ...     n_calls=244,
    ...     n_success=228,
    ...     n_judged=244,
    ...     failure_rate=16 / 244,
    ...     success_rate=228 / 244,
    ...     median_seconds=1.1,
    ...     n_blocked=16,
    ...     n_timeout=0,
    ...     n_host_fault=0,
    ...     n_unreadable=0,
    ...     n_missing=0,
    ...     n_range_ignored=0,
    ... )
    >>> sorted(unreliable_data_nodes({"dead": dead, "busy": busy}))
    ['dead.example.org']
    """
    return frozenset(
        each.data_node
        for each in health.values()
        if each.n_calls >= min_calls and each.success_rate <= max_success_rate
    )


def build_header_read_health_selector(
    engine: Engine,
    *,
    ranker: HeaderReadRanker = rank_by_reliability_then_latency,
    since: datetime.timedelta | datetime.datetime | None = DEFAULT_HEALTH_WINDOW,
    fallback: DataNodeSelector = DEFAULT_DATA_NODE_SELECTOR,
    skip_unreliable: bool = True,
) -> DataNodeSelector:
    """
    Build a selector that orders a file's routes by their nodes' header-read health

    Health is read **once**, when the selector is built, which keeps it cheap and
    makes a run deterministic.

    Parameters
    ----------
    engine
        The database to read health from

    ranker
        How to rank nodes we have health for. See [HeaderReadRanker][(m).].

    since
        How far back to look. See [aggregate_header_read_health][(m).].

    fallback
        The selector to defer to for a file none of whose nodes we know anything about

    skip_unreliable
        Whether to put the nodes [unreliable_data_nodes][(m).] names last

        Last rather than removed, deliberately. A node that fails four reads in five
        still holds files that no healthier node has, and dropping it outright would
        turn "slow to get" into "impossible to get". The ordering already means it is
        only reached when nothing better has the file.

    Returns
    -------
    :
        A health-based selector, falling back where there is no information
    """
    health = aggregate_header_read_health(engine, since=since)
    unreliable = unreliable_data_nodes(health) if skip_unreliable else frozenset()

    def select(candidates: Sequence[Route], attempt: int) -> Route | None:
        prepared = prepare_routes(candidates)
        known = [route for route in prepared if route.data_node in health]
        if not known:
            return fallback(candidates, attempt)

        unknown = [route for route in prepared if route.data_node not in health]
        # `sorted` is stable, so routes tying on the key keep their given order. The
        # unreliable flag leads the key, so a node being written off outranks every
        # other consideration without removing it from the list.
        ranked = sorted(
            known,
            key=lambda route: (
                route.data_node in unreliable,
                ranker(health[route.data_node]),
            ),
        )
        ordered = ranked + unknown

        return ordered[attempt] if attempt < len(ordered) else None

    return select
