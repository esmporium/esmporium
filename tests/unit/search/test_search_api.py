"""
Test the endpoints we know about and how we pick between them
"""

from __future__ import annotations

import pytest

from esmporium.query import QueryCanonical
from esmporium.search import (
    Request,
    SearchAPI,
    SearchAPIESGF1Solr,
    build_list_selector,
    build_project_list_selector,
    build_transient_retrying,
    get_url,
)


def make_api(host: str) -> SearchAPI:
    """Build a SearchAPI whose only interesting field, for these tests, is its host"""
    return SearchAPIESGF1Solr(host, retrying=build_transient_retrying(1))


def canonical(*projects: str) -> QueryCanonical:
    """Build a canonical query naming the given projects"""
    return QueryCanonical(project=projects)


def test_url_building():
    api = make_api("esgf.example.org")
    request = Request("GET", "/esg-search/search")

    assert get_url(api, request) == "https://esgf.example.org/esg-search/search"


def test_url_can_use_http():
    """A host which only offers http can be reached over it"""
    api = SearchAPIESGF1Solr(
        "esgf.example.org", retrying=build_transient_retrying(1), scheme="http"
    )
    request = Request("GET", "/esg-search/search")

    assert get_url(api, request) == "http://esgf.example.org/esg-search/search"


def test_list_selector_yields_in_order_then_stops():
    """A list selector walks its list and then says stop"""
    apis = [make_api("a"), make_api("b")]
    select = build_list_selector(apis)
    query = canonical("CMIP6")

    assert select(query, 0) is apis[0]
    assert select(query, 1) is apis[1]
    assert select(query, 2) is None


def test_list_selector_ignores_the_project():
    """Every query gets the same list, whatever its project"""
    apis = [make_api("a")]
    select = build_list_selector(apis)

    assert select(canonical("CMIP5"), 0) is apis[0]
    assert select(canonical("CMIP7"), 0) is apis[0]


def test_project_list_selector_picks_by_project():
    """The project decides which list is walked"""
    fives = [make_api("five")]
    sixes = [make_api("six")]
    select = build_project_list_selector({"CMIP5": fives, "CMIP6": sixes})

    assert select(canonical("CMIP5"), 0) is fives[0]
    assert select(canonical("CMIP6"), 0) is sixes[0]


def test_project_list_selector_stops_at_the_end_of_the_list():
    """Once the list is exhausted, the selector says stop"""
    select = build_project_list_selector({"CMIP6": [make_api("only")]})

    assert select(canonical("CMIP6"), 1) is None
    # If requested later, still gives None
    assert select(canonical("CMIP6"), 100) is None


def test_project_list_selector_needs_exactly_one_project():
    """A search that is not scoped to one project cannot be ranked"""
    select = build_project_list_selector({"CMIP6": [make_api("only")]})

    with pytest.raises(ValueError, match="exactly one project"):
        select(canonical(), 0)

    with pytest.raises(ValueError, match="exactly one project"):
        select(canonical("CMIP5", "CMIP6"), 0)


def test_project_list_selector_raises_for_an_unknown_project():
    """A project we have no plan for is a loud miss, not a quiet stop"""
    select = build_project_list_selector({"CMIP6": [make_api("only")]})

    with pytest.raises(KeyError):
        select(canonical("CMIP5"), 0)
