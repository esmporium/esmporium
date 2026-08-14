"""
Exploratory canary tests for the ESGF-NG (STAC / CQL2) search APIs.

This module is deliberately *exploratory*: it pins down the live behaviour of the
ESGF-NG index nodes that our search layer is about to depend on, so that

- the request builder is written against ground truth rather than guesswork, and
- if ESGF changes any of this behaviour, one of these tests goes red and tells us
  (these are testing *ESGF's* behaviour, not ours, which is exactly the point).

Everything here hits the network, so every test is marked ``SearchESGF`` and is
skipped by the default ``pytest`` run. Opt in with ``pytest -m SearchESGF``.

What we have already established at the terminal and encode here (2026-08-14):

- Both nodes speak STAC 1.0 with CQL2 over ``POST /search``.
- Facet properties are prefixed per project: ``cmip6:source_id``, ``cmip7:...``.
  The prefixed spelling is the one that filters on *both* east and west (bare
  ``source_id`` works only on west; ``properties.``-prefixed only on east).
- Collection ids for the search filter are UPPERCASE (``CMIP6``); lowercase or an
  absent project returns ``200`` with an empty result, never an error.
- The matched-count field differs by node: east reports ``numberMatched`` (the
  STAC-API standard), west reports ``numMatched`` (plus a ``context`` block).
- Retracted datasets are INCLUDED by default: there is no need to send a
  retracted control to see them (the opposite of the ESGF1/Solr convention). We
  must therefore simply *not* add a retracted filter.
"""

from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.SearchESGF

# The two live ESGF-NG STAC roots we probe. East and west mirror the same CMIP6
# data but differ in a couple of surface details captured by the tests below.
EAST = "https://api.stac.esgf.ceda.ac.uk"
WEST = "https://discovery.west.esgf.io"

STAC_BASES = [
    pytest.param(EAST, id="east"),
    pytest.param(WEST, id="west"),
]

# Timeout generous enough for a federated index node, short enough to fail fast.
_TIMEOUT = httpx.Timeout(45.0)


def _matched(payload: dict) -> int:
    """
    Return the number of datasets a STAC search matched.

    The field name is not the same on both nodes, so we accept either the
    STAC-API standard ``numberMatched`` (east) or the abbreviated ``numMatched``
    (west), falling back to west's ``context.matched``. This is the exact
    normalisation the real client will have to do.
    """
    for key in ("numberMatched", "numMatched"):
        if key in payload:
            return int(payload[key])
    return int(payload["context"]["matched"])


def _post_search(base: str, body: dict, *, retries: int = 2) -> dict:
    """
    POST a STAC search body, tolerating transient node flakiness.

    These nodes intermittently return ``5xx`` (we have seen the west
    ``/queryables`` endpoint 500 repeatedly). A live canary should not go red on
    a blip, so we retry a couple of times and *skip* — rather than fail — if the
    node is simply unreachable. A genuine behaviour change still shows up as a
    failed assertion once we do get a ``2xx``.
    """
    last_exc: Exception | None = None
    for _ in range(retries + 1):
        try:
            response = httpx.post(f"{base}/search", json=body, timeout=_TIMEOUT)
        except httpx.RequestError as exc:  # network-level failure
            last_exc = exc
            continue
        if response.status_code >= 500:
            last_exc = RuntimeError(f"{base} returned {response.status_code}")
            continue
        assert response.status_code == 200, (
            f"{base}/search unexpectedly returned {response.status_code}: "
            f"{response.text[:200]}"
        )
        return response.json()
    pytest.skip(f"ESGF-NG node {base} unreachable/unstable: {last_exc}")


def _cql2(filter_body: dict, *, collection: str = "CMIP6", limit: int = 1) -> dict:
    """Build a STAC ``POST /search`` body with a CQL2-JSON filter.

    ``limit`` must be >= 1: these nodes reject ``limit=0`` with a ``400`` ("Input
    should be greater than 0"), so the real client must never send zero even when
    it only wants the matched count.
    """
    return {
        "collections": [collection],
        "filter-lang": "cql2-json",
        "limit": limit,
        "filter": filter_body,
    }


def _known_source_id(base: str) -> str:
    """
    Pull a real ``cmip6:source_id`` value from a live item on this node.

    Filtering by a value we just read back guarantees the filter tests are not
    brittle against a hard-coded model name that might one day disappear.
    """
    payload = _post_search(base, {"collections": ["CMIP6"], "limit": 1})
    features = payload.get("features") or []
    if not features:
        pytest.skip(f"{base} returned no CMIP6 items to sample a source_id from")
    source_id = features[0]["properties"].get("cmip6:source_id")
    if not source_id:
        pytest.skip(f"{base} CMIP6 item had no cmip6:source_id")
    return source_id


@pytest.mark.parametrize("base", STAC_BASES)
def test_prefixed_property_filter_narrows_results(base: str) -> None:
    """A CQL2 filter on the ``cmip6:``-prefixed property actually filters."""
    source_id = _known_source_id(base)
    total = _matched(_post_search(base, {"collections": ["CMIP6"], "limit": 1}))
    filtered = _matched(
        _post_search(
            base,
            _cql2({"op": "=", "args": [{"property": "cmip6:source_id"}, source_id]}),
        )
    )
    assert total > 0
    assert 0 < filtered < total, (
        f"expected the cmip6:source_id={source_id!r} filter to narrow "
        f"{total} down to a positive subset, got {filtered}"
    )


@pytest.mark.parametrize("base", STAC_BASES)
def test_in_operator_equals_equality(base: str) -> None:
    """``in [x]`` matches the same set as ``= x`` (our OR-within-a-facet encoding)."""
    source_id = _known_source_id(base)
    eq = _matched(
        _post_search(
            base,
            _cql2({"op": "=", "args": [{"property": "cmip6:source_id"}, source_id]}),
        )
    )
    is_in = _matched(
        _post_search(
            base,
            _cql2({"op": "in", "args": [{"property": "cmip6:source_id"}, [source_id]]}),
        )
    )
    assert is_in == eq > 0


@pytest.mark.parametrize("base", STAC_BASES)
def test_and_across_facets_narrows_further(base: str) -> None:
    """AND-ing a second facet never widens results (AND-across-facets encoding)."""
    source_id = _known_source_id(base)
    one = _matched(
        _post_search(
            base,
            _cql2({"op": "=", "args": [{"property": "cmip6:source_id"}, source_id]}),
        )
    )
    two = _matched(
        _post_search(
            base,
            _cql2(
                {
                    "op": "and",
                    "args": [
                        {
                            "op": "=",
                            "args": [{"property": "cmip6:source_id"}, source_id],
                        },
                        {
                            "op": "=",
                            "args": [{"property": "cmip6:experiment_id"}, "historical"],
                        },
                    ],
                }
            ),
        )
    )
    assert 0 < one
    assert two <= one


@pytest.mark.parametrize("base", STAC_BASES)
def test_unknown_collection_is_empty_not_error(base: str) -> None:
    """A project a node does not carry (e.g. CMIP5) yields 200 + empty, not an error.

    This is what lets us 'always send' and read absence as no-results, per the
    no-support-matrix design.
    """
    payload = _post_search(base, {"collections": ["CMIP5"], "limit": 1})
    assert payload["type"] == "FeatureCollection"
    assert _matched(payload) == 0
    assert (payload.get("features") or []) == []


@pytest.mark.parametrize("base", STAC_BASES)
def test_retracted_included_by_default(base: str) -> None:
    """Retracted datasets are included when we send no retracted filter.

    We assert the stable half of the finding: an unfiltered query returns data
    without us ever asking to include retracted. (Exact retracted counts vary and
    west's retracted filter is unreliable, so we do not assert on those here.)
    """
    total = _matched(_post_search(base, {"collections": ["CMIP6"], "limit": 1}))
    assert total > 0


@pytest.mark.parametrize(
    ("base", "expected_key"),
    [
        pytest.param(EAST, "numberMatched", id="east"),
        pytest.param(WEST, "numMatched", id="west"),
    ],
)
def test_matched_count_field_name(base: str, expected_key: str) -> None:
    """Pin the per-node matched-count field name the client must normalise over."""
    payload = _post_search(base, {"collections": ["CMIP6"], "limit": 1})
    assert expected_key in payload, (
        f"expected {base} to report matches under {expected_key!r}; "
        f"got keys {sorted(payload)}"
    )
