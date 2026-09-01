"""
Unit tests related to search API health information
"""

from __future__ import annotations

import httpx
import pytest
from sqlmodel import Session, col, select

from esmporium.db import (
    HostHealth,
    SearchAPICallRecord,
    aggregate_host_health,
    build_health_selector,
    get_median_response_time_for_ranking,
    record_search_api_calls,
)
from esmporium.query import QueryCanonical, QueryCMIP6
from esmporium.search import (
    DEFAULT_SEARCH_API_FACADES_BY_PROJECT,
    SearchAPIESGF1Solr,
    SearchAPIFacade,
    SolrCMIP6Parameters,
    build_list_selector,
    search,
)
from esmporium.search.health import SearchAPICall
from esmporium.search.retry import build_transient_retrying


def make_call(  # noqa: PLR0913 - a factory mirroring every field of the record
    *,
    host: str = "esgf.example.org",
    http_method: str = "GET",
    url: str = "https://esgf.example.org/esg-search/search?x=1",
    request_body: str | None = None,
    response_code: int | None = 200,
    success: bool = True,
    error: Exception | None = None,
    num_results: int | None = 7,
    response_time_seconds: float = 0.25,
    attempt_number: int = 1,
) -> SearchAPICall:
    """Build a SearchAPICall with sensible defaults, overriding as needed."""
    return SearchAPICall(
        host=host,
        http_method=http_method,
        url=url,
        request_body=request_body,
        response_code=response_code,
        success=success,
        error=error,
        num_results=num_results,
        response_time_seconds=response_time_seconds,
        attempt_number=attempt_number,
    )


def test_from_call_copies_the_scalar_fields():
    call = make_call(
        host="h",
        http_method="POST",
        url="u",
        request_body='{"q": 1}',
        response_code=503,
        success=False,
        num_results=None,
        response_time_seconds=1.5,
        attempt_number=3,
        error=ValueError("x"),
    )

    row = SearchAPICallRecord.from_call(call)

    assert row.host == "h"
    assert row.http_method == "POST"
    assert row.url == "u"
    assert row.request_body == '{"q": 1}'
    assert row.response_code == 503
    assert row.success is False
    assert row.num_results is None
    assert row.response_time_seconds == 1.5
    assert row.attempt_number == 3


def test_from_call_translates_the_error_to_type_and_message():
    assert SearchAPICallRecord.from_call(make_call(error=None)).error is None

    row = SearchAPICallRecord.from_call(
        make_call(success=False, error=ValueError("boom"))
    )
    assert row.error == "ValueError: boom"


def test_record_persists_a_row(engine):
    observer = record_search_api_calls(engine)

    observer(make_call(host="node-a", num_results=42))

    with Session(engine) as session:
        rows = session.exec(select(SearchAPICallRecord)).all()

    (row,) = rows
    assert row.host == "node-a"
    assert row.num_results == 42
    assert row.success is True
    assert row.error is None
    assert row.created_at is not None


def test_record_stores_the_error_string(engine):
    observer = record_search_api_calls(engine)

    observer(
        make_call(
            success=False,
            response_code=500,
            num_results=None,
            error=ValueError("bad json"),
        )
    )

    with Session(engine) as session:
        (row,) = session.exec(select(SearchAPICallRecord)).all()

    assert row.success is False
    assert row.error == "ValueError: bad json"


def test_record_is_append_only(engine):
    observer = record_search_api_calls(engine)

    observer(make_call(host="a", attempt_number=1))
    observer(make_call(host="b", attempt_number=2))

    with Session(engine) as session:
        rows = session.exec(
            select(SearchAPICallRecord).order_by(col(SearchAPICallRecord.id))
        ).all()

    assert [row.host for row in rows] == ["a", "b"]
    assert [row.attempt_number for row in rows] == [1, 2]


# --- the health-based selector -------------------------------------------------

QUERY_CMIP6 = QueryCMIP6(experiment_id="historical", variable_id="tas", frequency="mon")
"""A single-project (CMIP6) query; its canonical form is what the selector sees"""

CMIP6 = QueryCanonical(project=("CMIP6",))
"""The canonical query a selector is handed for the tests that call it directly"""


def seed(engine, *calls: SearchAPICall) -> None:
    """Record some calls into the health table."""
    observer = record_search_api_calls(engine)
    for call in calls:
        observer(call)


def api(host: str) -> SearchAPIFacade:
    """Build a CMIP6-Solr facade for `host` (only the host matters to ranking)."""
    return SearchAPIFacade(
        parameters=SolrCMIP6Parameters,
        search_api=SearchAPIESGF1Solr(host, build_transient_retrying(1)),
    )


def hosts_offered(selector, canonical=CMIP6) -> list[str]:
    """Walk a selector to exhaustion and collect the hosts it offers, in order."""
    hosts: list[str] = []
    attempt = 0
    while (chosen := selector(canonical, attempt)) is not None:
        hosts.append(chosen.search_api.host)
        attempt += 1
    return hosts


def solr_response(num_found: int) -> httpx.Response:
    """A Solr-shaped 200 response reporting `num_found` matches."""
    return httpx.Response(200, json={"response": {"numFound": num_found, "docs": []}})


def client_for(handler) -> httpx.Client:
    """An httpx client whose requests are answered by `handler`."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_aggregate_rolls_up_speed_and_reliability(engine):
    seed(
        engine,
        make_call(host="a", success=True, response_time_seconds=0.2),
        make_call(host="a", success=True, response_time_seconds=0.4),
        make_call(host="a", success=False, num_results=None, response_time_seconds=9.0),
        make_call(host="b", success=False, num_results=None, response_time_seconds=9.0),
    )

    health = aggregate_host_health(engine)

    a = health["a"]
    assert a.n_calls == 3
    assert a.n_success == 2
    assert a.success_rate == pytest.approx(2 / 3)
    # Median of the successful times only; the failed call's time is ignored.
    assert a.median_response_time_seconds == pytest.approx((0.2 + 0.4) / 2.0)

    b = health["b"]
    assert b.n_calls == 1
    assert b.n_success == 0
    assert b.success_rate == 0.0
    assert b.median_response_time_seconds is None


def test_aggregate_filters_to_the_requested_hosts(engine):
    seed(engine, make_call(host="a"), make_call(host="b"))

    assert set(aggregate_host_health(engine, ["a"])) == {"a"}
    # An empty host list means "nothing to look up", not "everything".
    assert aggregate_host_health(engine, []) == {}


def test_rank_by_speed_orders_fastest_first_and_dead_last():
    fast = HostHealth("fast", 4, 4, 1.0, 0.1)
    mid = HostHealth("mid", 4, 4, 1.0, 0.5)
    dead = HostHealth("dead", 4, 0, 0.0, None)

    ranked = sorted([mid, dead, fast], key=get_median_response_time_for_ranking)

    assert [h.host for h in ranked] == ["fast", "mid", "dead"]


def test_selector_orders_the_pool_fastest_first(engine):
    seed(
        engine,
        make_call(host="slow", response_time_seconds=1.0),
        make_call(host="fast", response_time_seconds=0.1),
        make_call(host="mid", response_time_seconds=0.5),
    )
    candidates = {"CMIP6": [api("slow"), api("fast"), api("mid")]}

    selector = build_health_selector(engine, candidates)

    assert hosts_offered(selector) == ["fast", "mid", "slow"]


def test_selector_appends_hosts_with_no_health_after_ranked_ones(engine):
    seed(
        engine,
        make_call(host="fast", response_time_seconds=0.1),
        make_call(host="slow", response_time_seconds=0.9),
    )
    # "unknown" has never been called, so we cannot judge it.
    candidates = {"CMIP6": [api("unknown"), api("slow"), api("fast")]}

    selector = build_health_selector(engine, candidates)

    assert hosts_offered(selector) == ["fast", "slow", "unknown"]


def test_selector_falls_back_entirely_when_the_pool_has_no_health(engine):
    # Nothing recorded, so there is nothing to rank on.
    fallback = build_list_selector([api("fb-1"), api("fb-2")])
    candidates = {"CMIP6": [api("a"), api("b")]}

    selector = build_health_selector(engine, candidates, fallback=fallback)

    # It defers to the fallback verbatim, offering the fallback's own APIs.
    assert selector(CMIP6, 0).search_api.host == "fb-1"
    assert hosts_offered(selector) == ["fb-1", "fb-2"]


def test_selector_default_fallback_is_the_default_selector(engine):
    # Empty table + default candidates/fallback: behaves like the default order.
    selector = build_health_selector(engine)

    assert hosts_offered(selector) == [
        candidate.search_api.host
        for candidate in DEFAULT_SEARCH_API_FACADES_BY_PROJECT["CMIP6"]
    ]


def test_selector_injects_into_search_and_is_asked_in_ranked_order(engine):
    seed(
        engine,
        make_call(host="slow", response_time_seconds=0.9),
        make_call(host="fast", response_time_seconds=0.1),
    )
    candidates = {"CMIP6": [api("slow"), api("fast")]}
    selector = build_health_selector(engine, candidates)

    # Every node answers, so `results` is keyed in the order they were asked,
    # which is the selector's ranked (fastest-first) order.
    outcome = search(
        QUERY_CMIP6,
        selector,
        stop_at_first_result=False,
        client=client_for(lambda request: solr_response(1)),
    )

    assert list(outcome.results) == ["fast", "slow"]


def test_selector_needs_exactly_one_project(engine):
    seed(engine, make_call(host="a", response_time_seconds=0.1))
    selector = build_health_selector(engine, {"CMIP6": [api("a")]})

    with pytest.raises(ValueError, match="exactly one project"):
        selector(QueryCanonical(project=()), 0)
    with pytest.raises(ValueError, match="exactly one project"):
        selector(QueryCanonical(project=("CMIP5", "CMIP6")), 0)
