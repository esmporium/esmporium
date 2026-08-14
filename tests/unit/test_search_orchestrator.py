"""Unit tests for the search orchestrator (split / fallback / record / combine).

All network is faked with ``httpx.MockTransport`` via an injected client; nothing
here touches ESGF.
"""

import httpx
import pytest

from esmporium.esgf import ESGFQuery, ESGFQueryCMIP5, ESGFQueryCMIP6
from esmporium.esgf.search import (
    FakeRecorder,
    IndexNode,
    NoProjectToSearchError,
    SearchAPIGeneration,
    search,
)

STAC_NODE = IndexNode(host="stac.test", generation=SearchAPIGeneration.ESGF_NG_WEST)
SOLR_NODE = IndexNode(host="solr.test", generation=SearchAPIGeneration.ESGF1)

STAC_HIT = {"type": "FeatureCollection", "numberMatched": 3, "features": [{"id": "s"}]}
STAC_EMPTY = {"type": "FeatureCollection", "numberMatched": 0, "features": []}
SOLR_HIT = {"response": {"numFound": 2, "docs": [{"id": "d"}]}}


class _Router:
    """MockTransport handler; plays a per-host script, empty for unlisted hosts."""

    def __init__(self, by_host):
        self.by_host = {host: list(actions) for host, actions in by_host.items()}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        actions = self.by_host.get(request.url.host, [])
        action = actions.pop(0) if actions else (200, STAC_EMPTY)
        if isinstance(action, Exception):
            raise action
        status, payload = action
        return httpx.Response(status, json=payload)


def _client(router: _Router) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(router))


def test_no_project_raises():
    with pytest.raises(NoProjectToSearchError):
        search(ESGFQuery(model="X"))  # unified skin defaults project to ()


def test_first_node_with_results_wins_and_is_recorded():
    router = _Router({"stac.test": [(200, STAC_HIT)]})
    recorder = FakeRecorder()
    result = search(
        ESGFQueryCMIP6(source_id="ACCESS-CM2"),
        nodes=(STAC_NODE,),
        client=_client(router),
        recorder=recorder,
    )
    assert result == {"cmip6": STAC_HIT}
    assert len(recorder.stats) == 1
    stat = recorder.stats[0]
    assert stat.host == "stac.test"
    assert stat.generation is SearchAPIGeneration.ESGF_NG_WEST
    assert stat.project == "CMIP6"
    assert stat.ok and stat.num_matched == 3


def test_advances_to_next_node_on_no_results():
    router = _Router({"stac.test": [(200, STAC_EMPTY)], "solr.test": [(200, SOLR_HIT)]})
    recorder = FakeRecorder()
    result = search(
        ESGFQueryCMIP6(source_id="ACCESS-CM2"),
        nodes=(STAC_NODE, SOLR_NODE),
        client=_client(router),
        recorder=recorder,
    )
    assert result == {"cmip6": SOLR_HIT}
    # One stat per node actually called, in order.
    assert [s.host for s in recorder.stats] == ["stac.test", "solr.test"]
    assert recorder.stats[0].num_matched == 0
    assert recorder.stats[1].num_matched == 2


def test_no_node_has_results_yields_none():
    router = _Router(
        {
            "stac.test": [(200, STAC_EMPTY)],
            "solr.test": [(200, {"response": {"numFound": 0, "docs": []}})],
        }
    )
    recorder = FakeRecorder()
    result = search(
        ESGFQueryCMIP6(source_id="ACCESS-CM2"),
        nodes=(STAC_NODE, SOLR_NODE),
        client=_client(router),
        recorder=recorder,
    )
    assert result == {"cmip6": None}
    assert len(recorder.stats) == 2


def test_unrepresentable_node_is_skipped_without_a_call_or_stat():
    # CMIP5 cannot be expressed on STAC, so that node is skipped entirely (no HTTP
    # call, no stat) and only the Solr node is tried and recorded.
    router = _Router({"solr.test": [(200, SOLR_HIT)]})
    recorder = FakeRecorder()
    result = search(
        ESGFQueryCMIP5(model="ACCESS1-0"),
        nodes=(STAC_NODE, SOLR_NODE),
        client=_client(router),
        recorder=recorder,
    )
    assert result == {"cmip5": SOLR_HIT}
    assert [s.host for s in recorder.stats] == ["solr.test"]
    assert not any(r.url.host == "stac.test" for r in router.requests)


def test_multi_project_query_is_split_and_combined():
    hit_c6 = {
        "type": "FeatureCollection",
        "numberMatched": 1,
        "features": [{"id": "c6"}],
    }
    hit_c7 = {
        "type": "FeatureCollection",
        "numberMatched": 1,
        "features": [{"id": "c7"}],
    }
    router = _Router({"stac.test": [(200, hit_c6), (200, hit_c7)]})
    result = search(
        ESGFQuery(model="X", project=("CMIP6", "CMIP7")),
        nodes=(STAC_NODE,),
        client=_client(router),
    )
    assert result == {"cmip6": hit_c6, "cmip7": hit_c7}


def test_retries_are_passed_through_to_the_call():
    # A 500 then 200 on the same node succeeds only because retries>0 is honoured.
    router = _Router({"stac.test": [(500, STAC_EMPTY), (200, STAC_HIT)]})
    recorder = FakeRecorder()
    result = search(
        ESGFQueryCMIP6(source_id="ACCESS-CM2"),
        nodes=(STAC_NODE,),
        retries=1,
        client=_client(router),
        recorder=recorder,
    )
    assert result == {"cmip6": STAC_HIT}
    assert len(router.requests) == 2  # the retry happened
    assert len(recorder.stats) == 1  # but it is one logical call


def test_default_recorder_does_not_error():
    router = _Router({"stac.test": [(200, STAC_HIT)]})
    result = search(
        ESGFQueryCMIP6(source_id="ACCESS-CM2"),
        nodes=(STAC_NODE,),
        client=_client(router),
    )
    assert result == {"cmip6": STAC_HIT}
