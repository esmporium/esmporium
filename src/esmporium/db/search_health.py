"""
Recording search API health into the database

Additional opt-in host-ranking selector, which uses the recorded
search API health information to rank hosts for future requests.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
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

    median_response_time: float
    """
    A representative response time for this host, in seconds

    The median `response_time_seconds` over the successful calls, or `inf` if
    there were none, so a host that never succeeds sorts to the back on speed.
    """


def aggregate_host_health(
    engine: Engine, hosts: Iterable[str] | None = None
) -> dict[str, HostHealth]:
    """
    Combine call records into a [HostHealth][(m).] per host

    Parameters
    ----------
    engine
        The database to read from.

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
    if hosts is None:
        # TODO: look up hosts from DB
        raise NotImplementedError
        # if not wanted:
        #     msg = "The database is empty"
        #     raise AssertionError(msg)

    else:
        wanted = list(hosts)

        if not wanted:
            msg = f"Please pass a list of hosts, received {hosts=}"
            raise ValueError(msg)

    statement = statement.where(col(SearchAPICallRecord.host).in_(wanted))

    with Session(engine) as session:
        rows = session.exec(statement).all()

    by_host: dict[str, list[SearchAPICallRecord]] = {}
    for row in rows:
        by_host.setdefault(row.host, []).append(row)

    health: dict[str, HostHealth] = {}
    for host, host_rows in by_host.items():
        success_times = [r.response_time_seconds for r in host_rows if r.success]
        health[host] = HostHealth(
            host=host,
            n_calls=len(host_rows),
            n_success=len(success_times),
            success_rate=len(success_times) / len(host_rows) if host_rows else 0.0,
            median_response_time=median(success_times) if success_times else math.inf,
        )

    return health


HostRanker = Callable[[HostHealth], Any]
"""
Turns one host's health into a sort key

The key is constructed such that, when the hosts are sorted ascending,
the best host comes first.
"""


# TODO Future: In future PR we will rank more intelligently by result and by speed
# We hold off future a future step, because ranking by result is only
# significant if we know the request (esp. project/collection) information,
# and if we can link that to the search a user may want to
# perform in the future. This we can do only once the database is connected
# by request and populated with results.
def rank_by_median_response_time(health: HostHealth) -> float:
    """
    Get median response time
    """
    return health.median_response_time


# In future, a fancy selector
# could repoll the database periodically
# (i.e. update the preference on the fly,
# rather than only setting up the ranking once.)
# In future we will also want `build_health_selector_by_project`
# or similar
# (`build_health_selector_by_group`
# in case people want to group by something other than project?).
# I've removed that now to try and keep this to the simplest thing,
# waiting until we have the links to requests
# before we figure out how to use that request information when setting up preferences.
def build_health_selector(
    engine: Engine,
    candidates: Iterable[SearchAPI] = tuple({*CMIP5_APIS, *CMIP6_APIS, *CMIP7_APIS}),
    *,
    ranker: HostRanker = rank_by_median_response_time,
    fallback: SearchAPISelector = DEFAULT_SELECTOR,
) -> SearchAPISelector:
    """
    Build a selector that orders search APIs by recorded search API health

    The health is read *once*, now, when the selector is built.
    That keeps the selector cheap and deterministic for the run it is used in.
    Build a fresh selector to update to the lastest health information.

    Parameters
    ----------
    engine
        The database to read health from.

    candidates
        The endpoint pool to select from.

    ranker
        Function which orders hosts that have health information.

    fallback
        The selector to defer to if there is no health information yet.

    Returns
    -------
    :
        A selector that ranks by health where it can, and falls back where it
        cannot
    """
    # # We would like to be able to have the case where no candidates are provided
    # # work like the below,
    # # where we figure out the search APIs from the database.
    # with Session(engine) as session:
    #     pools_in_db = session.scalars(
    #         select(SearchAPICallRecord.host).distinct()
    #     ).all()
    #
    # if not pools_in_db:
    #     # No pools in the database, use the fallback.
    #     return fallback
    #
    # pool = pools_in_db
    #
    # However, that would require us to have all the information required
    # to build SearchAPI instances from what we have in the database alone.
    # That is doable, but more complication than I want to deal with right now.
    # Hence just use the default tuple in the function instead.

    # Aggregate only the hosts we could actually pick, once, up front.
    hosts = {api.host for api in candidates}
    health = aggregate_host_health(engine, hosts)
    if not health:
        # No health information for any of the hosts of interest,
        # drop to the fallback.
        return fallback

    have_data = [api for api in candidates if api.host in health]
    no_data = [api for api in candidates if api.host not in health]
    ranked_with_data = sorted(have_data, key=lambda api: ranker(health[api.host]))
    ranked = ranked_with_data + no_data

    def selector(canonical: QueryCanonical, attempt: int) -> SearchAPI | None:
        return ranked[attempt] if attempt < len(ranked) else None

    return selector
