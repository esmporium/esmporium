"""
Recording search API health into the database

Additional opt-in host-ranking selector, which uses the recorded
search API health information to rank hosts for future requests.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from statistics import median
from typing import TYPE_CHECKING, Any

from sqlmodel import Session, col, select

from esmporium.db.schema import SearchAPICallRecord
from esmporium.search.search_api import (
    CMIP5_APIS,
    CMIP6_APIS,
    CMIP7_APIS,
    DEFAULT_SELECTOR,
    SearchAPI,
    SearchAPISelector,
)

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from esmporium.query import QueryCanonical
    from esmporium.search.health import SearchAPICall, SearchAPICallObserver


def record_search_api_calls(engine: Engine) -> SearchAPICallObserver:
    """
    Build an observer which records each search API call record into database

    A fresh session is opened per call, so the observer is safe to call from
    several threads at once. Each call is committed on its own, so a run killed
    part way through keeps the records of the calls it had already made.

    Parameters
    ----------
    engine
        The database engine to record into.
        The caller owns it.

    Returns
    -------
    :
        An observer which writes one row per call it is told about
    """

    def observer(call: SearchAPICall) -> None:
        # thread safe to handle parallelism
        with Session(engine) as session:
            session.add(SearchAPICallRecord.from_call(call))
            session.commit()

    return observer


@dataclass(frozen=True)
class HostHealth:
    """
    A summary of one host (index node's) health

    This summary is based on the health information recorded from every
    single search API call (recorded as a row to the database).
    """

    host: str
    """The host these numbers are for, e.g. `esgf.nci.org.au`"""

    n_calls: int
    """How many calls to this host were recorded (every attempt, success or not)"""

    n_success: int
    """How many of those calls succeeded"""

    success_rate: float
    """`n_success / n_calls`, or `0.0` if there were somehow no calls"""

    median_response_time_seconds: float | None
    """
    Median response time for this host, in seconds.

    The median `response_time_seconds` over the successful calls.
    `None` if there were no successful calls.
    """


def aggregate_host_health(
    engine: Engine, hosts: Iterable[str] | None = None
) -> dict[str, HostHealth]:
    """
    Combine each call record into one [HostHealth][(m).] per host

    One read of the health table, grouped by host.

    Parameters
    ----------
    engine
        The database to read from. The caller owns it.

    hosts
        If given, only aggregate these hosts (the pool the caller cares about).
        Hosts with no recorded calls are simply absent from the result.

    Returns
    -------
    :
        A [HostHealth][(m).] per host that has at least one recorded call,
        keyed by host
    """
    statement = select(SearchAPICallRecord)
    if hosts is not None:
        wanted = list(hosts)
        # An empty `hosts` means "nothing to look up", not "everything".
        if not wanted:
            return {}
        statement = statement.where(col(SearchAPICallRecord.host).in_(wanted))

    with Session(engine) as session:
        rows = session.exec(statement).all()

    by_host: dict[str, list[SearchAPICallRecord]] = {}
    for row in rows:
        by_host.setdefault(row.host, []).append(row)

    health: dict[str, HostHealth] = {}
    for host, host_rows in by_host.items():
        # Only successful calls carry a meaningful time.
        times = [r.response_time_seconds for r in host_rows if r.success]
        health[host] = HostHealth(
            host=host,
            n_calls=len(host_rows),
            n_success=len(times),
            success_rate=len(times) / len(host_rows) if host_rows else 0.0,
            median_response_time_seconds=median(times) if times else math.inf,
        )

    return health


HostRanker = Callable[[HostHealth], Any]
"""
Turns one host's health into a sort key; hosts are sorted ascending, best first

This is the injection seam for how to rank. A ranker just says "here is the key
to sort this host by", and [build_health_selector][(m).] sorts the pool by it.
"""


# TODO Future: In a future PR we will probably rank more intelligently
# by speed based on relevant searches.
# We hold off, because ranking by search is only significant
# if we know the serach that was made (esp. project/collection).
# We will only know that once we start tracking searches
# and linking them to requests in the database.
def get_median_response_time_for_ranking(health: HostHealth) -> float:
    """
    Get median response time in a form that can be used for ranking based on host health

    Parameters
    ----------
    health
        Host health information

    Returns
    -------
    :
        Median response time

        For hosts that have no response time measurements,
        we cast to `math.inf` so that these hosts are sorted last.
    """
    return (
        health.median_response_time_seconds
        if health.median_response_time_seconds
        else math.inf
    )


# The pool to reorder for each project, when the caller does not supply one.
# Same lists as `DEFAULT_SELECTOR`
DEFAULT_CANDIDATES: Mapping[str, Sequence[SearchAPI]] = {
    "CMIP5": CMIP5_APIS,
    "CMIP6": CMIP6_APIS,
    "CMIP7": CMIP7_APIS,
}


def _single_project(canonical: QueryCanonical) -> str:
    """
    Pull the one project out of a query, or explain why we cannot

    This will be used to choose which `DEFAULT_CANDIDATE` list of
    hosts to use, if no health information has been recorded.
    """
    if len(canonical.project) != 1:
        msg = (
            "We can only unambiguously pick the SearchAPI list "
            "if there is exactly one project, "
            f"received: {canonical.project}"
        )
        raise ValueError(msg)

    return canonical.project[0]


def _rank_pool(
    pool: Sequence[SearchAPI],
    health: Mapping[str, HostHealth],
    ranker: HostRanker,
) -> list[SearchAPI] | None:
    """
    Reorder one project's pool by health, or report that it has none

    Hosts with recorded health come first, sorted by `rank`; hosts with none
    keep their original order and follow, so an endpoint we have never called is
    still tried, just after the ones we can actually judge.

    Returns `None` when *no* host in the pool has any health at all, which is the
    signal to fall back to the default order rather than invent a ranking from
    nothing.
    """
    have_data = [api for api in pool if api.host in health]
    if not have_data:
        return None

    no_data = [api for api in pool if api.host not in health]
    # `sorted` is stable, so hosts that tie on the key keep their pool order.
    ranked = sorted(have_data, key=lambda api: ranker(health[api.host]))
    return ranked + no_data


def build_health_selector(
    engine: Engine,
    candidates: Mapping[str, Sequence[SearchAPI]] | None = None,
    *,
    rank: HostRanker = get_median_response_time_for_ranking,
    fallback: SearchAPISelector = DEFAULT_SELECTOR,
) -> SearchAPISelector:
    """
    Build a selector that orders each project's endpoints by recorded health

    The health is read *once*, now, when the selector is built. That keeps the
    selector cheap and deterministic for the run it is used in; build a fresh one
    to pick up calls recorded since.

    Parameters
    ----------
    engine
        The database to read health from. The caller owns it.

    candidates
        The endpoint pool to reorder per project. Defaults to the same lists as
        [DEFAULT_SELECTOR][esmporium.search.search_api.DEFAULT_SELECTOR].

    rank
        How to order the hosts that have health. Defaults to
        [rank_by_speed][(m).].

    fallback
        The selector to defer to for a query whose pool has no health yet.
        Defaults to [DEFAULT_SELECTOR][esmporium.search.search_api.DEFAULT_SELECTOR].

    Returns
    -------
    :
        A selector that ranks by health where it can, and falls back where it
        cannot
    """
    pools = candidates if candidates is not None else DEFAULT_CANDIDATES

    # Aggregate only the hosts we could actually pick, once, up front.
    pool_hosts = {api.host for pool in pools.values() for api in pool}
    health = aggregate_host_health(engine, pool_hosts)

    def select(canonical: QueryCanonical, attempt: int) -> SearchAPI | None:
        project = _single_project(canonical)
        pool = pools[project]

        ranked = _rank_pool(pool, health, rank)
        if ranked is None:
            # Nothing to rank on for this project: behave like the fallback.
            return fallback(canonical, attempt)

        return ranked[attempt] if attempt < len(ranked) else None

    return select
