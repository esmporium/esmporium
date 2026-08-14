"""
Live canary: does our ``extra_facets`` prefixing produce a *real* STAC queryable?

The STAC request builder namespaces a passthrough facet with the project prefix
(``sub_experiment_id`` -> ``cmip6:sub_experiment_id``). That is a heuristic: it is
only useful if the resulting property is one ESGF-NG actually filters on. This test
proves it does, end-to-end through ``search`` and against live nodes.

It is deliberately kept in its own file: the ``extra_facets`` prefixing is a
best-effort convenience, and if we later decide not to rely on it, this file can be
deleted without touching the core live suite. Marked ``SearchESGF`` (opt-in only).
"""

from __future__ import annotations

from typing import Any

import pytest

from esmporium.esgf import ESGFQueryCMIP6
from esmporium.esgf.query_models import _ESGFQueryBase
from esmporium.esgf.search import (
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
NG_NODES = (EAST, WEST)

_BOGUS = "definitely-not-a-real-sub-experiment"


def _matched(payload: dict[str, Any] | None) -> int:
    """Roughly read the matched count from a STAC payload (0 if none/absent)."""
    if payload is None:
        return 0
    for key in ("numberMatched", "numMatched"):
        if key in payload:
            return int(payload[key])
    return int(payload.get("context", {}).get("matched", 0))


def _run(query: _ESGFQueryBase) -> dict[str, Any]:
    """Search the NG nodes with a couple of retries."""
    return search(query, nodes=NG_NODES, retries=2)


def test_stac_extra_facet_prefixing_actually_filters():
    # 1. Read a real (source_id, sub_experiment_id) pair off a live CMIP6 item.
    baseline = _run(ESGFQueryCMIP6(source_id="UKESM1-0-LL"))
    payload = baseline["cmip6"]
    features = (payload or {}).get("features") or []
    if not features:
        pytest.skip("no live CMIP6 items available to sample from")
    props = features[0]["properties"]
    source_id = props["cmip6:source_id"]
    real_sub = props["cmip6:sub_experiment_id"]
    baseline_count = _matched(payload)

    # 2. The SAME query plus the real sub_experiment_id (a passthrough facet, so it
    #    exercises the prefixing path) still matches, and never widens the baseline.
    real = _run(
        ESGFQueryCMIP6(source_id=source_id, other_terms={"sub_experiment_id": real_sub})
    )
    assert 0 < _matched(real["cmip6"]) <= baseline_count

    # 3. A bogus value must drive the count to zero. If the prefixed property were
    #    NOT a real queryable, ESGF would ignore the clause and return the baseline
    #    instead — so this is what proves the filter is genuinely applied.
    bogus = _run(
        ESGFQueryCMIP6(source_id=source_id, other_terms={"sub_experiment_id": _BOGUS})
    )
    assert _matched(bogus["cmip6"]) == 0
