"""
Test the ESGF1/Solr search API format

These never touch the network. They pin the two halves of the API separately:
given facet values, the request we build; given a response, what we read out of it.
What a *result* means (its bundle id, its project, how many dataset rows it is) is
not this layer's business either: that is the result parsers', tested in
`tests/unit/search/test_result_parsers.py`.
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
    NoSearchResultDocumentsError,
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


def test_extract_result_documents_reads_the_records():
    """A search answer keeps its records under `response.docs`"""
    docs = [{"master_id": ["a"]}, {"master_id": ["b"]}]

    assert api().extract_result_documents({"response": {"docs": docs}}) == docs


def test_extract_result_documents_of_an_empty_search_is_empty():
    """A search which matched nothing still answers with a `docs` list"""
    raw = {"response": {"numFound": 0, "docs": []}}

    assert api().extract_result_documents(raw) == []


@pytest.mark.parametrize(
    "raw",
    (
        pytest.param({}, id="nothing-we-recognise"),
        pytest.param({"response": {"numFound": 0}}, id="a-response-without-docs"),
        pytest.param({"docs": []}, id="docs-in-the-wrong-place"),
    ),
)
def test_extract_result_documents_without_docs_raises(raw):
    """No `docs` at all is a response we do not understand, not an empty search"""
    with pytest.raises(
        NoSearchResultDocumentsError,
        match=re.escape(
            "This response does not carry the documents a search answers with. "
            "We expected to read them from 'response.docs'"
        ),
    ):
        api().extract_result_documents(raw)


@pytest.mark.parametrize(
    "value, exp",
    (
        pytest.param(["tas", "pr"], ("tas", "pr"), id="a-list"),
        pytest.param(["tas"], ("tas",), id="solrs-usual-one-element-list"),
        pytest.param("tas", ("tas",), id="a-bare-string"),
        pytest.param(3, ("3",), id="an-int"),
        pytest.param(1.5, ("1.5",), id="a-float"),
        pytest.param(None, (), id="a-null"),
        pytest.param(..., (), id="not-there-at-all"),
    ),
)
def test_read_facet_list_reads_the_shapes_solr_writes(value, exp):
    """Every shape Solr really writes a facet in is read as the values it holds"""
    doc = {} if value is ... else {"variable_id": value}

    assert api().read_facet_list(doc, "variable_id") == exp


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

    A `dict` used to come back as its `str()`, which is a value nobody published.
    Each value is checked, not just the shape around it, so one bad value inside an
    otherwise readable list is caught too.
    """
    with pytest.raises(
        NotImplementedError,
        match=re.escape("We do not know how to read 'variable_id'"),
    ):
        api().read_facet_list({"variable_id": value}, "variable_id")


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
    "raw, exp",
    (
        pytest.param(
            {},
            pytest.raises(
                NoFacetValuesReturnedError,
                match=re.escape(
                    "This response does not report facet values. "
                    "We expected to read the facet values from "
                    "'facet_counts.facet_fields', "
                    "but the response is empty."
                ),
            ),
            id="nothing-we-recognise",
        ),
        pytest.param(
            {"facet_counts": {}},
            pytest.raises(
                NoFacetValuesReturnedError,
                match=re.escape(
                    "This response does not report facet values. "
                    "We expected to read the facet values from "
                    "'facet_counts.facet_fields', "
                    "but 'facet_fields' is not in 'facet_counts', "
                    "there are no keys at this path."
                ),
            ),
            id="no-facet-fields",
        ),
        pytest.param(
            {"facet_counts": {"facet_fields": {}}},
            pytest.raises(
                NoFacetValuesReturnedError,
                match=re.escape(
                    "This response does not report facet values. "
                    "We expected to read the facet values from "
                    "'facet_counts.facet_fields', "
                    "but we found {} at 'facet_counts.facet_fields'."
                ),
            ),
            id="no-facets",
        ),
    ),
)
def test_parse_facet_values_with_nothing_to_read_raises(raw, exp):
    with exp:
        api().parse_facet_values(raw, {"variable_id"})


def test_no_facet_value_returned_error_when_there_is_a_match_raises():
    with pytest.raises(
        AssertionError,
        match=re.escape("hi.bye is in {'hi': {'bye': 1}}, raw[hi][bye]=1"),
    ):
        NoFacetValuesReturnedError({"hi": {"bye": 1}}, expected_at="hi.bye")


def test_parse_facet_patterns_is_always_empty():
    """Solr enumerates its facet values; it never describes their form"""
    assert api().parse_facet_patterns({"summaries": {}}, {"variant_label"}) == {}
