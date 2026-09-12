"""
Test the ESGF-NG/STAC search API format

STAC answers with CQL2 and describes its facet values in a collection document.
These pin the request we build from facet values and the reading of a collection,
both under the API's own parameter names: the caller (the facade) is assumed to have
already put each property under its collection prefix, and named the collection.

What a *result* means (its bundle id, its project, where this deployment writes the
match count) is the result parsers' business, tested in
`tests/unit/search/test_result_parsers.py`.
"""

from __future__ import annotations

import re

import pytest

from esmporium.search.apis import (
    LimitOutOfRangeError,
    NoFacetValuesReturnedError,
    NoSearchResultDocumentsError,
    SearchAPIESGFNGSTAC,
    UncompilableFacetPatternError,
    UnreadableResponseError,
)
from esmporium.search.apis.esgfng import stac_nodes
from esmporium.search.result_parsing import DataNodeInfo
from esmporium.search.retry import build_transient_retrying


def api() -> SearchAPIESGFNGSTAC:
    """An ESGF-NG/STAC API whose retry policy never sleeps"""
    return SearchAPIESGFNGSTAC("search.example.io", build_transient_retrying(1))


def test_build_search_request_builds_a_cql2_filter():
    """Each facet value becomes an `in` clause; the caller carries the prefix"""
    request = api().build_search_request(
        {"collection": ("CMIP6",), "cmip6:variable_id": ("tas",)}, limit=5
    )

    assert request.method == "POST"
    assert request.path == "/search"
    assert request.params is None
    assert request.json_body["filter-lang"] == "cql2-json"
    assert request.json_body["limit"] == 5
    clauses = request.json_body["filter"]["args"]
    assert {"op": "in", "args": [{"property": "collection"}, ["CMIP6"]]} in clauses
    assert {
        "op": "in",
        "args": [{"property": "cmip6:variable_id"}, ["tas"]],
    } in clauses


def test_build_search_request_with_no_facets_has_no_filter():
    """Nothing to filter on means no filter clause, rather than an empty one"""
    request = api().build_search_request({}, limit=5)

    assert "filter" not in request.json_body


@pytest.mark.parametrize(
    "limit", (pytest.param(0, id="below-the-floor"), pytest.param(10_001, id="above"))
)
def test_build_search_request_refuses_an_impossible_limit(limit):
    with pytest.raises(LimitOutOfRangeError):
        api().build_search_request({}, limit=limit)


def test_build_search_request_accepts_the_ends_of_the_range():
    for limit in (1, 10_000):
        assert api().build_search_request({}, limit=limit).json_body["limit"] == limit


def test_extract_result_documents_reads_the_features():
    """A search answer keeps its records under `features`"""
    features = [{"id": "a"}, {"id": "b"}]

    assert api().extract_result_documents({"features": features}) == features


def test_extract_result_documents_of_an_empty_search_is_empty():
    """A search which matched nothing still answers with a `features` list"""
    assert api().extract_result_documents({"numberMatched": 0, "features": []}) == []


@pytest.mark.parametrize(
    "raw",
    (
        pytest.param({}, id="nothing-we-recognise"),
        pytest.param({"numberMatched": 3}, id="a-count-but-no-features"),
    ),
)
def test_extract_result_documents_without_features_raises(raw):
    """No `features` at all is a response we do not understand, not an empty search"""
    with pytest.raises(
        NoSearchResultDocumentsError,
        match=re.escape(
            "This response does not carry the documents a search answers with. "
            "We expected to read them from 'features'"
        ),
    ):
        api().extract_result_documents(raw)


def test_read_facet_list_reads_a_features_properties():
    """A facet lives in `properties`, under the name the caller asks for"""
    feature = {"properties": {"cmip6:variable_id": "tas", "cmip6:realm": ["atmos"]}}

    assert api().read_facet_list(feature, "cmip6:variable_id") == ("tas",)
    assert api().read_facet_list(feature, "cmip6:realm") == ("atmos",)
    # A facet this feature does not carry is simply absent, which is normal.
    assert api().read_facet_list(feature, "cmip6:grid_label") == ()


def test_read_facet_list_of_a_feature_with_no_properties_raises():
    """A feature with no `properties` is one we cannot read at all"""
    with pytest.raises(
        UnreadableResponseError,
        match=re.escape(
            "This response does not carry the facets of this record "
            "('cmip6:variable_id' in particular). We expected to read the facets "
            "of this record ('cmip6:variable_id' in particular) from "
            "'properties', but 'properties' is not in the response's top level, "
            "there is only: 'id'. This response came from SearchAPIESGFNGSTAC at "
            "https://search.example.io."
        ),
    ):
        api().read_facet_list({"id": "a"}, "cmip6:variable_id")


@pytest.mark.parametrize(
    "value",
    (
        pytest.param({"nested": "value"}, id="a-bare-dict"),
        pytest.param([{"nested": "value"}], id="a-dict-inside-a-list"),
        pytest.param(["tas", {"nested": "value"}], id="one-bad-value-among-good-ones"),
        pytest.param([["tas"]], id="a-nested-list"),
    ),
)
def test_read_facet_list_of_something_we_do_not_recognise_raises(value):
    """Anything else is not stringified into a value that looks real

    As on Solr, each value is checked rather than only the shape around it, so one bad
    value inside an otherwise readable list is caught too.
    """
    with pytest.raises(
        NotImplementedError,
        match=re.escape("We do not know how to read 'cmip6:variable_id'"),
    ):
        api().read_facet_list(
            {"properties": {"cmip6:variable_id": value}}, "cmip6:variable_id"
        )


def test_nodes_of_a_feature_with_no_assets_is_empty():
    """Nothing is hosting a feature with no assets, so it has no nodes"""
    assert stac_nodes({"id": "a"}) == ()


def test_nodes_reads_the_distinct_hosts_of_a_features_assets():
    """Two files on the same node are one node; `href` is deliberately not read"""
    feature = {
        "assets": {
            "one.nc": {
                "alternate:name": "ceda.ac.uk",
                "href": "https://dap.ceda.ac.uk",
            },
            "two.nc": {"alternate:name": "ceda.ac.uk"},
            "three.nc": {"alternate:name": "esgf.nci.org.au"},
        },
        "id": "feature.id.v20220508",
    }

    assert stac_nodes(feature) == (
        DataNodeInfo("ceda.ac.uk"),
        DataNodeInfo("esgf.nci.org.au"),
    )


def test_nodes_of_an_asset_which_does_not_say_where_it_is_hosted_raises():
    """Something is hosting this asset and we cannot see what, so we say so

    Quietly dropping the node would leave us reporting a dataset as available from
    fewer places than it really is.
    """
    feature = {
        "assets": {"one.nc": {"href": "https://dap.ceda.ac.uk"}},
        "id": "feature_id",
    }

    # Asset names are filenames, so they carry dots:
    # the message has to name the asset
    # without pretending its name is a path we walked down.
    with pytest.raises(
        UnreadableResponseError,
        match=re.escape(
            "In the following, `response` refers to the ['assets']['one.nc'] path "
            "in the API response's 'feature_id' feature. "
            "The information provided for the 'one.nc' asset of 'feature_id' "
            "does not specify the data node. "
            "We expected to read the data node from 'alternate:name', "
            "but 'alternate:name' is not in the response's top level, "
            "there is only: 'href'. "
        ),
    ):
        stac_nodes(feature)


def test_nodes_of_a_feature_with_no_id_raises():
    """
    A feature which cannot say what it is is not one we can report on
    """
    feature = {"assets": {"one.nc": {"alternate:name": "ceda.ac.uk"}}}

    with pytest.raises(
        UnreadableResponseError,
        match=re.escape(
            "This response does not carry this record's id. We expected to read "
            "this record's id from 'id', but 'id' is not in the response's top "
            "level, there is only: 'assets'."
        ),
    ):
        stac_nodes(feature)


def test_build_get_facet_values_request_asks_for_the_collection():
    request = api().build_get_facet_values_for_project_request(
        {"cmip6:variable_id"}, "CMIP6"
    )

    assert request.method == "GET"
    assert request.path == "/collections/CMIP6"


STAC_COLLECTION = {
    "summaries": {
        # A list of values, so it is enumerated.
        "cmip6:variable_id": ["tas", "pr"],
        # A pattern (a generated identifier), so it is described, not listed.
        "cmip6:variant_label": "^r\\d+i\\d+p\\d+f\\d+$",
        # Not asked for, so not reported.
        "cmip6:table_id": ["Amon"],
    }
}


def test_parse_facet_values_reads_only_enumerated_lists():
    """A facet summarised as a pattern is left out of the values"""
    res = api().parse_facet_values(
        STAC_COLLECTION, {"cmip6:variable_id", "cmip6:variant_label"}
    )

    assert res == {"cmip6:variable_id": {"tas", "pr"}}


def test_parse_facet_values_without_summaries_raises():
    with pytest.raises(
        NoFacetValuesReturnedError,
        match=re.escape(
            "This response does not report facet values. "
            "We expected to read the facet values from 'summaries', "
            "but the response is empty."
        ),
    ):
        api().parse_facet_values({}, {"cmip6:variable_id"})


# A summary can be a list whose items are not values but pattern objects.
# `cmip6:member_id` really is shaped like this on search.east.esgf.io
# (see the recording in tests/test-data/search/esgf-ng-stac-cmip6-facets.json),
# so this is a real response shape, not a hypothetical one.
# A dict is not hashable, so reading these as values would not merely be wrong,
# it would raise `TypeError` while building the set.
LIST_OF_PATTERNS_COLLECTION = {
    "summaries": {
        "cmip6:member_id": [
            {"pattern": "^r\\d+i\\d+p\\d+f\\d+$"},
            {"pattern": "^s1976-r\\d+i\\d+p\\d+f\\d+$"},
        ],
        "cmip6:variable_id": ["tas", "pr"],
    }
}


def test_parse_facet_values_ignores_non_string_items_in_a_list_summary():
    """A list of pattern objects is not a list of values, so it reports nothing"""
    res = api().parse_facet_values(
        LIST_OF_PATTERNS_COLLECTION, {"cmip6:member_id", "cmip6:variable_id"}
    )

    # Left out entirely rather than reported as an empty set:
    # "we cannot list this one" must stay distinguishable from
    # "this one has no values".
    assert res == {"cmip6:variable_id": {"tas", "pr"}}


def test_parse_facet_values_keeps_the_strings_in_a_mixed_list_summary():
    """A list which mixes values and pattern objects still yields its values"""
    raw = {"summaries": {"cmip6:variable_id": ["tas", {"pattern": "^v.*$"}, "pr"]}}

    assert api().parse_facet_values(raw, {"cmip6:variable_id"}) == {
        "cmip6:variable_id": {"tas", "pr"}
    }


def test_parse_facet_patterns_does_not_read_a_list_of_patterns():
    """
    Test that a list of pattern objects is left out of the patterns too

    `stac_summary_patterns` only reads a summary which is itself a pattern
    string, so this shape falls out of both halves of the parsing.
    That is deliberate for now (nothing we name is summarised this way),
    but it is worth pinning so the day we do want to read it,
    a test says what the current behaviour was.
    """
    res = api().parse_facet_patterns(
        LIST_OF_PATTERNS_COLLECTION, {"cmip6:member_id", "cmip6:variable_id"}
    )

    assert res == {}


def test_parse_facet_patterns_reads_only_the_patterns():
    res = api().parse_facet_patterns(
        STAC_COLLECTION, {"cmip6:variable_id", "cmip6:variant_label"}
    )

    assert set(res) == {"cmip6:variant_label"}
    assert res["cmip6:variant_label"].fullmatch("r1i1p1f1")
    assert not res["cmip6:variant_label"].fullmatch("r1i1pf1")


def test_parse_facet_patterns_without_summaries_raises():
    """
    Test that the patterns half is as loud as the values half

    A collection which describes no facet at all cannot tell us anything about
    the ones we asked for, whether we asked for values or for patterns,
    so both halves raise rather than quietly reporting nothing.
    """
    with pytest.raises(
        NoFacetValuesReturnedError,
        match=re.escape(
            "This response does not report facet values. "
            "We expected to read the facet values from 'summaries', "
            "but the response is empty."
        ),
    ):
        api().parse_facet_patterns({}, {"cmip6:variant_label"})


def test_parse_facet_patterns_of_an_uncompilable_pattern_raises():
    raw = {"summaries": {"cmip6:variant_label": "^r(\\d+$"}}

    with pytest.raises(UncompilableFacetPatternError, match="variant_label"):
        api().parse_facet_patterns(raw, {"cmip6:variant_label"})
