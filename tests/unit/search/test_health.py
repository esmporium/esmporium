"""
Unit tests for recording search-API health, driven against a mock search API

These cover what `fire` records on each path: success (with results, with none, and
with an uncountable body), the failure paths (client error, transient failure
retried, transport error, unparseable body), retries recording one row per attempt,
the opt-out when no observer is given, that `fire` fails loudly with a cause, and
that both callers (`search` and `check_query_values`) thread the observer down.
`fan_out` is covered here too. The database side of recording is covered in
`tests/unit/db/test_search_health.py`.
"""

from __future__ import annotations

import contextlib

import httpx
import pytest
from tenacity import Retrying, retry_if_exception, stop_after_attempt

from esmporium.query import QueryCMIP6, to_canonical
from esmporium.search import (
    ESGF1_CMIP6_FACADE_PARAMETERS,
    ESGFNG_CMIP6_FACADE_PARAMETERS,
    NoAPIWouldAnswerError,
    SearchAPIESGF1Solr,
    SearchAPIESGFNGSTAC,
    SearchAPIFacade,
    SearchAPIRequestError,
    build_list_selector,
    check_query_values,
    fan_out,
    fire,
    search,
)
from esmporium.search.health import SearchAPICall
from esmporium.search.retry import _is_transient

# NOTE: the mock helpers below are duplicated from
# `tests/unit/search/test_search.py` and `test_check_query_values.py`. They are
# small, and copying keeps this file self-contained. A future PR (PR3 needs a mock
# search endpoint of its own) may pull them into a shared `mock_search_api` module.

# NOTE 2: We explicitly do not test parallelism on search calls yet.
# A concurrency/thread test — without WAL + busy_timeout a
# concurrent-write SQLite test is flaky, and thread-safety is the deferred parallel
# PR's concern. Deterministic multi-row behaviour is already covered by the
# retry / multi-host cases in file 1.
# FROM ZN: "make sure that database writing works, even when calls are made in
# parallel so can clash/race each other or have other weird parallel side effects"

QUERY = QueryCMIP6(experiment_id="historical", variable_id="tas", frequency="mon")
"""A CMIP6 query the mock APIs answer for"""


def fast_retrying(attempts: int) -> Retrying:
    """Build a retry policy that respects the transient rule but never sleeps."""
    return Retrying(
        stop=stop_after_attempt(attempts),
        retry=retry_if_exception(_is_transient),
        reraise=True,
    )


def client_for(handler) -> httpx.Client:
    """Build an httpx client whose requests are answered by `handler`."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def solr_response(num_found: int) -> httpx.Response:
    """Build a Solr-shaped 200 response reporting `num_found` matches."""
    return httpx.Response(200, json={"response": {"numFound": num_found, "docs": []}})


def make_api(host, *, stac=False, attempts=1) -> SearchAPIFacade:
    """Build a CMIP6 facade for `host` with a no-sleep retry policy."""
    if stac:
        return SearchAPIFacade(
            parameters=ESGFNG_CMIP6_FACADE_PARAMETERS,
            search_api=SearchAPIESGFNGSTAC(host, fast_retrying(attempts)),
        )
    return SearchAPIFacade(
        parameters=ESGF1_CMIP6_FACADE_PARAMETERS,
        search_api=SearchAPIESGF1Solr(host, fast_retrying(attempts)),
    )


def record(handler, apis) -> list[SearchAPICall]:
    """Run a search through `handler`, returning the calls it recorded."""
    calls: list[SearchAPICall] = []
    # A total failure still records what it tried; that is what we assert on.
    with contextlib.suppress(NoAPIWouldAnswerError):
        search(
            QUERY,
            build_list_selector(apis),
            client=client_for(handler),
            observer=calls.append,
        )
    return calls


def a_call() -> SearchAPICall:
    """A minimal SearchAPICall, for the `fan_out` tests."""
    return SearchAPICall(
        host="h",
        http_method="GET",
        url="u",
        request_body=None,
        response_code=200,
        success=True,
        error=None,
        num_results=1,
        response_time_seconds=0.1,
        attempt_number=1,
    )


def test_success_with_results_records_one_call():
    (call,) = record(lambda r: solr_response(812), [make_api("esgf.example.org")])

    assert call.host == "esgf.example.org"
    assert call.http_method == "GET"
    assert "esg-search/search" in call.url
    assert "experiment_id=historical" in call.url  # the facet rode in the query string
    assert call.request_body is None  # a GET has no body
    assert call.response_code == 200
    assert call.success is True
    assert call.error is None
    assert call.num_results == 812
    assert call.response_time_seconds >= 0.0
    assert call.attempt_number == 1


def test_zero_results_still_records_a_success():
    (call,) = record(lambda r: solr_response(0), [make_api("host")])

    assert call.success is True
    assert call.num_results == 0


def test_success_with_an_uncountable_body_records_none_results():
    # A 200 we can parse as JSON but which reports no count.
    (call,) = record(
        lambda r: httpx.Response(200, json={"response": {"docs": []}}),
        [make_api("host")],
    )

    assert call.success is True
    assert call.num_results is None


def test_stac_post_records_its_method_and_body():
    api = make_api("search.example.io", stac=True)

    (call,) = record(lambda r: httpx.Response(200, json={"numberMatched": 3}), [api])

    assert call.http_method == "POST"
    assert call.request_body is not None
    assert "historical" in call.request_body  # the query rode in the JSON body
    assert call.num_results == 3


def test_a_client_error_is_not_retried():
    # attempts=3 would allow three tries, but a 4xx is a definite "no": the policy
    # does not retry it, so exactly one attempt is made and one call recorded.
    calls = record(lambda r: httpx.Response(404), [make_api("host", attempts=3)])

    assert len(calls) == 1
    assert calls[0].attempt_number == 1


def test_a_failed_call_records_the_status_and_error():
    (call,) = record(lambda r: httpx.Response(404), [make_api("host")])

    assert call.success is False
    assert call.response_code == 404
    assert isinstance(call.error, httpx.HTTPStatusError)
    assert call.num_results is None


def test_a_transient_failure_records_every_attempt():
    calls = record(lambda r: httpx.Response(503), [make_api("host", attempts=3)])

    assert [c.attempt_number for c in calls] == [1, 2, 3]
    assert all(c.success is False for c in calls)
    assert all(c.response_code == 503 for c in calls)


def test_a_retry_that_succeeds_records_the_failure_then_the_success():
    responses = iter([httpx.Response(503), solr_response(5)])

    fail, ok = record(lambda r: next(responses), [make_api("host", attempts=3)])

    assert (fail.attempt_number, fail.success) == (1, False)
    assert (ok.attempt_number, ok.success, ok.num_results) == (2, True, 5)


def test_a_transport_error_records_no_status_code():
    def boom(request):
        raise httpx.ConnectError("nope", request=request)

    (call,) = record(boom, [make_api("host", attempts=1)])

    assert call.success is False
    assert call.response_code is None  # nothing answered, so there is no code
    assert isinstance(call.error, httpx.TransportError)


def test_an_unparseable_body_is_not_retried():
    # A body we cannot parse is not a transient failure, so the policy does not
    # retry it: exactly one attempt is made and one call recorded, despite attempts=3.
    calls = record(
        lambda r: httpx.Response(200, content=b"not json"),
        [make_api("host", attempts=3)],
    )

    assert len(calls) == 1
    assert calls[0].attempt_number == 1


def test_an_unparseable_body_records_a_failure_with_the_status():
    (call,) = record(
        lambda r: httpx.Response(200, content=b"not json"),
        [make_api("host")],
    )

    assert call.success is False
    assert call.response_code == 200
    assert isinstance(call.error, ValueError)


def test_no_observer_records_nothing_but_still_works():
    selector = build_list_selector([make_api("host")])

    # Success still returns the JSON.
    outcome = search(QUERY, selector, client=client_for(lambda r: solr_response(1)))
    assert outcome.results["host"]["response"]["numFound"] == 1

    # Failure still raises.
    with pytest.raises(NoAPIWouldAnswerError):
        search(QUERY, selector, client=client_for(lambda r: httpx.Response(404)))


def test_fire_fails_loudly_carrying_the_cause():
    facade = make_api("host")
    request = facade.build_search_request(to_canonical(QUERY), 10)

    with pytest.raises(SearchAPIRequestError) as excinfo:
        fire(client_for(lambda r: httpx.Response(500)), facade.search_api, request)

    assert excinfo.value.host == "host"
    assert isinstance(excinfo.value.__cause__, httpx.HTTPStatusError)


def test_search_records_one_row_per_host_tried():
    def by_host(request):
        if request.url.host == "host-a":
            return httpx.Response(404)
        return solr_response(3)

    calls = record(by_host, [make_api("host-a"), make_api("host-b")])

    assert [c.host for c in calls] == ["host-a", "host-b"]
    assert [c.success for c in calls] == [False, True]
    assert calls[-1].num_results == 3


def test_check_query_values_records_its_call():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "facet_counts": {"facet_fields": {"experiment_id": ["historical", 5]}}
            },
        )

    calls: list[SearchAPICall] = []
    check_query_values(
        QueryCMIP6(experiment_id="historical"),
        build_list_selector([make_api("node")]),
        client=client_for(handler),
        observer=calls.append,
    )

    (call,) = calls
    assert call.host == "node"
    assert call.success is True


def test_fan_out_calls_each_observer_in_order():
    seen: list[str] = []

    fan_out(lambda c: seen.append("a"), lambda c: seen.append("b"))(a_call())

    assert seen == ["a", "b"]


def test_fan_out_with_no_observers_is_a_no_op():
    fan_out()(a_call())  # must not raise


def test_fan_out_propagates_an_observer_error():
    def boom(call):
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        fan_out(boom)(a_call())
