"""
Test the search step against a mock API

These cover the paths the live integration tests cannot control:
what happens when a node errors, when it errors transiently,
when its body cannot be read, when several nodes answer
and what we log.
"""

from __future__ import annotations

import logging
import os
import threading

import httpx
from tenacity import Retrying, retry_if_exception, stop_after_attempt

from esmporium.query import QueryCMIP6
from esmporium.search import Request, SearchAPI, build_list_selector, fire, search
from esmporium.search.retry import _is_transient
from esmporium.search.search_api import SOLR_CMIP6

LOGGER_NAME = "esmporium.search.search"

QUERY_CMIP6 = QueryCMIP6(experiment_id="historical", variable_id="tas", frequency="mon")
"""A CMIP6 query; search() canonicalises it for us"""


def fast_retrying(attempts: int) -> Retrying:
    """Build a retry policy without backoff sleeps"""
    return Retrying(
        stop=stop_after_attempt(attempts),
        retry=retry_if_exception(_is_transient),
        reraise=True,
    )


def client_for(handler) -> httpx.Client:
    """Build an httpx client whose requests are answered by `handler`"""
    return httpx.Client(transport=httpx.MockTransport(handler))


def solr_response(num_found: int) -> httpx.Response:
    """Build a Solr-shaped 200 response reporting `num_found` matches"""
    return httpx.Response(200, json={"response": {"numFound": num_found, "docs": []}})


def make_search_api(host: str, attempts: int = 1) -> SearchAPI:
    """Build a CMIP6-Solr SearchAPI for `host`"""
    return SearchAPI(host, SOLR_CMIP6, fast_retrying(attempts))


# TODO Anna: please use the below prompt with claude (or just do it yourself, up to you)
# Please change all the tests of fire into equivalent tests of `search`.
# `fire` is a function that we might change or remove.
# I want to keep these tests, but I want to keep them on the function I care about,
# `search`, not a function that is (to me) an implementation detail i.e. `fire`.
def test_fire_returns_the_json_on_success():
    """A 200 with a JSON body comes back as that JSON"""
    client = client_for(lambda request: solr_response(3))
    request = Request("GET", "/esg-search/search", params={"limit": 2})

    raw = fire(client, make_search_api("host"), request)

    assert raw == {"response": {"numFound": 3, "docs": []}}


def test_fire_returns_none_on_a_client_error_without_retrying():
    """A 4xx is a real 'no'; we do not ask again"""
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(404)

    client = client_for(handler)
    request = Request("GET", "/esg-search/search")

    raw = fire(client, make_search_api("host", attempts=3), request)

    assert raw is None
    assert calls == 1


def test_fire_retries_a_transient_failure_then_gives_up():
    """A 5xx is retried up to the policy's limit, then reported as no answer"""
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    client = client_for(handler)
    request = Request("GET", "/esg-search/search")

    raw = fire(client, make_search_api("host", attempts=3), request)

    assert raw is None
    assert calls == 3


def test_fire_retries_a_transient_failure_then_succeeds():
    """A node that flaps once and then answers is retried into a success"""
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(500) if calls == 1 else solr_response(9)

    client = client_for(handler)
    request = Request("GET", "/esg-search/search")

    raw = fire(client, make_search_api("host", attempts=3), request)

    assert raw == {"response": {"numFound": 9, "docs": []}}
    assert calls == 2


def test_fire_returns_none_when_the_body_is_not_json():
    """A 200 we cannot read as JSON is no more useful than no answer"""
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"not json at all")

    client = client_for(handler)
    request = Request("GET", "/esg-search/search")

    raw = fire(client, make_search_api("host", attempts=3), request)

    assert raw is None
    assert calls == 1, "an unreadable body is not a transient failure"


def by_host(request: httpx.Request) -> httpx.Response:
    """Answer with a match count that depends on which host was asked"""
    counts = {"host-a": 5, "host-b": 7}
    return solr_response(counts.get(request.url.host, 0))


def test_search_stops_at_the_first_answer_by_default():
    """One good answer is enough, so the second node is never asked"""
    selector = build_list_selector(
        [make_search_api("host-a"), make_search_api("host-b")]
    )

    results = search(QUERY_CMIP6, selector, client=client_for(by_host))

    assert list(results) == ["host-a"]
    assert results["host-a"]["response"]["numFound"] == 5


def test_search_aggregates_every_node_when_asked_to():
    """With stop turned off, every node's answer is kept, keyed by host"""
    selector = build_list_selector(
        [make_search_api("host-a"), make_search_api("host-b")]
    )

    results = search(
        QUERY_CMIP6, selector, stop_at_first_result=False, client=client_for(by_host)
    )

    assert set(results) == {"host-a", "host-b"}
    assert results["host-a"]["response"]["numFound"] == 5
    assert results["host-b"]["response"]["numFound"] == 7


def test_search_skips_a_node_that_does_not_answer():
    """A node that errors is passed over, and the next one is tried"""

    def handler(request):
        if request.url.host == "host-a":
            return httpx.Response(404)
        return solr_response(4)

    selector = build_list_selector(
        [make_search_api("host-a"), make_search_api("host-b")]
    )

    results = search(QUERY_CMIP6, selector, client=client_for(handler))

    assert list(results) == ["host-b"]
    assert results["host-b"]["response"]["numFound"] == 4


def test_search_keeps_an_empty_but_valid_answer():
    """'Nothing matched' is an answer, so it is kept"""
    selector = build_list_selector([make_search_api("host-a")])

    results = search(
        QUERY_CMIP6, selector, client=client_for(lambda request: solr_response(0))
    )

    assert results["host-a"]["response"]["numFound"] == 0


def test_search_builds_and_closes_its_own_client(monkeypatch):
    """With no client given, search builds one for the call and closes it after"""
    built = client_for(lambda request: solr_response(2))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: built)

    selector = build_list_selector([make_search_api("host-a")])
    results = search(QUERY_CMIP6, selector)

    assert results["host-a"]["response"]["numFound"] == 2
    assert built.is_closed, "a client search built itself should be closed after"


# TODO Anna: as above, make these focus on `search` not `fire`
def test_fire_logs_the_request_at_debug(caplog):
    """At DEBUG, the request is logged as URL, curl, and structured fields"""
    client = client_for(lambda request: solr_response(1))
    request = Request("GET", "/esg-search/search", params={"limit": 2})

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        fire(client, make_search_api("esgf.example.org"), request)

    records = [r for r in caplog.records if r.name == LOGGER_NAME]
    assert len(records) == 1
    record = records[0]

    assert record.levelno == logging.DEBUG
    assert record.http_method == "GET"
    assert record.search_api_host == "esgf.example.org"
    assert record.http_url.startswith("https://esgf.example.org/esg-search/search")
    assert "limit=2" in record.http_url
    assert record.http_curl.startswith("curl ")
    assert "curl " in record.getMessage()

    # The process and thread the request went out on: the standard library
    # records these on every log record, which is what we rely on.
    assert record.process == os.getpid()
    assert record.thread == threading.get_ident()


def test_fire_curl_reproduces_a_post_body(caplog):
    """The curl-equivalent of a POST carries its method and body"""
    client = client_for(lambda request: httpx.Response(200, json={"numberMatched": 1}))
    request = Request("POST", "/search", json_body={"filter": "keep-me"})

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        fire(client, make_search_api("search.example.io"), request)

    (record,) = [r for r in caplog.records if r.name == LOGGER_NAME]
    assert "-X POST" in record.http_curl
    assert "--data" in record.http_curl
    assert "keep-me" in record.http_curl


def test_fire_does_not_log_below_debug(caplog):
    """Below DEBUG nothing is logged"""
    client = client_for(lambda request: solr_response(1))
    request = Request("GET", "/esg-search/search")

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        fire(client, make_search_api("host"), request)

    assert [r for r in caplog.records if r.name == LOGGER_NAME] == []
