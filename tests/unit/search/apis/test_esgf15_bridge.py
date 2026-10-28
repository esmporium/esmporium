"""
Test the ESGF1.5 bridge/Solr search API format
"""

from __future__ import annotations

import re

import pytest

from esmporium.search.apis import (
    LimitOutOfRangeError,
    NoFacetValuesReturnedError,
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


def test_next_page_request_advances_the_offset_like_solr():
    """The bridge pages by offset too, carrying its own comma-OR params along"""
    request = api().build_search_request({"variable_id": ("tas",)}, limit=10)
    raw = {"response": {"numFound": 25, "docs": []}}

    nxt = api().next_page_request(request, raw)

    assert nxt is not None
    assert nxt.params["offset"] == 10
    assert nxt.path == "/esgf-1-5-bridge/"
    assert nxt.params["variable_id"] == "tas"

    # ...and stops once the offset reaches the total.
    assert api().next_page_request(nxt, raw).params["offset"] == 20
    assert api().next_page_request(api().next_page_request(nxt, raw), raw) is None


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
    with pytest.raises(
        NoFacetValuesReturnedError,
        match=re.escape(
            "This response does not report facet values. "
            "We expected to read the facet values from "
            "'facet_counts.facet_fields', "
            "but the response is empty."
        ),
    ):
        api().parse_facet_values({}, {"variable_id"})


def test_parse_facet_patterns_is_always_empty():
    assert api().parse_facet_patterns({}, {"variant_label"}) == {}


def test_n_matches_reads_the_count_the_solr_way():
    """The bridge is Solr-shaped, so its total is at `response.numFound` too"""
    raw = {"response": {"numFound": 25, "docs": []}}

    assert api().get_n_matches(raw) == 25


def test_read_facet_list_reads_the_solr_way():
    """The bridge is Solr-shaped, so its facet lists read like ESGF1's too

    The shapes Solr writes are covered in detail against ESGF1; what matters here
    is that the bridge reads them the same way rather than inventing its own.
    """
    assert api().read_facet_list({"variable_id": ["tas", "pr"]}, "variable_id") == (
        "tas",
        "pr",
    )
    assert api().read_facet_list({"variable_id": "tas"}, "variable_id") == ("tas",)
    assert api().read_facet_list({}, "variable_id") == ()


def test_build_search_request_asks_for_dataset_documents():
    """Stated rather than left to the default, as on classic Solr"""
    request = api().build_search_request({"variable_id": ("tas",)}, limit=25)

    assert request.params["type"] == "Dataset"


def test_build_file_search_request_joins_the_dataset_ids_on_a_comma():
    """
    This API ORs on a comma, and silently keeps only *one* value of a repeated parameter

    Which makes joining the classic way worse than an error: asking this API about
    three datasets with repeated parameters returns an answer about one of them, with
    nothing in the response to say so. Verified live -- the same two-value OR gives
    281,864 matches repeated and 938,500 comma-joined.
    """
    request = api().build_file_search_request(
        ["CMIP6.a.v1|esgf.ceda.ac.uk", "CMIP6.b.v1|esgf.nci.org.au"], limit=25
    )

    assert request.params["type"] == "File"
    assert request.path == "/esgf-1-5-bridge/"
    assert request.params["dataset_id"] == (
        "CMIP6.a.v1|esgf.ceda.ac.uk,CMIP6.b.v1|esgf.nci.org.au"
    )


def test_build_file_search_request_joins_its_facets_on_a_comma_too():
    """The same dialect applies to the variable narrowing CMIP5 needs"""
    request = api().build_file_search_request(
        ["cmip5.a.v1|esgf.ceda.ac.uk"],
        limit=25,
        facet_values={"variable": ("tas", "pr")},
    )

    assert request.params["variable"] == "tas,pr"
