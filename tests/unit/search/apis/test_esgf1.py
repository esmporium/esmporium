"""
Test the ESGF1/Solr search API format

These never touch the network. They pin the two halves of the API separately:
given facet values, the request we build; given a response, what we read out of it.
The facet values and facet names here are already the API parameter names,
because translating canonical names into them is the facade's job, not this
layer's (that translation is tested in `tests/unit/search/test_facade.py`).
"""

from __future__ import annotations

import re

import pytest

from esmporium.search.apis import (
    LimitOutOfRangeError,
    NoFacetValuesReturnedError,
    NoSearchResultNumberOfMatchesReturnedError,
    SearchAPIESGF1Solr,
)
from esmporium.search.retry import build_transient_retrying


def api(**kwargs) -> SearchAPIESGF1Solr:
    """An ESGF1/Solr API whose retry policy never sleeps"""
    return SearchAPIESGF1Solr("node.example", build_transient_retrying(1), **kwargs)


def test_build_search_request_renders_facet_values():
    """A multi-value facet becomes a repeated parameter, which is how Solr ORs"""
    request = api().build_search_request(
        {"experiment_id": ("historical", "ssp126"), "variable_id": ("tas",)}, limit=25
    )

    assert request.method == "GET"
    assert request.path == "/esg-search/search"
    assert request.json_body is None
    assert request.params["format"] == "application/solr+json"
    assert request.params["limit"] == 25
    assert request.params["distrib"] == "true"
    assert request.params["experiment_id"] == ["historical", "ssp126"]
    assert request.params["variable_id"] == ["tas"]


@pytest.mark.parametrize(
    "distrib, exp",
    (pytest.param(True, "true", id="sweep"), pytest.param(False, "false", id="local")),
)
def test_distrib_is_configurable(distrib, exp):
    """Both requests carry the caller's choice of whether to sweep the federation"""
    built = api(distrib=distrib)
    assert built.build_search_request({}, limit=1).params["distrib"] == exp
    assert (
        built.build_get_facet_values_for_project_request({"variable"}, "CMIP6").params[
            "distrib"
        ]
        == exp
    )


@pytest.mark.parametrize(
    "limit", (pytest.param(-1, id="below-the-floor"), pytest.param(10_001, id="above"))
)
def test_build_search_request_refuses_an_impossible_limit(limit):
    """A page size no API will honour is refused, rather than quietly clamped"""
    with pytest.raises(LimitOutOfRangeError):
        api().build_search_request({}, limit=limit)


def test_build_search_request_accepts_the_ends_of_the_range():
    """The ends of the accepted range are themselves accepted"""
    for limit in (0, 10_000):
        assert api().build_search_request({}, limit=limit).params["limit"] == limit


@pytest.mark.parametrize(
    "raw, exp",
    (
        pytest.param({"response": {"numFound": 3, "docs": []}}, 3, id="a-count"),
        pytest.param({"response": {"numFound": 0, "docs": []}}, 0, id="no-matches"),
    ),
)
def test_get_search_result_n_matches(raw, exp):
    assert api().get_search_result_n_matches(raw) == exp


@pytest.mark.parametrize(
    "raw, exp",
    (
        pytest.param(
            {"response": {"docs": []}},
            pytest.raises(
                NoSearchResultNumberOfMatchesReturnedError,
                match=re.escape(
                    "This response does not report "
                    "how many records matched the search. "
                    "We expected to read the count from 'response.numFound', "
                    "but 'numFound' is not in 'response', there is only: 'docs'"
                ),
            ),
            id="no-count",
        ),
        pytest.param(
            {},
            pytest.raises(
                NoSearchResultNumberOfMatchesReturnedError,
                match=re.escape(
                    "This response does not report "
                    "how many records matched the search. "
                    "We expected to read the count from 'response.numFound', "
                    "but the response is empty."
                ),
            ),
            id="nothing-we-recognise",
        ),
        pytest.param(
            {"response": {"numFound": "3"}},
            pytest.raises(
                TypeError,
                match=re.escape(
                    "We expected to get an integer at 'response.numFound', "
                    "but instead got '3'"
                ),
            ),
            id="a-count-we-cannot-read",
        ),
    ),
)
def test_get_search_result_n_matches_with_no_count_raises(raw, exp):
    """A response we cannot read a count out of is one we have not understood"""
    with exp:
        api().get_search_result_n_matches(raw)


def test_build_get_facet_values_request_names_the_facets_sorted():
    """The facets are listed under their API names, sorted, so the request is stable"""
    request = api().build_get_facet_values_for_project_request(
        {"variable_id", "experiment_id"}, "CMIP6"
    )

    assert request.params["facets"] == "experiment_id,variable_id"
    assert request.params["project"] == "CMIP6"
    assert request.params["limit"] == api().min_limit


def test_parse_facet_values_keeps_only_the_asked_for_facets():
    raw = {
        "facet_counts": {
            "facet_fields": {
                # Values are interleaved with their counts; the counts are dropped.
                "experiment_id": ["historical", 5, "ssp126", 2],
                "variable_id": ["tas", 9],
                # Not asked for, so not reported.
                "table_id": ["Amon", 3],
            }
        }
    }

    res = api().parse_facet_values(raw, {"experiment_id", "variable_id"})

    assert res == {"experiment_id": {"historical", "ssp126"}, "variable_id": {"tas"}}


@pytest.mark.parametrize(
    "raw",
    (
        pytest.param({}, id="nothing-we-recognise"),
        pytest.param({"facet_counts": {}}, id="no-facet-fields"),
        pytest.param({"facet_counts": {"facet_fields": {}}}, id="no-facets"),
    ),
)
def test_parse_facet_values_with_nothing_to_read_raises(raw):
    with pytest.raises(NoFacetValuesReturnedError, match="pinme"):
        api().parse_facet_values(raw, {"variable_id"})


def test_no_facet_value_returned_error_when_there_is_a_match_raises():
    with pytest.raises(AssertionError, match="pinme"):
        NoFacetValuesReturnedError({"hi": {"bye": 1}}, expected_at="hi.bye")


def test_parse_facet_patterns_is_always_empty():
    """Solr enumerates its facet values; it never describes their form"""
    assert api().parse_facet_patterns({"summaries": {}}, {"variant_label"}) == {}
