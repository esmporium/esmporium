"""
Test the search API facade: the layer that owns the name translation

The API formats are tested in `tests/unit/search/apis/`; here we test what the
facade adds on top of them: turning a canonical query into a request under the
API parameter names, and turning the answer back into the names it was asked
under. The `SearchAPIFacadeStore` lookups are tested here too.
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
    ESGF1_CMIP6_FACADE_PARAMETERS,
    ESGFNG_CMIP6_FACADE_PARAMETERS,
    INBUILT_SEARCH_API_FACADE_STORE,
    ESGFNGCMIP6ParametersQueryStyle,
    LimitOutOfRangeError,
    OneProjectRequiredError,
    ProjectPrefixMismatchError,
    SearchAPIESGF1Solr,
    SearchAPIESGFNGSTAC,
    SearchAPIFacade,
    SearchAPIFacadeClassification,
    SearchAPIFacadeStore,
    STACFacadeParameters,
    UnaskableFacetError,
    build_transient_retrying,
    identity_string,
)

# This file is where at least some of the tests in PR2.2 should be added.

CMIP6_CANONICALISED = to_canonical(
    QueryCMIP6(
        experiment_id=("historical", "ssp126"),
        variable_id="tas",
        frequency="mon",
        table_id="Amon",
    )
)
"""A CMIP6 query written in its own query style, canonicalised"""


def api_facade_cmip6_esgf1(host="node.example") -> SearchAPIFacade:
    """A CMIP6/ESGF1 facade"""
    return SearchAPIFacade(
        parameters=ESGF1_CMIP6_FACADE_PARAMETERS,
        search_api=SearchAPIESGF1Solr(host, build_transient_retrying(1)),
    )


def api_facade_cmip6_esgfng(host="stac.example") -> SearchAPIFacade:
    """A CMIP6/ESGF-NG facade"""
    return SearchAPIFacade(
        parameters=ESGFNG_CMIP6_FACADE_PARAMETERS,
        search_api=SearchAPIESGFNGSTAC(host, build_transient_retrying(1)),
    )


# --- building requests from a canonical query ---------------------------------


def test_esgf1_build_search_request_translates_the_query():
    """The canonical facets come through under the ESGF1 parameter names, incl. project"""  # noqa: E501
    params = (
        api_facade_cmip6_esgf1()
        .build_search_request(CMIP6_CANONICALISED, limit=25)
        .params
    )

    assert params["project"] == ["CMIP6"]
    assert params["experiment_id"] == ["historical", "ssp126"]
    assert params["variable_id"] == ["tas"]
    assert params["frequency"] == ["mon"]
    assert params["table_id"] == ["Amon"]


def test_esgfng_build_search_request_scopes_by_collection_and_prefixes():
    """The project becomes the collection; every other property carries the prefix"""
    body = (
        api_facade_cmip6_esgfng()
        .build_search_request(CMIP6_CANONICALISED, limit=25)
        .json_body
    )
    clauses = body["filter"]["args"]

    assert {"op": "in", "args": [{"property": "collection"}, ["CMIP6"]]} in clauses
    assert {
        "op": "in",
        "args": [{"property": "cmip6:variable_id"}, ["tas"]],
    } in clauses
    # The project is the collection, never a `cmip6:project` property.
    properties = {clause["args"][0]["property"] for clause in clauses}
    assert "cmip6:project" not in properties


def test_esgf1_build_get_facet_values_request_scopes_to_the_project():
    request = api_facade_cmip6_esgf1().build_get_facet_values_request(
        CMIP6_CANONICALISED, {"experiment", "variable"}
    )

    assert request.params["project"] == "CMIP6"
    assert request.params["facets"] == "experiment_id,variable_id"


def test_esgfng_build_get_facet_values_request_asks_for_the_collection():
    request = api_facade_cmip6_esgfng().build_get_facet_values_request(
        CMIP6_CANONICALISED, {"variable"}
    )

    assert request.path == "/collections/CMIP6"


def test_build_search_request_delegates_limit_checking():
    with pytest.raises(LimitOutOfRangeError):
        api_facade_cmip6_esgf1().build_search_request(CMIP6_CANONICALISED, limit=10_001)


def test_build_get_facet_values_request_refuses_an_unexpressible_facet():
    """`product` is CMIP5's alone, so a CMIP6 facade cannot ask about it"""
    with pytest.raises(FacetNotExpressibleError, match="product"):
        api_facade_cmip6_esgf1().build_get_facet_values_request(
            CMIP6_CANONICALISED, {"variable", "product"}
        )


# --- reading a response back into the names it was asked under ----------------


def test_esgf1_parse_facet_values_reads_back_into_canonical_names():
    """The response is keyed by the API's names; we hand back the canonical ones"""
    raw = {
        "facet_counts": {
            "facet_fields": {
                "experiment_id": ["historical", 5, "ssp126", 2],
                "frequency": ["mon", 9],
            }
        }
    }

    res = api_facade_cmip6_esgf1().parse_facet_values(
        raw, {"experiment", "reporting_interval"}
    )

    assert res == {
        "experiment": {"historical", "ssp126"},
        "reporting_interval": {"mon"},
    }


def test_esgfng_parse_facet_values_strips_the_prefix_and_ignores_other_projects():
    raw = {
        "summaries": {
            "cmip6:variable_id": ["tas", "pr"],
            "cmip6:frequency": ["mon"],
            # Another project's property; it must not be read with this query style.
            # It should be impossible to get this,
            # because you have to specify the collection when getting the facet values,
            # but just in case.
            "cmip7:variable_id": ["should-not-be-read"],
        }
    }

    res = api_facade_cmip6_esgfng().parse_facet_values(
        raw, {"variable", "reporting_interval"}
    )

    assert res == {"variable": {"tas", "pr"}, "reporting_interval": {"mon"}}


def test_esgfng_parse_facet_patterns_reads_back_into_canonical_names():
    raw = {"summaries": {"cmip6:variant_label": "^r\\d+i\\d+p\\d+f\\d+$"}}

    res = api_facade_cmip6_esgfng().parse_facet_patterns(raw, {"variant_label"})

    assert set(res) == {"variant_label"}
    assert isinstance(res["variant_label"], re.Pattern)


def test_parse_facet_values_of_an_unexpressible_facet_raises():
    """Reading a facet the query style cannot express is our bug, not the caller's"""
    with pytest.raises(UnaskableFacetError, match="project"):
        api_facade_cmip6_esgfng().parse_facet_values({"summaries": {}}, {"project"})


def test_askable_facets_drops_what_the_query_style_cannot_express():
    # ESGF-NG has no `project` property (the project is the collection).
    assert api_facade_cmip6_esgfng().askable_facets({"variable", "project"}) == {
        "variable"
    }


# --- ESGF-NG's one-project-and-matching-prefix rule ------------------------------


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
            id="a-project-this-query-style-does-not-describe",
        ),
    ),
)
def test_esgfng_build_search_request_needs_one_matching_project(canonical, exp):
    with pytest.raises(exp):
        api_facade_cmip6_esgfng().build_search_request(canonical, limit=1)


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
            id="a-project-this-query-style-does-not-describe",
        ),
    ),
)
def test_esgfng_build_get_facet_values_request_needs_one_matching_project(
    canonical, exp
):
    """
    Test that the facet values path applies the same rule as the search path
    """
    with pytest.raises(exp):
        api_facade_cmip6_esgfng().build_get_facet_values_request(
            canonical, {"variable"}
        )


def test_esgfng_facet_values_of_a_mismatched_collection_would_be_silent():
    """
    Test the failure the check above prevents, so its value is on the record

    A CMIP7 collection read with a CMIP6 facade reports nothing at all,
    because every property in it carries `cmip7:` rather than `cmip6:`.
    """
    cmip7_collection = {
        "summaries": {
            "cmip7:variable_id": ["tas"],
            "cmip7:experiment_id": ["historical"],
        }
    }

    assert (
        api_facade_cmip6_esgfng().parse_facet_values(
            cmip7_collection, {"variable", "experiment"}
        )
        == {}
    )


# --- ESGF-NG's injectable project -> collection and prefix rules -----------------


def test_esgfng_default_converters_lowercase_the_project_for_the_prefix():
    """The observed ESGF-NG convention: collection `CMIP6`, properties `cmip6:`"""
    assert ESGFNG_CMIP6_FACADE_PARAMETERS.get_collection(CMIP6_CANONICALISED) == "CMIP6"
    assert ESGFNG_CMIP6_FACADE_PARAMETERS.prefix == "cmip6"


def test_stac_facade_project_to_collection_converter_is_used():
    """A deployment which names its collection differently is expressible"""
    parameters = STACFacadeParameters(
        base_query_style=ESGFNGCMIP6ParametersQueryStyle,
        prefix="cmip6",
        project_to_collection_converter=str.lower,
    )

    assert parameters.get_collection(CMIP6_CANONICALISED) == "cmip6"

    facade = SearchAPIFacade(
        parameters=parameters,
        search_api=SearchAPIESGFNGSTAC("stac.example", build_transient_retrying(1)),
    )
    body = facade.build_search_request(CMIP6_CANONICALISED, limit=5).json_body
    clauses = body["filter"]["args"]

    assert {"op": "in", "args": [{"property": "collection"}, ["cmip6"]]} in clauses


def test_stac_facade_project_to_prefix_converter_is_used():
    """
    Test that the project -> prefix rule is a convention we can override

    Lowercasing is what every ESGF-NG collection we have seen does, but it is
    not a rule in the STAC standard, so a deployment is free to disagree.
    """
    parameters = STACFacadeParameters(
        base_query_style=ESGFNGCMIP6ParametersQueryStyle,
        prefix="CMIP6",
        # This deployment prefixes with the project exactly as written.
        project_to_prefix_converter=identity_string,
    )

    assert parameters.get_collection(CMIP6_CANONICALISED) == "CMIP6"
    assert parameters.get_mapping_to_api_facet_names({"variable"}) == {
        "variable": "CMIP6:variable_id"
    }


def test_stac_facade_prefix_converter_which_disagrees_still_raises():
    """Injecting a rule does not switch the check off, it only changes the rule"""
    parameters = STACFacadeParameters(
        base_query_style=ESGFNGCMIP6ParametersQueryStyle,
        prefix="cmip6",
        # Under this rule `CMIP6` should be prefixed `CMIP6`, not `cmip6`.
        project_to_prefix_converter=identity_string,
    )

    with pytest.raises(ProjectPrefixMismatchError, match="cmip6"):
        parameters.get_collection(CMIP6_CANONICALISED)


# --- the facade store ---------------------------------------------------------


def a_store() -> SearchAPIFacadeStore:
    """A small store: host-a serves CMIP5+CMIP6, host-b serves CMIP6 only"""
    return SearchAPIFacadeStore(
        classifications=(
            SearchAPIFacadeClassification(
                api_facade_cmip6_esgf1("host-a"), ("CMIP5", "CMIP6")
            ),
            SearchAPIFacadeClassification(
                api_facade_cmip6_esgfng("host-b"), ("CMIP6",)
            ),
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
    assert facade.parameters is ESGF1_CMIP6_FACADE_PARAMETERS


def test_store_get_for_a_project_from_a_host_that_has_no_such_pairing_raises():
    with pytest.raises(
        ValueError,
        match=(
            re.escape("No API from host='host-a' is associated with project='CMIP7'. ")
            + r"Available hosts and supported projects:.*\s*"
            + re.escape("  - host-a: ['CMIP5', 'CMIP6']")
            + r".*\s*"
            + re.escape("- host-b: ['CMIP6']")
        ),
    ):
        a_store().get_api_facade_for_project_from_host("CMIP7", "host-a")


def test_store_get_for_an_ambiguous_pairing_is_an_assertion_error():
    store = SearchAPIFacadeStore(
        classifications=(
            SearchAPIFacadeClassification(api_facade_cmip6_esgf1("dup"), ("CMIP6",)),
            SearchAPIFacadeClassification(api_facade_cmip6_esgfng("dup"), ("CMIP6",)),
        )
    )
    with pytest.raises(
        AssertionError,
        match=(
            re.escape("More than one candidate for host='dup' and project='CMIP6'. ")
            + re.escape("matches_summary=[")
            + '"'
            + re.escape(
                "facade host='dup', facade API type='SearchAPIESGF1Solr', "
                "supported projects=('CMIP6',)"
            )
            + '"'
            + ", "
            + '"'
            + re.escape(
                "facade host='dup', facade API type='SearchAPIESGFNGSTAC', "
                "supported projects=('CMIP6',)"
            )
            + '"'
            + re.escape("]. matches=")
        ),
    ):
        store.get_api_facade_for_project_from_host("CMIP6", "dup")


def test_inbuilt_store_does_not_substring_match_project_names():
    """`CMIP` is not a project, so it must match nothing (not every CMIP* pool)"""
    assert INBUILT_SEARCH_API_FACADE_STORE.get_api_facades_for_project("CMIP") == []


# --- the default store's retry policies ---------------------------------------


def all_facades(store: SearchAPIFacadeStore):
    """Every facade a store holds, whatever project it is classified against"""
    return [classification.facade for classification in store.classifications]


def test_default_store_gives_every_api_its_own_retry_policy():
    """
    Test that no two APIs share a `Retrying`

    A tenacity `Retrying` carries per-run state, so sharing one across APIs
    is not safe once calls can be made in parallel.
    Identity is what matters here, hence `id` rather than equality.
    """
    store = SearchAPIFacadeStore.initialise_with_default_api_facades()
    facades = all_facades(store)

    retryings = [id(f.search_api.retrying) for f in facades]

    assert len(facades) > 1, "this test needs more than one API to say anything"
    assert len(set(retryings)) == len(retryings)


def test_default_store_builds_one_retry_policy_per_api():
    """The builder is called once per API, no more and no fewer"""
    calls = 0

    def create_retrying():
        nonlocal calls
        calls += 1
        return build_transient_retrying(1)

    store = SearchAPIFacadeStore.initialise_with_default_api_facades(
        create_retrying=create_retrying
    )

    assert calls == len(store.classifications)


def test_default_store_uses_the_policy_the_builder_returns():
    """An injected builder's policy is the one the APIs actually get"""
    store = SearchAPIFacadeStore.initialise_with_default_api_facades(
        create_retrying=lambda: build_transient_retrying(7)
    )

    for facade in all_facades(store):
        assert facade.search_api.retrying.stop.max_attempt_number == 7


def test_default_store_can_be_asked_to_share_one_policy():
    """
    Test that sharing is still possible, it just has to be asked for

    Making the parameter a builder means sharing cannot happen by accident,
    not that it cannot happen at all.
    """
    shared = build_transient_retrying(2)

    store = SearchAPIFacadeStore.initialise_with_default_api_facades(
        create_retrying=lambda: shared
    )

    assert {id(f.search_api.retrying) for f in all_facades(store)} == {id(shared)}


def test_inbuilt_store_gives_every_api_its_own_retry_policy():
    """The store we hand out by default gets the same treatment"""
    facades = all_facades(INBUILT_SEARCH_API_FACADE_STORE)

    retryings = [id(f.search_api.retrying) for f in facades]

    assert len(set(retryings)) == len(retryings)
