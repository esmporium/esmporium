"""
Test the ESGF-NG/STAC search API format

STAC answers with CQL2 and describes its facet values in a collection document.
These pin the request we build from facet values and the reading of a collection,
both under the API's own parameter names: the caller (the facade) is assumed to have
already put each property under its collection prefix, and named the collection.
"""

from __future__ import annotations

import pytest

from esmporium.search.apis import (
    LimitOutOfRangeError,
    NoFacetValuesReturnedError,
    NoSearchResultNumberOfMatchesReturnedError,
    SearchAPIESGFNGSTAC,
    UncompilableFacetPatternError,
)
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


@pytest.mark.parametrize(
    "raw, exp",
    (
        pytest.param({"numberMatched": 7}, 7, id="stac-spelling"),
        pytest.param({"numMatched": 0}, 0, id="west-spelling"),
        pytest.param({"context": {"matched": 4}}, 4, id="west-context"),
    ),
)
def test_get_search_result_n_matches_reads_whichever_spelling_is_present(raw, exp):
    """The two deployments disagree on where the total lives; we read either"""
    assert api().get_search_result_n_matches(raw) == exp


@pytest.mark.parametrize(
    "raw",
    (
        pytest.param({}, id="nothing-we-recognise"),
        pytest.param({"features": [{"id": "a"}]}, id="records-but-no-count"),
    ),
)
def test_get_search_result_n_matches_with_no_count_raises(raw):
    with pytest.raises(NoSearchResultNumberOfMatchesReturnedError, match="pinme"):
        api().get_search_result_n_matches(raw)


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
    with pytest.raises(NoFacetValuesReturnedError, match="pinme"):
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
    with pytest.raises(NoFacetValuesReturnedError, match="pinme"):
        api().parse_facet_patterns({}, {"cmip6:variant_label"})


def test_parse_facet_patterns_of_an_uncompilable_pattern_raises():
    raw = {"summaries": {"cmip6:variant_label": "^r(\\d+$"}}

    with pytest.raises(UncompilableFacetPatternError, match="variant_label"):
        api().parse_facet_patterns(raw, {"cmip6:variant_label"})
