"""
Test the search step against a mock API

These cover the paths the live integration tests cannot control:
what happens when a node errors, when it errors transiently,
when its body cannot be read, when several nodes answer
and what we log.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading

import httpx
import pytest
from tenacity import Retrying, retry_if_exception, stop_after_attempt

from esmporium.query import QueryCMIP6
from esmporium.search import (
    ESGF1_CMIP5_FACADE_PARAMETERS,
    ESGF1_CMIP6_FACADE_PARAMETERS,
    ESGFNG_CMIP6_FACADE_PARAMETERS,
    CouldNotGetSearchResponseError,
    CouldNotUseSearchResultsError,
    ESGFNGResultParser,
    NoFacadeAnsweredError,
    PaginationLimitError,
    PaginationWarning,
    SearchAPIESGF1Solr,
    SearchAPIESGFNGSTAC,
    SearchAPIFacade,
    SelectorOfferedNoAPIFacadeError,
    SolrSingleRowResultParser,
    build_list_selector,
    search_single_project,
    stac_east_n_matches,
)
from esmporium.search.retry import _is_transient
from esmporium.search.search import (
    ClashingFacetsForFacadeError,
    CouldNotSearchError,
    FacadeKey,
    get_facade_key,
)

LOGGER_NAME = "esmporium.search.search"

QUERY_CMIP6 = QueryCMIP6(experiment_id="historical", variable_id="tas", frequency="mon")
"""A CMIP6 query; search_single_project() canonicalises it for us"""


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


def never_asked(request):
    """A handler for the tests in which nothing should be sent anywhere."""
    pytest.fail(f"unexpected request to {request.url}")


def solr_response(num_found: int) -> httpx.Response:
    """Build a Solr-shaped 200 response reporting `num_found` matches"""
    return httpx.Response(200, json={"response": {"numFound": num_found, "docs": []}})


def make_facade_cmip6_esgf1(
    host: str, attempts: int = 1, timeout: float = 30.0
) -> SearchAPIFacade:
    """Build a CMIP6-ESGF1 facade for `host`"""
    return SearchAPIFacade(
        parameters=ESGF1_CMIP6_FACADE_PARAMETERS,
        search_api=SearchAPIESGF1Solr(host, fast_retrying(attempts), timeout=timeout),
        result_parser=SolrSingleRowResultParser(),
    )


def solr_doc(doc_id: str) -> dict:
    """A minimal CMIP6 Solr record that parses into one dataset row"""
    return {
        "master_id": [f"CMIP6.{doc_id}"],
        "id": [doc_id],
        "project": ["CMIP6"],
        "source_id": ["ACCESS"],
        "institution_id": ["CSIRO"],
        "experiment_id": ["historical"],
        "variant_label": ["r1i1p1f1"],
        "variable_id": ["tas"],
        "frequency": ["mon"],
        "table_id": ["Amon"],
        "grid_label": ["gn"],
        "version": ["20200101"],
        "latest": [True],
        "retracted": [False],
        "data_node": ["node.example"],
    }


def paginated_solr(total: int, limit: int, seen_offsets: list[int] | None = None):
    """A Solr handler that serves `total` records `limit` at a time, by offset"""

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", 0))
        if seen_offsets is not None:
            seen_offsets.append(offset)
        docs = [
            solr_doc(f"d{offset + i}")
            for i in range(min(limit, max(0, total - offset)))
        ]
        return httpx.Response(
            200, json={"response": {"numFound": total, "start": offset, "docs": docs}}
        )

    return handler


def make_facade_cmip6_stac(host: str = "search.example.io") -> SearchAPIFacade:
    """Build a CMIP6-STAC (ESGF-NG) facade for `host`"""
    return SearchAPIFacade(
        parameters=ESGFNG_CMIP6_FACADE_PARAMETERS,
        search_api=SearchAPIESGFNGSTAC(host, fast_retrying(1)),
        result_parser=ESGFNGResultParser(read_n_matches=stac_east_n_matches),
    )


def stac_feature(feature_id: str) -> dict:
    """A minimal CMIP6 STAC feature that parses into one dataset row"""
    return {
        "id": feature_id,
        "collection": "CMIP6",
        "properties": {
            "title": f"CMIP6.{feature_id}",
            "version": "20200101",
            "latest": True,
            "retracted": False,
            "cmip6:source_id": "ACCESS",
            "cmip6:institution_id": "CSIRO",
            "cmip6:experiment_id": "historical",
            "cmip6:variant_label": "r1i1p1f1",
            "cmip6:variable_id": "tas",
            "cmip6:frequency": "mon",
            "cmip6:table_id": "Amon",
            "cmip6:grid_label": "gn",
        },
    }


def stac_next_link(token: str) -> dict:
    """The `next` link a STAC server hands back, carrying a continuation token"""
    return {
        "rel": "next",
        "method": "POST",
        "href": "https://search.example.io/search",
        "body": {"filter-lang": "cql2-json", "limit": 1, "token": token},
    }


def key_cmip6_esgf1(host: str) -> FacadeKey:
    """Build the key under which a CMIP6-ESGF1 facade for `host` files its answer"""
    return get_facade_key(make_facade_cmip6_esgf1(host))


def make_facade_cmip5_esgf1(host: str) -> SearchAPIFacade:
    """Build a CMIP5-ESGF1 facade for `host`"""
    return SearchAPIFacade(
        parameters=ESGF1_CMIP5_FACADE_PARAMETERS,
        search_api=SearchAPIESGF1Solr(host, fast_retrying(1), timeout=30.0),
        result_parser=SolrSingleRowResultParser(),
    )


def test_search_parses_the_answer_on_success():
    """A 200 is parsed into datasets and its match count, keyed by host"""
    selector = build_list_selector([make_facade_cmip6_esgf1("host")])

    outcome = search_single_project(
        QUERY_CMIP6, selector, client=client_for(lambda r: solr_response(3))
    )

    # The body carries no docs, so there are no datasets, but the count is still read.
    result_key = key_cmip6_esgf1("host")
    assert outcome.parsed_docs == {result_key: ()}
    assert outcome.n_matches == {result_key: 3}
    assert outcome.failures == {}


def test_search_uses_the_apis_own_timeout():
    """The per-node timeout on the SearchAPI is the one applied to the request"""
    seen: list[httpx.Timeout] = []

    def handler(request):
        seen.append(request.extensions["timeout"])
        return solr_response(1)

    selector = build_list_selector([make_facade_cmip6_esgf1("host", timeout=5.0)])

    search_single_project(QUERY_CMIP6, selector, client=client_for(handler))

    assert seen == [{"connect": 5.0, "read": 5.0, "write": 5.0, "pool": 5.0}]


def test_search_raises_on_a_client_error_without_retrying():
    """A 4xx is a real 'no'; we do not ask again"""
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(404)

    selector = build_list_selector([make_facade_cmip6_esgf1("host", attempts=3)])

    with pytest.raises(NoFacadeAnsweredError, match="host"):
        search_single_project(QUERY_CMIP6, selector, client=client_for(handler))

    assert calls == 1


def test_search_retries_a_transient_failure_then_gives_up():
    """A 5xx is retried up to the policy's limit, then reported as no answer"""
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    selector = build_list_selector([make_facade_cmip6_esgf1("host", attempts=3)])

    with pytest.raises(NoFacadeAnsweredError, match="host"):
        search_single_project(QUERY_CMIP6, selector, client=client_for(handler))

    assert calls == 3


def test_search_retries_a_transient_failure_then_succeeds():
    """A node that flaps once and then answers is retried into a success"""
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(500) if calls == 1 else solr_response(9)

    selector = build_list_selector([make_facade_cmip6_esgf1("host", attempts=3)])

    outcome = search_single_project(QUERY_CMIP6, selector, client=client_for(handler))

    assert outcome.n_matches == {key_cmip6_esgf1("host"): 9}
    assert calls == 2


def test_search_raises_when_the_body_is_not_json():
    """A 200 we cannot read as JSON is no more useful than no answer"""
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"not json at all")

    selector = build_list_selector([make_facade_cmip6_esgf1("host", attempts=3)])

    with pytest.raises(NoFacadeAnsweredError, match="host"):
        search_single_project(QUERY_CMIP6, selector, client=client_for(handler))

    assert calls == 1, "an unreadable body is not a transient failure"


def by_host(request: httpx.Request) -> httpx.Response:
    """Answer with a match count that depends on which host was asked"""
    counts = {"host-a": 5, "host-b": 7}
    return solr_response(counts.get(request.url.host, 0))


def test_search_stops_at_the_first_answer_by_default():
    """One good answer is enough, so the second node is never asked"""
    selector = build_list_selector(
        [make_facade_cmip6_esgf1("host-a"), make_facade_cmip6_esgf1("host-b")]
    )

    outcome = search_single_project(QUERY_CMIP6, selector, client=client_for(by_host))

    result_key = key_cmip6_esgf1("host-a")
    assert list(outcome.parsed_docs) == [result_key]
    assert outcome.n_matches[result_key] == 5
    # host-b was never asked, so it did not fail either.
    assert outcome.failures == {}


def test_search_aggregates_every_node_when_asked_to():
    """With stop turned off, every node's answer is kept, keyed by host"""
    selector = build_list_selector(
        [make_facade_cmip6_esgf1("host-a"), make_facade_cmip6_esgf1("host-b")]
    )

    outcome = search_single_project(
        QUERY_CMIP6, selector, stop_at_first_result=False, client=client_for(by_host)
    )

    assert set(outcome.parsed_docs) == {
        key_cmip6_esgf1("host-a"),
        key_cmip6_esgf1("host-b"),
    }
    assert outcome.n_matches[key_cmip6_esgf1("host-a")] == 5
    assert outcome.n_matches[key_cmip6_esgf1("host-b")] == 7


def test_search_hands_each_answer_to_the_processor():
    """As each host answers, the processor is called with that host's parsed docs"""
    calls: list[tuple[str, tuple]] = []
    selector = build_list_selector(
        [make_facade_cmip6_esgf1("host-a"), make_facade_cmip6_esgf1("host-b")]
    )

    outcome = search_single_project(
        QUERY_CMIP6,
        selector,
        stop_at_first_result=False,
        client=client_for(by_host),
        processor=lambda facade, parsed: calls.append((facade, parsed)),
    )

    assert [facade for facade, _ in calls] == [selector(None, 0), selector(None, 1)]
    for facade, parsed in calls:
        assert parsed == outcome.parsed_docs[get_facade_key(facade)]


def test_search_does_not_call_the_processor_for_a_failure():
    """A host that does not answer is never handed to the processor"""
    calls: list[str] = []

    def handler(request):
        if request.url.host == "host-a":
            return httpx.Response(404)
        return solr_response(4)

    selector = build_list_selector(
        [make_facade_cmip6_esgf1("host-a"), make_facade_cmip6_esgf1("host-b")]
    )

    search_single_project(
        QUERY_CMIP6,
        selector,
        client=client_for(handler),
        processor=lambda facade, parsed: calls.append(facade),
    )

    assert calls == [selector(None, 1)]


def test_search_skips_a_node_that_does_not_answer():
    """A node that errors is passed over, and the next one is tried"""

    def handler(request):
        if request.url.host == "host-a":
            return httpx.Response(404)
        return solr_response(4)

    selector = build_list_selector(
        [make_facade_cmip6_esgf1("host-a"), make_facade_cmip6_esgf1("host-b")]
    )

    outcome = search_single_project(QUERY_CMIP6, selector, client=client_for(handler))

    assert list(outcome.parsed_docs) == [key_cmip6_esgf1("host-b")]
    assert outcome.n_matches[key_cmip6_esgf1("host-b")] == 4
    # The node which was passed over is kept, with what it said.
    assert set(outcome.failures) == {key_cmip6_esgf1("host-a")}
    failure = outcome.failures[key_cmip6_esgf1("host-a")]
    assert isinstance(failure, CouldNotGetSearchResponseError)
    assert failure.facade_key == key_cmip6_esgf1("host-a")
    assert "host-a" in str(failure)


def test_search_skips_a_node_whose_answer_we_cannot_read():
    """
    A node answering in a shape we do not understand must not sink the search

    This is the whole point of parsing per host: `host-a` replies 200 with a
    document that carries no `master_id`, which is a real answer we simply cannot
    read. `host-b`'s perfectly good answer has to survive that.
    """

    def handler(request):
        if request.url.host == "host-a":
            # A 200, with a document missing a field every Solr record has.
            return httpx.Response(
                200,
                json={"response": {"numFound": 1, "docs": [{"id": ["no-master-id"]}]}},
            )

        return solr_response(4)

    selector = build_list_selector(
        [make_facade_cmip6_esgf1("host-a"), make_facade_cmip6_esgf1("host-b")]
    )

    outcome = search_single_project(
        QUERY_CMIP6, selector, client=client_for(handler), stop_at_first_result=False
    )

    assert list(outcome.parsed_docs) == [key_cmip6_esgf1("host-b")]
    assert outcome.n_matches[key_cmip6_esgf1("host-b")] == 4

    failure = outcome.failures[key_cmip6_esgf1("host-a")]
    assert isinstance(failure, CouldNotUseSearchResultsError)
    # The failure says the host answered, says what we could not read in it,
    # and says what to send again to see it for yourself.
    assert "answered our search request with something we could not read" in str(
        failure
    )
    assert "the project specific id" in str(failure)
    assert "We asked: https://host-a/esg-search/search." in str(failure)


def test_search_raises_when_no_node_answers_readably():
    """Every node failing is still every node failing, however they failed"""

    def handler(request):
        return httpx.Response(
            200, json={"response": {"numFound": 1, "docs": [{"id": ["no-master-id"]}]}}
        )

    selector = build_list_selector([make_facade_cmip6_esgf1("host-a")])

    with pytest.raises(
        NoFacadeAnsweredError,
        match=re.escape(
            f"{key_cmip6_esgf1('host-a')} answered our search request "
            "with something we could not read"
        ),
    ):
        search_single_project(QUERY_CMIP6, selector, client=client_for(handler))


def test_search_with_no_endpoint_to_try_raises():
    """
    Test that a selector with nothing to offer is an error, not an empty result

    Somebody who calls `search` wants a search to happen.
    An empty dict would say "we searched and found nothing",
    when in truth nothing was searched at all.
    """
    with pytest.raises(SelectorOfferedNoAPIFacadeError, match="CMIP6"):
        search_single_project(
            QUERY_CMIP6, build_list_selector([]), client=client_for(never_asked)
        )


def test_search_keeps_an_empty_but_valid_answer():
    """'Nothing matched' is an answer, so it is kept"""
    selector = build_list_selector([make_facade_cmip6_esgf1("host-a")])

    outcome = search_single_project(
        QUERY_CMIP6, selector, client=client_for(lambda request: solr_response(0))
    )

    assert outcome.n_matches[key_cmip6_esgf1("host-a")] == 0


def test_search_builds_and_closes_its_own_client(monkeypatch):
    """With no client given, search builds one for the call and closes it after"""
    built = client_for(lambda request: solr_response(2))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: built)

    selector = build_list_selector([make_facade_cmip6_esgf1("host-a")])
    outcome = search_single_project(QUERY_CMIP6, selector)

    assert outcome.n_matches[key_cmip6_esgf1("host-a")] == 2
    assert built.is_closed, "a client search built itself should be closed after"


def test_search_logs_the_request_at_debug(caplog):
    """At DEBUG, the request is logged as URL, curl, and structured fields"""
    selector = build_list_selector([make_facade_cmip6_esgf1("esgf.example.org")])

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        search_single_project(
            QUERY_CMIP6,
            selector,
            limit=2,
            client=client_for(lambda r: solr_response(1)),
        )

    # Filter to the request log specifically (only it carries the http_* fields):
    # paging may also debug-log, e.g. when the reported total and what came back differ.
    records = [
        r for r in caplog.records if r.name == LOGGER_NAME and hasattr(r, "http_curl")
    ]
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


def test_search_curl_reproduces_a_post_body(caplog):
    """The curl-equivalent of a POST carries its method and body"""
    # STAC is our POST-based API format, so search it to exercise the POST path.
    stac_api = SearchAPIFacade(
        parameters=ESGFNG_CMIP6_FACADE_PARAMETERS,
        search_api=SearchAPIESGFNGSTAC("search.example.io", fast_retrying(1)),
        result_parser=ESGFNGResultParser(
            read_n_matches=stac_east_n_matches,
        ),
    )
    selector = build_list_selector([stac_api])

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        search_single_project(
            QUERY_CMIP6,
            selector,
            # A real STAC answer always carries `features`, empty or not.
            client=client_for(
                lambda r: httpx.Response(200, json={"numberMatched": 1, "features": []})
            ),
        )

    (record,) = [
        r for r in caplog.records if r.name == LOGGER_NAME and hasattr(r, "http_curl")
    ]
    assert "-X POST" in record.http_curl
    assert "--data" in record.http_curl
    # "historical" is the experiment_id from QUERY_CMIP6, so it rides in the body.
    assert "historical" in record.http_curl


def test_search_does_not_log_below_debug(caplog):
    """Below DEBUG nothing is logged"""
    selector = build_list_selector([make_facade_cmip6_esgf1("host")])

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        search_single_project(
            QUERY_CMIP6, selector, client=client_for(lambda r: solr_response(1))
        )

    assert [r for r in caplog.records if r.name == LOGGER_NAME] == []


def test_search_pages_through_all_solr_results():
    """When more matched than fit one page, every page is fetched and collected"""
    selector = build_list_selector([make_facade_cmip6_esgf1("host")])
    offsets: list[int] = []
    pages: list[tuple[str, tuple]] = []

    outcome = search_single_project(
        QUERY_CMIP6,
        selector,
        limit=2,
        client=client_for(paginated_solr(5, 2, seen_offsets=offsets)),
        processor=lambda facade, parsed: pages.append((facade, parsed)),
    )

    # All 5 records are collected across the pages, and the total is the total.
    assert len(outcome.parsed_docs[key_cmip6_esgf1("host")]) == 5
    assert outcome.n_matches[key_cmip6_esgf1("host")] == 5
    # 5 records at 2 per page is three pages, requested at offsets 0, 2, 4.
    assert offsets == [0, 2, 4]
    # The processor is handed each page as it arrives, not the lot at the end.
    assert [len(parsed) for _, parsed in pages] == [2, 2, 1]


def test_search_does_not_page_when_the_first_page_holds_everything():
    """A single-page result makes a single request"""
    selector = build_list_selector([make_facade_cmip6_esgf1("host")])
    offsets: list[int] = []

    outcome = search_single_project(
        QUERY_CMIP6,
        selector,
        limit=10,
        client=client_for(paginated_solr(3, 10, seen_offsets=offsets)),
    )

    assert len(outcome.parsed_docs[key_cmip6_esgf1("host")]) == 3
    assert offsets == [0]


def test_search_pages_through_all_stac_results():
    """STAC is paged by following the server's `next` token until it is gone"""
    selector = build_list_selector([make_facade_cmip6_stac()])
    responses = iter(
        [
            httpx.Response(
                200,
                json={
                    "numberMatched": 3,
                    "features": [stac_feature("a")],
                    "links": [stac_next_link("t1")],
                },
            ),
            httpx.Response(
                200,
                json={
                    "numberMatched": 3,
                    "features": [stac_feature("b")],
                    "links": [stac_next_link("t2")],
                },
            ),
            httpx.Response(
                200,
                json={"numberMatched": 3, "features": [stac_feature("c")], "links": []},
            ),
        ]
    )
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return next(responses)

    outcome = search_single_project(
        QUERY_CMIP6, selector, limit=1, client=client_for(handler)
    )

    stac_key = get_facade_key(make_facade_cmip6_stac())
    assert len(outcome.parsed_docs[stac_key]) == 3
    assert outcome.n_matches[stac_key] == 3
    # Page 1 is our own request (no token); pages 2 and 3 resend the tokens the
    # server handed back, in order.
    assert "token" not in bodies[0]
    assert bodies[1]["token"] == "t1"  # noqa: S105 - a pagination token, not a secret
    assert bodies[2]["token"] == "t2"  # noqa: S105 - a pagination token, not a secret


def test_search_keeps_earlier_pages_when_a_later_page_fails():
    """A failure part way keeps the pages we got and records the failure"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={
                    "response": {
                        "numFound": 6,
                        "start": 0,
                        "docs": [solr_doc("a"), solr_doc("b")],
                    }
                },
            )
        return httpx.Response(503)

    selector = build_list_selector([make_facade_cmip6_esgf1("host", attempts=1)])

    outcome = search_single_project(
        QUERY_CMIP6, selector, limit=2, client=client_for(handler)
    )

    # The first page is kept...
    assert len(outcome.parsed_docs[key_cmip6_esgf1("host")]) == 2
    # ...alongside the total, so the shortfall is visible...
    assert outcome.n_matches[key_cmip6_esgf1("host")] == 6
    # ...and the failure that stopped us is recorded for the same facade.
    assert isinstance(
        outcome.failures[key_cmip6_esgf1("host")], CouldNotGetSearchResponseError
    )


def test_search_partial_answer_does_not_stop_it_trying_the_next_host():
    """A host that fails part way should not count as the first result"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "host-a":
            # First page fine, then the next page fails.
            if request.url.params.get("offset") is None:
                return httpx.Response(
                    200,
                    json={
                        "response": {"numFound": 4, "start": 0, "docs": [solr_doc("a")]}
                    },
                )
            return httpx.Response(404)
        return solr_response(2)

    selector = build_list_selector(
        [make_facade_cmip6_esgf1("host-a"), make_facade_cmip6_esgf1("host-b")]
    )

    outcome = search_single_project(
        QUERY_CMIP6, selector, limit=1, client=client_for(handler)
    )

    # host-a gave a partial answer and a failure; host-b was still asked and answered.
    assert set(outcome.failures) == {key_cmip6_esgf1("host-a")}
    assert key_cmip6_esgf1("host-b") in outcome.parsed_docs


def test_search_raises_when_the_result_cap_is_exceeded():
    """The max_results guardrail stops a runaway search"""
    selector = build_list_selector([make_facade_cmip6_esgf1("host")])

    with pytest.raises(
        PaginationLimitError,
        match=re.escape(
            f"While paging results from {key_cmip6_esgf1('host')}: collected more "
            "than the max_results safety cap of 3. Raise max_results (or set it to "
            "None) to fetch more."
        ),
    ):
        search_single_project(
            QUERY_CMIP6,
            selector,
            limit=2,
            max_results=3,
            client=client_for(paginated_solr(10, 2)),
        )


def test_search_raises_when_an_endpoint_loops():
    """An endpoint that re-offers a page we already asked for is a loop, and refused"""
    selector = build_list_selector([make_facade_cmip6_stac()])

    def handler(request: httpx.Request) -> httpx.Response:
        # Always the same token, so the next request never changes: a loop.
        return httpx.Response(
            200,
            json={
                "numberMatched": 10,
                "features": [],
                "links": [stac_next_link("stuck")],
            },
        )

    with pytest.raises(
        PaginationLimitError,
        match=re.escape(
            f"While paging results from {get_facade_key(make_facade_cmip6_stac())}: "
            "the endpoint asked us to re-request a page we had already requested, "
            "which would page forever."
        ),
    ):
        search_single_project(QUERY_CMIP6, selector, limit=1, client=client_for(handler))


def test_search_warns_when_it_will_paginate():
    """A search matching more than one page's worth warns that it will page"""
    selector = build_list_selector([make_facade_cmip6_esgf1("host")])

    # 6 matches at 2 per page is 3 pages, so it warns and names the numbers.
    with pytest.warns(
        PaginationWarning,
        match=re.escape(
            "This search of host matched 6 records but fetches 2 per page, so it "
            "will page through about 3 requests and may take a while. Raise `limit` "
            "to fetch more records per request, narrow your query, or pass "
            "warn_on_pagination=False to silence this."
        ),
    ):
        search_single_project(
            QUERY_CMIP6,
            selector,
            limit=2,
            client=client_for(paginated_solr(6, 2)),
        )


def test_warn_on_pagination_false_silences_the_warning(recwarn):
    """The pagination warning is opt-out: `warn_on_pagination=False` stops it"""
    selector = build_list_selector([make_facade_cmip6_esgf1("host")])

    search_single_project(
        QUERY_CMIP6,
        selector,
        limit=2,
        warn_on_pagination=False,
        client=client_for(paginated_solr(6, 2)),
    )

    assert not [w for w in recwarn if isinstance(w.message, PaginationWarning)]


def test_search_does_not_warn_when_the_results_fit_in_one_page(recwarn):
    """A search whose matches all fit in one page does not warn about paging"""
    selector = build_list_selector([make_facade_cmip6_esgf1("host")])

    # 2 matches at 5 per page is a single page: no paging, so nothing to warn about.
    search_single_project(
        QUERY_CMIP6,
        selector,
        limit=5,
        client=client_for(paginated_solr(2, 5)),
    )

    assert not [w for w in recwarn if isinstance(w.message, PaginationWarning)]


def test_search_treats_an_other_terms_clash_as_that_facade_failing():
    """
    A clash stops one facade, not the search

    Which names clash depends on the facade: `variable_id` is what the CMIP6
    query style calls the variable, so it collides there, while the CMIP5 style
    calls it `variable` and has no quarrel with it. The clash is therefore
    carried in `failures` like any other reason one facade gave us nothing.
    """
    query = QueryCMIP6(variable_id="tas", other_terms={"variable_id": "tas"})
    clashing = make_facade_cmip6_esgf1("host-a")
    answering = make_facade_cmip5_esgf1("host-b")
    selector = build_list_selector([clashing, answering])

    outcome = search_single_project(
        query, selector, stop_at_first_result=False, client=client_for(by_host)
    )

    assert set(outcome.parsed_docs) == {get_facade_key(answering)}

    failure = outcome.failures[get_facade_key(clashing)]
    assert isinstance(failure, ClashingFacetsForFacadeError)
    # It is a search failure, so `except CouldNotSearchError` catches it too.
    assert isinstance(failure, CouldNotSearchError)
    assert failure.facade_key == get_facade_key(clashing)
    assert failure.clashing == ("variable_id",)
    assert failure.query is query
