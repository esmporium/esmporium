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
from esmporium.search.search_api_facade import (
    DEFAULT_SEARCH_API_FACADES_BY_PROJECT,
    DEFAULT_SELECTOR,
    SearchAPIFacade,
    SearchAPIFacadeSelector,
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

    Returns
    -------
    :
        An observer which writes one row per call it is told about
    """

    def observer(call: SearchAPICall) -> None:
        # This function needs to be thread safe to handle parallelism
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
    """
    `n_success / n_calls`

    Should be set to `0.0` if there are somehow no calls
    (but this is generally impossible as hosts only appear in the database
    if they've been called).
    """

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

    Parameters
    ----------
    engine
        The database to read from.

    hosts
        Only aggregate these hosts (the pool the caller cares about).

        If not provided, we return information for all hosts in the database.

    Returns
    -------
    :
        A [HostHealth][(m).] per host that has at least one recorded call,
        keyed by host
    """
    # Grab everything
    statement = select(SearchAPICallRecord)
    if hosts is not None:
        wanted = list(hosts)
        # Make sure that if we get empty hosts,
        # we return nothing, not everything.
        if not wanted:
            return {}

        # Restrict search to just what we want
        statement = statement.where(col(SearchAPICallRecord.host).in_(wanted))

    with Session(engine) as session:
        rows = session.exec(statement).all()

    by_host: dict[str, list[SearchAPICallRecord]] = {}
    for row in rows:
        by_host.setdefault(row.host, []).append(row)

    health: dict[str, HostHealth] = {}
    for host, host_rows in by_host.items():
        times_successful = [r.response_time_seconds for r in host_rows if r.success]
        health[host] = HostHealth(
            host=host,
            n_calls=len(host_rows),
            n_success=len(times_successful),
            success_rate=len(times_successful) / len(host_rows) if host_rows else 0.0,
            median_response_time_seconds=median(times_successful)
            if times_successful
            else None,
        )

    return health


HostRanker = Callable[[HostHealth], Any]
"""
Turns one host's health into a sort key

This should return keys such that, when hosts are sorted ascending, the best is first.
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
        if health.median_response_time_seconds is not None
        else math.inf
    )


def _single_project(canonical: QueryCanonical) -> str:
    """
    Pull the one project out of a query, or explain why we cannot
    """
    if len(canonical.project) != 1:
        msg = (
            "We can only unambiguously pick the SearchAPI list "
            "if there is exactly one project in the query, "
            f"received: {canonical.project}"
        )
        raise ValueError(msg)

    return canonical.project[0]


def _rank_pool(
    pool: Sequence[SearchAPIFacade],
    health: Mapping[str, HostHealth],
    ranker: HostRanker,
) -> list[SearchAPIFacade] | None:
    """
    Reorder a search API pool by health, or report that there is no health information

    Hosts with recorded health come first, sorted by `rank`; hosts with none
    keep their original order and follow, so an endpoint we have never called is
    still tried, just after the ones we can actually judge.

    Returns `None` when *no* host in the pool has any health at all, which is the
    signal to fall back to the default order rather than invent a ranking from
    nothing.
    """
    have_data = [api for api in pool if api.search_api.host in health]
    if not have_data:
        return None

    no_data = [api for api in pool if api.search_api.host not in health]
    # `sorted` is stable, so hosts that tie on the key keep their pool order.
    ranked = sorted(have_data, key=lambda api: ranker(health[api.search_api.host]))
    return ranked + no_data


def build_health_selector(
    engine: Engine,
    candidates: Mapping[str, Sequence[SearchAPIFacade]] | None = None,
    *,
    ranker: HostRanker = get_median_response_time_for_ranking,
    fallback: SearchAPIFacadeSelector = DEFAULT_SELECTOR,
) -> SearchAPIFacadeSelector:
    """
    Build a selector that orders each project's search APIs by their health

    The health is read *once*, now, when the selector is built.
    That keeps the selector cheap and deterministic for the run it is used in.
    Build a new selector if you want to use updated information.

    Parameters
    ----------
    engine
        The database to read health from.

    candidates
        The search API pool to reorder, grouped by project.

    ranker
        How to rank hosts for which we have health information.

    fallback
        The selector to defer to for a query with no relevant health information.

    Returns
    -------
    :
        Health-based selector (falling back to a default where there is no information)
    """
    pools = (
        candidates if candidates is not None else DEFAULT_SEARCH_API_FACADES_BY_PROJECT
    )

    # Aggregate only the hosts we could actually pick, once, up front.
    all_hosts = {api.search_api.host for pool in pools.values() for api in pool}
    health = aggregate_host_health(engine, all_hosts)

    def select(canonical: QueryCanonical, attempt: int) -> SearchAPIFacade | None:
        project = _single_project(canonical)
        pool = pools[project]

        ranked = _rank_pool(pool, health, ranker)
        if ranked is None:
            return fallback(canonical, attempt)

        return ranked[attempt] if attempt < len(ranked) else None

    return select
