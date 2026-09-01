"""
Test the search API facade: the layer that owns vocabulary translation

The wire formats are tested in `tests/unit/search/apis/`; here we test what the
facade adds on top of them: turning a canonical query into a request in the
vocabulary the wire format speaks, and turning the answer back into the canonical
vocabulary. The `SearchAPIFacadeStore` lookups are tested here too.
"""

from __future__ import annotations

import re

import pytest

from esmporium.query import (
    FacetNotExpressibleError,
    QueryCanonical,
    QueryCMIP6,
    to_canonical,
)
from esmporium.search import (
    INBUILT_SEARCH_API_FACADE_STORE,
    LimitOutOfRangeError,
    OneProjectRequiredError,
    ProjectPrefixMismatchError,
    SearchAPIESGF1Solr,
    SearchAPIESGFNGSTAC,
    SearchAPIFacade,
    SearchAPIFacadeClassification,
    SearchAPIFacadeStore,
    SolrCMIP6Parameters,
    STACCMIP6Parameters,
    UnaskableFacetError,
    build_transient_retrying,
)

CMIP6 = to_canonical(
    QueryCMIP6(
        experiment_id=("historical", "ssp126"),
        variable_id="tas",
        frequency="mon",
        table_id="Amon",
    )
)
"""A CMIP6 query written in its own dialect, canonicalised"""


def solr6(host="node.example") -> SearchAPIFacade:
    """A CMIP6/Solr facade"""
    return SearchAPIFacade(
        parameters=SolrCMIP6Parameters,
        search_api=SearchAPIESGF1Solr(host, build_transient_retrying(1)),
    )


def stac6(host="stac.example") -> SearchAPIFacade:
    """A CMIP6/STAC facade"""
    return SearchAPIFacade(
        parameters=STACCMIP6Parameters,
        search_api=SearchAPIESGFNGSTAC(host, build_transient_retrying(1)),
    )


# --- building requests from a canonical query ---------------------------------


def test_solr_build_search_request_translates_the_query():
    """The canonical facets come through under the Solr wire names, incl. project"""
    params = solr6().build_search_request(CMIP6, limit=25).params

    assert params["project"] == ["CMIP6"]
    assert params["experiment_id"] == ["historical", "ssp126"]
    assert params["variable_id"] == ["tas"]
    assert params["frequency"] == ["mon"]
    assert params["table_id"] == ["Amon"]


def test_stac_build_search_request_scopes_by_collection_and_prefixes():
    """The project becomes the collection; every other property carries the prefix"""
    body = stac6().build_search_request(CMIP6, limit=25).json_body
    clauses = body["filter"]["args"]

    assert {"op": "in", "args": [{"property": "collection"}, ["CMIP6"]]} in clauses
    assert {
        "op": "in",
        "args": [{"property": "cmip6:variable_id"}, ["tas"]],
    } in clauses
    # The project is the collection, never a `cmip6:project` property.
    properties = {clause["args"][0]["property"] for clause in clauses}
    assert "cmip6:project" not in properties


def test_solr_build_get_facet_values_request_scopes_to_the_project():
    request = solr6().build_get_facet_values_request(CMIP6, {"experiment", "variable"})

    assert request.params["project"] == "CMIP6"
    assert request.params["facets"] == "experiment_id,variable_id"


def test_stac_build_get_facet_values_request_asks_for_the_collection():
    request = stac6().build_get_facet_values_request(CMIP6, {"variable"})

    assert request.path == "/collections/CMIP6"


def test_build_search_request_delegates_limit_checking():
    with pytest.raises(LimitOutOfRangeError):
        solr6().build_search_request(CMIP6, limit=10_001)


def test_build_get_facet_values_request_refuses_an_unexpressible_facet():
    """`product` is CMIP5's alone, so a CMIP6 facade cannot ask about it"""
    with pytest.raises(FacetNotExpressibleError, match="product"):
        solr6().build_get_facet_values_request(CMIP6, {"variable", "product"})


# --- reading a response back into the canonical vocabulary --------------------


def test_solr_parse_facet_values_reads_back_into_canonical_names():
    """The response is keyed by the API's names; we hand back the canonical ones"""
    raw = {
        "facet_counts": {
            "facet_fields": {
                "experiment_id": ["historical", 5, "ssp126", 2],
                "frequency": ["mon", 9],
            }
        }
    }

    res = solr6().parse_facet_values(raw, {"experiment", "reporting_interval"})

    assert res == {
        "experiment": {"historical", "ssp126"},
        "reporting_interval": {"mon"},
    }


def test_stac_parse_facet_values_strips_the_prefix_and_ignores_other_projects():
    raw = {
        "summaries": {
            "cmip6:variable_id": ["tas", "pr"],
            "cmip6:frequency": ["mon"],
            # Another project's property; it must not be read with this vocabulary.
            "cmip7:variable_id": ["should-not-be-read"],
        }
    }

    res = stac6().parse_facet_values(raw, {"variable", "reporting_interval"})

    assert res == {"variable": {"tas", "pr"}, "reporting_interval": {"mon"}}


def test_stac_parse_facet_patterns_reads_back_into_canonical_names():
    raw = {"summaries": {"cmip6:variant_label": "^r\\d+i\\d+p\\d+f\\d+$"}}

    res = stac6().parse_facet_patterns(raw, {"variant_label"})

    assert set(res) == {"variant_label"}
    assert isinstance(res["variant_label"], re.Pattern)


def test_parse_facet_values_of_an_unexpressible_facet_raises():
    """Reading a facet the vocabulary cannot express is our bug, not the caller's"""
    with pytest.raises(UnaskableFacetError, match="project"):
        stac6().parse_facet_values({"summaries": {}}, {"project"})


def test_askable_facets_drops_what_the_vocabulary_cannot_express():
    # STAC has no `project` property (the project is the collection).
    assert stac6().askable_facets({"variable", "project"}) == {"variable"}


# --- STAC's one-project-and-matching-prefix rule ------------------------------


@pytest.mark.parametrize(
    "canonical, exp",
    (
        pytest.param(
            QueryCanonical(project=()),
            OneProjectRequiredError,
            id="no-project",
        ),
        pytest.param(
            QueryCanonical(project=("CMIP6", "CMIP7")),
            OneProjectRequiredError,
            id="two-projects",
        ),
        pytest.param(
            QueryCanonical(project=("CMIP7",)),
            ProjectPrefixMismatchError,
            id="a-project-this-vocabulary-does-not-describe",
        ),
    ),
)
def test_stac_build_search_request_needs_one_matching_project(canonical, exp):
    with pytest.raises(exp):
        stac6().build_search_request(canonical, limit=1)


# --- the facade store ---------------------------------------------------------


def a_store() -> SearchAPIFacadeStore:
    """A small store: host-a serves CMIP5+CMIP6, host-b serves CMIP6 only"""
    return SearchAPIFacadeStore(
        classifications=(
            SearchAPIFacadeClassification(solr6("host-a"), ("CMIP5", "CMIP6")),
            SearchAPIFacadeClassification(stac6("host-b"), ("CMIP6",)),
        )
    )


def test_store_gets_facades_for_a_project():
    hosts = {f.search_api.host for f in a_store().get_api_facades_for_project("CMIP6")}
    assert hosts == {"host-a", "host-b"}

    hosts = {f.search_api.host for f in a_store().get_api_facades_for_project("CMIP5")}
    assert hosts == {"host-a"}


def test_store_gets_facades_from_a_host():
    facades = a_store().get_api_facades_from_host("host-a")

    assert [f.search_api.host for f in facades] == ["host-a"]


def test_store_gets_the_one_facade_for_a_project_from_a_host():
    facade = a_store().get_api_facade_for_project_from_host("CMIP5", "host-a")

    assert facade.search_api.host == "host-a"
    assert facade.parameters is SolrCMIP6Parameters


def test_store_get_for_a_project_from_a_host_that_has_no_such_pairing_raises():
    with pytest.raises(ValueError, match="No API from"):
        a_store().get_api_facade_for_project_from_host("CMIP7", "host-a")


def test_store_get_for_an_ambiguous_pairing_is_an_assertion_error():
    store = SearchAPIFacadeStore(
        classifications=(
            SearchAPIFacadeClassification(solr6("dup"), ("CMIP6",)),
            SearchAPIFacadeClassification(stac6("dup"), ("CMIP6",)),
        )
    )

    with pytest.raises(AssertionError, match="More than one candidate"):
        store.get_api_facade_for_project_from_host("CMIP6", "dup")


def test_inbuilt_store_does_not_substring_match_project_names():
    """`CMIP` is not a project, so it must match nothing (not every CMIP* pool)"""
    assert INBUILT_SEARCH_API_FACADE_STORE.get_api_facades_for_project("CMIP") == []
