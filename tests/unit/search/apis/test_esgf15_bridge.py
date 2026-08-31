"""
Test the ESGF1.5 bridge/Solr search API wire format

The facet names are ESGF1's, and the response shape is Solr's, so the reading half
is shared with `test_esgf1.py`. What differs is the encoding of a search request,
which is what these pin. Facet values and names are in the API's own vocabulary;
the canonical translation is the facade's job and is tested elsewhere.
"""

from __future__ import annotations

import pytest

from esmporium.search.apis import (
    LimitOutOfRangeError,
    NoFacetValuesReturned,
    SearchAPIESGF15BridgeSolr,
)
from esmporium.search.retry import build_transient_retrying


def api() -> SearchAPIESGF15BridgeSolr:
    """An ESGF1.5 bridge API whose retry policy never sleeps"""
    return SearchAPIESGF15BridgeSolr("node.example", build_transient_retrying(1))


def test_build_search_request_ors_on_a_comma():
    """This API ORs values on a comma, not on a repeated parameter"""
    request = api().build_search_request(
        {"experiment_id": ("historical", "ssp126"), "variable_id": ("tas",)}, limit=25
    )

    assert request.method == "GET"
    assert request.path == "/esgf-1-5-bridge/"
    assert request.params["experiment_id"] == "historical,ssp126"
    assert request.params["variable_id"] == "tas"
    assert request.params["limit"] == 25


@pytest.mark.parametrize(
    "limit", (pytest.param(-1, id="below-the-floor"), pytest.param(10_001, id="above"))
)
def test_build_search_request_refuses_an_impossible_limit(limit):
    with pytest.raises(LimitOutOfRangeError):
        api().build_search_request({}, limit=limit)


def test_get_search_result_n_matches_reads_the_solr_count():
    assert api().get_search_result_n_matches({"response": {"numFound": 17}}) == 17


def test_build_get_facet_values_request_names_the_facets_sorted():
    request = api().build_get_facet_values_for_project_request(
        {"variable_id", "experiment_id"}, "CMIP6"
    )

    assert request.params["facets"] == "experiment_id,variable_id"
    assert request.params["project"] == "CMIP6"
    # No distrib on the bridge; it is not a federated sweep.
    assert "distrib" not in request.params


def test_parse_facet_values_reads_the_solr_shape():
    raw = {"facet_counts": {"facet_fields": {"variable_id": ["tas", 9]}}}

    assert api().parse_facet_values(raw, {"variable_id"}) == {"variable_id": {"tas"}}


def test_parse_facet_values_with_nothing_to_read_raises():
    with pytest.raises(NoFacetValuesReturned):
        api().parse_facet_values({}, {"variable_id"})


def test_parse_facet_patterns_is_always_empty():
    assert api().parse_facet_patterns({}, {"variant_label"}) == {}
