"""Unit tests for sending a single search request (retry/parse/timing logic).

All network is faked with ``httpx.MockTransport``; nothing here touches ESGF.
"""

import json

import httpx

from esmporium.esgf.canonical import CanonicalQuery
from esmporium.esgf.search import (
    IndexNode,
    SearchAPIGeneration,
    build_request,
    get_generation_config,
    search_once,
)

STAC_CFG = get_generation_config(SearchAPIGeneration.ESGF_NG_WEST)
SOLR_CFG = get_generation_config(SearchAPIGeneration.ESGF1)
STAC_NODE = IndexNode(
    host="discovery.west.esgf.io", generation=SearchAPIGeneration.ESGF_NG_WEST
)
SOLR_NODE = IndexNode(host="esgf.ceda.ac.uk", generation=SearchAPIGeneration.ESGF1)
STAC_REQ = build_request(CanonicalQuery(model=("X",)), "CMIP6", STAC_CFG)
SOLR_REQ = build_request(CanonicalQuery(model=("X",)), "CMIP6", SOLR_CFG)

STAC_HIT = {"type": "FeatureCollection", "numberMatched": 5, "features": [{"id": "a"}]}
STAC_EMPTY = {"type": "FeatureCollection", "numberMatched": 0, "features": []}


class _Script:
    """A MockTransport handler that plays a scripted list of actions in order.

    Each action is either ``(status, payload_or_None)`` or an exception to raise.
    """

    def __init__(self, *actions):
        self.actions = list(actions)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        action = self.actions.pop(0)
        if isinstance(action, Exception):
            raise action
        status, payload = action
        if payload is None:
            return httpx.Response(status, text="error-body")
        return httpx.Response(status, json=payload)

    @property
    def calls(self) -> int:
        return len(self.requests)


def _client(script: _Script) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(script))


def test_success_returns_parsed_result():
    script = _Script((200, STAC_HIT))
    result = search_once(_client(script), STAC_NODE, STAC_REQ, STAC_CFG)
    assert result.ok
    assert result.status_code == 200
    assert result.num_matched == 5
    assert result.has_results
    assert result.data == STAC_HIT
    assert result.elapsed_seconds >= 0.0
    assert script.calls == 1


def test_empty_result_is_ok_but_no_results():
    result = search_once(
        _client(_Script((200, STAC_EMPTY))), STAC_NODE, STAC_REQ, STAC_CFG
    )
    assert result.ok
    assert result.num_matched == 0
    assert result.has_results is False


def test_transient_500_is_retried_then_succeeds():
    script = _Script((500, None), (200, STAC_HIT))
    result = search_once(_client(script), STAC_NODE, STAC_REQ, STAC_CFG, retries=1)
    assert result.ok
    assert script.calls == 2


def test_transient_500_exhausts_retries_and_fails():
    script = _Script((500, None), (500, None), (500, None))
    result = search_once(_client(script), STAC_NODE, STAC_REQ, STAC_CFG, retries=2)
    assert result.ok is False
    assert result.status_code == 500
    assert "server error" in result.error
    assert script.calls == 3


def test_network_error_is_retried():
    script = _Script(httpx.ConnectError("boom"), (200, STAC_HIT))
    result = search_once(_client(script), STAC_NODE, STAC_REQ, STAC_CFG, retries=1)
    assert result.ok
    assert script.calls == 2


def test_network_error_exhausted_reports_no_status():
    script = _Script(httpx.ConnectError("boom"), httpx.ConnectError("boom"))
    result = search_once(_client(script), STAC_NODE, STAC_REQ, STAC_CFG, retries=1)
    assert result.ok is False
    assert result.status_code is None
    assert "request error" in result.error


def test_4xx_is_not_retried():
    script = _Script((400, {"detail": "bad request"}))
    result = search_once(_client(script), STAC_NODE, STAC_REQ, STAC_CFG, retries=3)
    assert result.ok is False
    assert result.status_code == 400
    # A client error is definitive; we do not waste the retry budget on it.
    assert script.calls == 1


def test_solr_uses_get_with_params_at_the_solr_path():
    script = _Script((200, {"response": {"numFound": 42, "docs": [{"id": "d"}]}}))
    result = search_once(_client(script), SOLR_NODE, SOLR_REQ, SOLR_CFG)
    sent = script.requests[0]
    assert sent.method == "GET"
    assert str(sent.url).startswith("https://esgf.ceda.ac.uk/esg-search/search")
    assert "source_id=X" in str(sent.url)
    # Solr count comes from response.numFound.
    assert result.num_matched == 42


def test_stac_uses_post_with_json_body_at_the_search_path():
    script = _Script((200, STAC_HIT))
    search_once(_client(script), STAC_NODE, STAC_REQ, STAC_CFG)
    sent = script.requests[0]
    assert sent.method == "POST"
    assert str(sent.url) == "https://discovery.west.esgf.io/search"
    body = json.loads(sent.content)
    assert body["collections"] == ["CMIP6"]


def test_num_matched_reads_west_context_matched():
    payload = {"type": "FeatureCollection", "context": {"matched": 3}, "features": [{}]}
    result = search_once(
        _client(_Script((200, payload))), STAC_NODE, STAC_REQ, STAC_CFG
    )
    assert result.num_matched == 3
