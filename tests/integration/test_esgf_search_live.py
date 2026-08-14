"""
Live integration tests for the whole search pipeline, against real ESGF nodes.

These exercise ``search`` end-to-end: query -> per-generation request -> live HTTP
-> retry/fallback -> combined ``{project: raw_json}``. They hit the network, so
every test is marked ``SearchESGF`` and skipped by the default run; opt in with
``pytest -m SearchESGF``.

Two standing caveats, by design of this step:

- We only *roughly* parse the returned JSON (counts, key/field presence). Proper
  parsing into dataset objects is a later PR; several tests below are marked with a
  note that their shape will change when that lands.
- ESGF1 (Solr) endpoints are 501 across the federation. Tests that need a live
  ESGF1 response skip themselves when every ESGF1 call fails, rather than fail.
"""

from __future__ import annotations

from typing import Any

import pytest

from esmporium.esgf import (
    ESGFQuery,
    ESGFQueryCMIP5,
    ESGFQueryCMIP6,
    ESGFQueryCMIP7,
)
from esmporium.esgf.query_models import _ESGFQueryBase
from esmporium.esgf.search import (
    FakeRecorder,
    IndexNode,
    SearchAPIGeneration,
    search,
)

pytestmark = pytest.mark.SearchESGF

EAST = IndexNode(
    host="api.stac.esgf.ceda.ac.uk", generation=SearchAPIGeneration.ESGF_NG_EAST
)
WEST = IndexNode(
    host="discovery.west.esgf.io", generation=SearchAPIGeneration.ESGF_NG_WEST
)
ESGF1 = IndexNode(host="esgf.ceda.ac.uk", generation=SearchAPIGeneration.ESGF1)

NG_NODES = (EAST, WEST)


def _matched(payload: dict[str, Any] | None) -> int:
    """Roughly read the matched count across STAC and Solr payloads (0 if absent)."""
    if payload is None:
        return 0
    for key in ("numberMatched", "numMatched"):
        if key in payload:
            return int(payload[key])
    context = payload.get("context")
    if isinstance(context, dict) and "matched" in context:
        return int(context["matched"])
    response = payload.get("response")
    if isinstance(response, dict) and "numFound" in response:
        return int(response["numFound"])
    return 0


def _run(
    query: _ESGFQueryBase, nodes: tuple[IndexNode, ...]
) -> tuple[dict[str, Any], FakeRecorder]:
    """Run a search over the nodes with retries, returning the result and stats."""
    recorder = FakeRecorder()
    result = search(query, nodes=nodes, retries=2, recorder=recorder)
    return result, recorder


def _skip_if_endpoints_dead(recorder: FakeRecorder) -> None:
    """Skip when calls were made but all failed (e.g. ESGF1 is 501 everywhere)."""
    if recorder.stats and not any(stat.ok for stat in recorder.stats):
        pytest.skip("target endpoint(s) not answering — all calls failed")


# ---------------------------------------------------- reachable happy path (NG)


def test_cmip6_on_ng_returns_results_we_can_roughly_parse():
    result, _ = _run(ESGFQueryCMIP6(source_id="UKESM1-0-LL"), NG_NODES)
    payload = result["cmip6"]
    assert payload is not None
    assert _matched(payload) > 0
    features = payload.get("features") or []
    assert features, "expected at least one returned feature"
    assert features[0]["properties"]["cmip6:source_id"] == "UKESM1-0-LL"


def test_cmip7_on_ng_west_does_not_error():
    # West carries a CMIP7 collection; this should return a dict without raising,
    # whether or not the specific query matches anything.
    result, _ = _run(ESGFQueryCMIP7(source_id="UKESM1-0-LL"), (WEST,))
    assert "cmip7" in result


# ----------------------------------------- cross combinations: no error, no data


def test_cmip5_on_ng_is_empty_not_an_error():
    result, _ = _run(ESGFQueryCMIP5(model="ACCESS1-0"), NG_NODES)
    # No CMIP5 on ESGF-NG -> unrepresentable/absent -> no results, but no error.
    assert result == {"cmip5": None}


def test_cmip7_on_esgf1_is_empty_not_an_error():
    # CMIP7 cannot be expressed on Solr, so the node is skipped: no call, no error,
    # no results. Works whether or not ESGF1 is live.
    result, recorder = _run(ESGFQueryCMIP7(source_id="UKESM1-0-LL"), (ESGF1,))
    assert result == {"cmip7": None}
    assert recorder.stats == []


@pytest.mark.parametrize(
    "query",
    [
        pytest.param(ESGFQueryCMIP6(source_id="UKESM1-0-LL"), id="cmip6"),
        pytest.param(ESGFQueryCMIP5(model="ACCESS1-0"), id="cmip5"),
        pytest.param(ESGFQueryCMIP7(source_id="UKESM1-0-LL"), id="cmip7"),
        pytest.param(
            ESGFQuery(experiment="historical", project=("CMIP6",)), id="unified"
        ),
    ],
)
def test_every_query_class_hits_ng_without_error(query: _ESGFQueryBase):
    result, _ = _run(query, NG_NODES)
    assert isinstance(result, dict)
    assert set(result) == {p.lower() for p in query.project}


def test_complex_unified_query_splits_and_combines():
    """The most complex case: one ESGFQuery over CMIP5+CMIP6+CMIP7.

    NOTE: this returns a raw ``{project: json}`` dict as an interim shape. A later
    PR returns parsed Dataset objects and merges across nodes; this test changes then.
    """
    query = ESGFQuery(experiment="historical", project=("CMIP5", "CMIP6", "CMIP7"))
    result, _ = _run(query, (*NG_NODES, ESGF1))
    assert set(result) == {"cmip5", "cmip6", "cmip7"}
    # CMIP6 historical is plentiful on ESGF-NG.
    assert _matched(result["cmip6"]) > 0
    # CMIP5 comes only from the ESGF1 (Solr) side: it is always a key in the
    # combined dict, with data when esg-search is up and None when it is not.
    assert "cmip5" in result


# ------------------------------------------- ESGF AND/OR behaviour (ESGF's own)


@pytest.mark.parametrize(
    "node", [pytest.param(EAST, id="east"), pytest.param(WEST, id="west")]
)
def test_or_within_a_facet_does_not_narrow(node: IndexNode):
    """Two OR-ed values match at least as many datasets as one.

    NOTE: interim raw-JSON shape; this test changes when we return Dataset objects.
    """
    one, _ = _run(ESGFQuery(variable=("tas",), project=("CMIP6",)), (node,))
    two, _ = _run(ESGFQuery(variable=("tas", "pr"), project=("CMIP6",)), (node,))
    assert _matched(one["cmip6"]) > 0
    assert _matched(two["cmip6"]) >= _matched(one["cmip6"])


@pytest.mark.parametrize(
    "node", [pytest.param(EAST, id="east"), pytest.param(WEST, id="west")]
)
def test_and_across_facets_does_not_widen(node: IndexNode):
    """Adding an AND-ed facet matches no more datasets than without it.

    NOTE: interim raw-JSON shape; this test changes when we return Dataset objects.
    """
    one, _ = _run(ESGFQuery(experiment=("historical",), project=("CMIP6",)), (node,))
    two, _ = _run(
        ESGFQuery(experiment=("historical",), variable=("tas",), project=("CMIP6",)),
        (node,),
    )
    assert _matched(one["cmip6"]) > 0
    assert _matched(two["cmip6"]) <= _matched(one["cmip6"])


def test_and_or_behaviour_on_esgf1_if_live():
    """Same AND/OR checks on ESGF1 (Solr), skipped while esg-search is 501.

    NOTE: interim raw-JSON shape; this test changes when we return Dataset objects.
    """
    one, rec_one = _run(ESGFQuery(variable=("tas",), project=("CMIP6",)), (ESGF1,))
    _skip_if_endpoints_dead(rec_one)
    two, rec_two = _run(ESGFQuery(variable=("tas", "pr"), project=("CMIP6",)), (ESGF1,))
    _skip_if_endpoints_dead(rec_two)
    assert _matched(one["cmip6"]) > 0
    assert _matched(two["cmip6"]) >= _matched(one["cmip6"])
