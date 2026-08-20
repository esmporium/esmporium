"""
Test the search API generations

These tests never touch the network.
They pin the two halves of a generation separately:

1. given a query, the request we build
2. given a response, what we read out of it

The responses used here are written by hand,
so a failure means that our code changed,
not that a live API did.
Whether the live APIs still answer the way we assume
is what the integration tests are for.
"""

from __future__ import annotations

from dataclasses import asdict

import pytest

from esmporium.query import (
    FacetNotExpressibleError,
    QueryCanonical,
    QueryCMIP5,
    QueryCMIP6,
    QueryCMIP7,
    to_canonical,
)
from esmporium.search import (
    MAX_LIMIT,
    MIN_LIMIT,
    ESGF1Solr,
    ESGF15Bridge,
    ESGFNGStac,
    LimitOutOfRangeError,
    NoFacetValuesReturned,
    NoResultCountReturned,
    OneProjectRequiredError,
    ProjectPrefixMismatchError,
    SolrCMIP5Parameters,
    SolrCMIP6Parameters,
    SolrCMIP7Parameters,
    StacCMIP5Parameters,
    StacCMIP6Parameters,
    StacCMIP7Parameters,
    UnaskableFacetError,
    solr_facet_values,
    solr_num_found,
    stac_summary_values,
)

# The queries the request-building tests start from.
# Each sets a multi-value facet, so the difference between the wire formats
# (repeated parameters, comma-joined values, a CQL2 `in` clause) is visible,
# and a facet which is specific to the project's dialect,
# so we can see those come through too.
QUERY_CMIP5 = QueryCMIP5(
    experiment=("historical", "rcp85"),
    variable="tas",
    time_frequency="mon",
    ensemble="r1i1p1",
    product="output1",
)

QUERY_CMIP6 = QueryCMIP6(
    experiment_id=("historical", "ssp126"),
    variable_id="tas",
    frequency="mon",
    table_id="Amon",
    sub_experiment_id="none",
)

QUERY_CMIP7 = QueryCMIP7(
    experiment_id=("historical", "esm-hist"),
    variable_id="tas",
    frequency="mon",
    branding_suffix="tavg-h2m-hxy-air",
    region="global",
)

GENERATION_CASES = (
    pytest.param(
        ESGF1Solr(params=SolrCMIP5Parameters), QUERY_CMIP5, id="esgf1-solr-cmip5"
    ),
    pytest.param(
        ESGF1Solr(params=SolrCMIP6Parameters), QUERY_CMIP6, id="esgf1-solr-cmip6"
    ),
    pytest.param(
        ESGF1Solr(params=SolrCMIP7Parameters), QUERY_CMIP7, id="esgf1-solr-cmip7"
    ),
    pytest.param(
        ESGF15Bridge(params=SolrCMIP5Parameters), QUERY_CMIP5, id="esgf15-bridge-cmip5"
    ),
    pytest.param(
        ESGF15Bridge(params=SolrCMIP6Parameters), QUERY_CMIP6, id="esgf15-bridge-cmip6"
    ),
    pytest.param(
        ESGFNGStac(params=StacCMIP5Parameters), QUERY_CMIP5, id="esgf-ng-stac-cmip5"
    ),
    pytest.param(
        ESGFNGStac(params=StacCMIP6Parameters), QUERY_CMIP6, id="esgf-ng-stac-cmip6"
    ),
    pytest.param(
        ESGFNGStac(params=StacCMIP7Parameters), QUERY_CMIP7, id="esgf-ng-stac-cmip7"
    ),
)
"""Each generation, paired with a query written in the project's own dialect"""

SOLR_GENERATION_CASES = tuple(
    case for case in GENERATION_CASES if "stac" not in str(case.id)
)
"""The generations which answer in Solr's shape"""


@pytest.mark.parametrize("generation, query", GENERATION_CASES)
def test_build_search_request(generation, query, data_regression):
    """
    Test that the request we build for a search hasn't changed unnoticed

    When this test fails, read the diff and ask whether the request it now
    describes is one the API would understand.
    If it is, regenerate with `--force-regen` (from pytest-regressions).
    """
    request = generation.build_search_request(to_canonical(query), limit=25)

    data_regression.check(asdict(request))


@pytest.mark.parametrize("generation, query", GENERATION_CASES)
def test_build_get_facet_values_request(generation, query, data_regression):
    """
    Test that the request we build to list facet values hasn't changed unnoticed

    Only facets every vocabulary here can express are asked for.
    Asking for one a vocabulary cannot express is an error, not a request:
    see `test_build_get_facet_values_request_for_a_facet_we_cannot_express`.
    """
    facets = {"variable", "reporting_interval", "model"}

    request = generation.build_get_facet_values_request(to_canonical(query), facets)

    data_regression.check(asdict(request))


@pytest.mark.parametrize("generation, query", SOLR_GENERATION_CASES)
def test_build_get_facet_values_request_without_a_project(generation, query):
    """
    Test that a query with no project asks the API for everything it has

    There is nothing to scope to, so we send no scope,
    rather than sending an empty one and letting the API decide what that means.
    """
    canonical = to_canonical(query).model_copy(update={"project": ()})

    request = generation.build_get_facet_values_request(canonical, {"variable"})

    assert "project" not in request.params


@pytest.mark.parametrize(
    "distrib, exp",
    (
        pytest.param(True, "true", id="sweep-the-federation"),
        pytest.param(False, "false", id="this-node-only"),
    ),
)
def test_esgf1_solr_distrib_is_configurable(distrib, exp):
    """
    Test that the caller can choose whether to sweep the federation

    Both requests carry it, because "which node am I asking" is a property
    of the conversation rather than of the question being asked.
    """
    generation = ESGF1Solr(params=SolrCMIP5Parameters, distrib=distrib)
    canonical = to_canonical(QUERY_CMIP5)

    assert generation.build_search_request(canonical, limit=25).params["distrib"] == exp
    assert (
        generation.build_get_facet_values_request(canonical, {"variable"}).params[
            "distrib"
        ]
        == exp
    )


@pytest.mark.parametrize(
    "limit",
    (
        pytest.param(MIN_LIMIT - 1, id="below-the-floor"),
        pytest.param(MAX_LIMIT + 1, id="above-the-ceiling"),
    ),
)
@pytest.mark.parametrize("generation, query", GENERATION_CASES)
def test_build_search_request_with_an_impossible_limit(generation, query, limit):
    """
    Test that we refuse a page size no API will honour, rather than clamping

    A caller who asks for more than the maximum and is quietly given the maximum
    has no way to tell that they only received part of the answer.
    """
    with pytest.raises(LimitOutOfRangeError):
        generation.build_search_request(to_canonical(query), limit=limit)


@pytest.mark.parametrize("generation, query", GENERATION_CASES)
def test_build_search_request_at_the_limits(generation, query):
    """Test that the ends of the accepted range are themselves accepted"""
    for limit in (MIN_LIMIT, MAX_LIMIT):
        request = generation.build_search_request(to_canonical(query), limit=limit)

        sent = (
            request.params["limit"]
            if request.params is not None
            else request.json_body["limit"]
        )
        assert sent == limit


@pytest.mark.parametrize(
    "raw, exp",
    (
        pytest.param(
            {"response": {"numFound": 3, "docs": [{"id": "a"}]}}, 3, id="a-count"
        ),
        pytest.param({"response": {"numFound": 0, "docs": []}}, 0, id="no-matches"),
    ),
)
def test_solr_num_found(raw, exp):
    assert solr_num_found(raw) == exp


@pytest.mark.parametrize(
    "raw",
    (
        pytest.param({"response": {"docs": []}}, id="no-count-reported"),
        pytest.param({}, id="nothing-we-recognise"),
        pytest.param({"response": {"numFound": "3"}}, id="a-count-we-cannot-read"),
    ),
)
def test_solr_num_found_with_no_count_we_can_read(raw):
    """
    Test that we say so when a response does not tell us how many matched

    Every search API we know of reports a count,
    so a response we cannot read one out of is a response we have not understood.
    Returning `None` and letting the caller decide what that means
    ends with somebody being told their query matched nothing.
    """
    with pytest.raises(NoResultCountReturned, match="Expected to read the count from"):
        solr_num_found(raw)


@pytest.mark.parametrize("generation, query", SOLR_GENERATION_CASES)
def test_solr_result_count(generation, query):
    """The Solr-shaped generations read their count out of the same place"""
    assert generation.result_count({"response": {"numFound": 17, "docs": []}}) == 17


@pytest.mark.parametrize(
    "raw, exp",
    (
        pytest.param(
            {"numberMatched": 7, "features": [{"id": "a"}, {"id": "b"}]},
            7,
            id="the-total-is-reported",
        ),
        pytest.param(
            {"features": [{"id": "a"}, {"id": "b"}]},
            2,
            id="fall-back-to-counting-this-page",
        ),
        pytest.param({"features": []}, 0, id="no-matches"),
    ),
)
def test_stac_result_count(raw, exp):
    """
    Test how we count matches on the STAC APIs

    Note that the fall back counts the page we were given,
    so it is a lower bound on the total rather than the total.
    """
    generation = ESGFNGStac(params=StacCMIP6Parameters)

    assert generation.result_count(raw) == exp


def test_stac_result_count_with_no_count_we_can_read():
    """
    Test that a response with neither a count nor records is an error

    Same rule as the Solr path: a response we cannot read a count out of
    is not a response which matched nothing.
    """
    generation = ESGFNGStac(params=StacCMIP6Parameters)

    with pytest.raises(NoResultCountReturned, match="Expected to read the count from"):
        generation.result_count({})


SOLR_CMIP5_FACETS_RESPONSE = {
    "responseHeader": {"status": 0},
    "response": {"numFound": 0, "docs": []},
    "facet_counts": {
        "facet_fields": {
            # Asked for, so kept, and the counts are dropped.
            "variable": ["tas", 12, "pr", 4],
            # Asked for, under a name only this vocabulary uses.
            "time_frequency": ["mon", 12, "day", 3],
            # Not asked for, so not reported.
            "cmor_table": ["Amon", 12],
            # Specific to this vocabulary, so it has no canonical name
            # and is only reported when it is asked for by its own name
            # (see `test_solr_parse_facet_values_of_a_dialect_specific_facet`).
            "product": ["output1", 12],
        }
    },
}
"""
A facets count response

This is the shape ESGF1 and the ESGF 1.5 bridge send back
when facet counts are requested.
"""


@pytest.mark.parametrize(
    "generation",
    (
        pytest.param(ESGF1Solr(params=SolrCMIP5Parameters), id="esgf1-solr-cmip5"),
        pytest.param(
            ESGF15Bridge(params=SolrCMIP5Parameters), id="esgf15-bridge-cmip5"
        ),
    ),
)
def test_solr_parse_facet_values(generation):
    """
    Test that we read facet values back into the canonical vocabulary

    The response is keyed by the API's names,
    so `time_frequency` has to come back as `reporting_interval`
    for the caller to be able to do anything with it.
    """
    res = generation.parse_facet_values(
        SOLR_CMIP5_FACETS_RESPONSE, {"variable", "reporting_interval"}
    )

    assert res == {
        "variable": {"tas", "pr"},
        "reporting_interval": {"mon", "day"},
    }


def test_solr_parse_facet_values_uses_the_vocabulary_it_is_given():
    """
    Test that the same response is read differently by a different vocabulary

    The API's names are only meaningful next to the vocabulary they were asked in,
    which is why parsing takes the parameter class rather than guessing.
    """
    raw = {
        "facet_counts": {
            "facet_fields": {
                "variable_id": ["tas", 12],
                "frequency": ["mon", 12],
            }
        }
    }

    res = solr_facet_values(
        raw, SolrCMIP6Parameters, {"variable", "reporting_interval"}
    )

    assert res == {"variable": {"tas"}, "reporting_interval": {"mon"}}

    # The CMIP5 vocabulary calls these something else,
    # so it reads nothing out of the same response.
    assert (
        solr_facet_values(raw, SolrCMIP5Parameters, {"variable", "reporting_interval"})
        == {}
    )


@pytest.mark.parametrize(
    "raw",
    (
        pytest.param({}, id="nothing-we-recognise"),
        pytest.param({"facet_counts": {}}, id="no-facet-fields"),
        pytest.param({"facet_counts": {"facet_fields": {}}}, id="no-facets-reported"),
    ),
)
def test_solr_parse_facet_values_with_nothing_to_read_raises(raw):
    """
    Test that we say so, loudly, when a response enumerates nothing at all
    """
    with pytest.raises(NoFacetValuesReturned):
        solr_facet_values(raw, SolrCMIP5Parameters, {"variable"})


@pytest.mark.parametrize(
    "parse, params",
    (
        pytest.param(solr_facet_values, SolrCMIP5Parameters, id="solr"),
        pytest.param(stac_summary_values, StacCMIP5Parameters, id="stac"),
    ),
)
def test_parse_facet_values_of_facets_we_could_not_have_asked_about(parse, params):
    """
    Test that we fail when asked to read facets we could never have asked about

    CMIP5 has no concept of an activity, so it can never be in the request,
    so it can never be in the response.
    Building the request should have refused already,
    so getting here means our own flow is broken rather than the caller's call
    being wrong, which is why this one is an `AssertionError`.
    """
    with pytest.raises(UnaskableFacetError, match=f"{params.__name__} has no name for"):
        parse({}, params, {"activity"})

    assert issubclass(UnaskableFacetError, AssertionError)


CMIP5_GENERATION_CASES = tuple(
    case for case in GENERATION_CASES if "cmip5" in str(case.id)
)
"""The generations whose vocabulary is CMIP5's, which has no concept of activity"""


@pytest.mark.parametrize("generation, query", CMIP5_GENERATION_CASES)
def test_build_get_facet_values_request_for_a_facet_we_cannot_express(
    generation, query
):
    """
    Test that asking about a facet the vocabulary does not have is an error

    CMIP5 has no concept of an activity.
    Dropping it and answering the rest would tell the caller which variables
    CMIP5 has while saying nothing at all about the other question they asked,
    so they would have no way to know it went nowhere.
    This is the same rule, and the same error,
    as translating a query which names a facet the target cannot express.
    """
    facets = {"variable", "reporting_interval", "activity"}

    with pytest.raises(
        FacetNotExpressibleError,
        match=(
            f"facet 'activity' cannot be represented in {generation.params.__name__}"
        ),
    ):
        generation.build_get_facet_values_request(to_canonical(query), facets)


@pytest.mark.parametrize("generation, query", CMIP5_GENERATION_CASES)
def test_build_get_facet_values_request_reports_every_facet_we_cannot_express(
    generation, query
):
    """
    Test that we are told about all the facets we cannot ask about, not just one
    """
    facets = {"variable", "activity", "grid_label"}

    with pytest.raises(
        FacetNotExpressibleError,
        match=(
            "facets 'activity', 'grid_label' cannot be represented in "
            f"{generation.params.__name__}"
        ),
    ):
        generation.build_get_facet_values_request(to_canonical(query), facets)


@pytest.mark.parametrize("generation, query", GENERATION_CASES)
def test_build_get_facet_values_request_for_a_facet_which_does_not_exist(
    generation, query
):
    """
    Test that we refuse to build a facets request we could never ask

    Failing here rather than on the way back means the mistake is reported
    where it was made, and no pointless request is sent.
    """
    with pytest.raises(FacetNotExpressibleError):
        generation.build_get_facet_values_request(
            to_canonical(query), {"esmporium-made-this-up"}
        )


@pytest.mark.parametrize(
    "generation",
    (
        pytest.param(ESGF1Solr(params=SolrCMIP5Parameters), id="esgf1-solr-cmip5"),
        pytest.param(
            ESGF15Bridge(params=SolrCMIP5Parameters), id="esgf15-bridge-cmip5"
        ),
    ),
)
def test_build_get_facet_values_request_for_a_dialect_specific_facet(generation):
    """
    Test that we can ask about a facet which only this vocabulary has

    `product` is CMIP5's alone, so it has no canonical name
    and is asked for by the name CMIP5 uses, exactly as it would be in a query.
    These are the facets a caller is least likely to know the values of,
    so they are the last ones which should be unaskable.
    """
    request = generation.build_get_facet_values_request(
        to_canonical(QUERY_CMIP5), {"variable", "product"}
    )

    assert request.params["facets"] == "product,variable"


@pytest.mark.parametrize(
    "generation",
    (
        pytest.param(ESGF1Solr(params=SolrCMIP5Parameters), id="esgf1-solr-cmip5"),
        pytest.param(
            ESGF15Bridge(params=SolrCMIP5Parameters), id="esgf15-bridge-cmip5"
        ),
    ),
)
def test_solr_parse_facet_values_of_a_dialect_specific_facet(generation):
    """
    Test that a facet only this vocabulary has comes back under its own name

    It has no canonical name to come back under,
    and the caller asked for it by this name, so this is the name they get.
    """
    res = generation.parse_facet_values(
        SOLR_CMIP5_FACETS_RESPONSE, {"variable", "product"}
    )

    assert res == {"variable": {"tas", "pr"}, "product": {"output1"}}


STAC_CMIP6_COLLECTION = {
    "type": "Collection",
    "id": "CMIP6",
    "summaries": {
        # Asked for, so kept.
        "cmip6:variable_id": ["tas", "pr"],
        # Asked for, under a name only this vocabulary uses.
        "cmip6:frequency": ["mon", "day"],
        # Asked for, but summarised as a pattern rather than as values:
        # this API generates these rather than choosing them from a list.
        "cmip6:variant_label": "^r\\d+i\\d+p\\d+f\\d+$",
        # Asked for, but summarised as patterns rather than as values.
        "cmip6:grid_label": [{"pattern": "^g.*$"}],
        # Not asked for, so not reported.
        "cmip6:table_id": ["Amon", "day"],
        # Specific to this vocabulary, so it is asked for, and reported,
        # under the name this vocabulary uses.
        "cmip6:sub_experiment_id": ["none", "s1960"],
        # Another project's properties, which this collection would not carry,
        # but which must not be read with this vocabulary if it did.
        "cmip7:variable_id": ["should-not-be-read"],
    },
}
"""A collection of the shape ESGF-NG sends back"""


def test_stac_parse_facet_values():
    """
    Test that we read a STAC collection's summaries into the canonical vocabulary

    A facet the collection does not enumerate is left out rather than reported
    as empty: "we cannot list this one" and "this one has no values"
    are answers a caller has to be able to tell apart.
    """
    generation = ESGFNGStac(params=StacCMIP6Parameters)

    res = generation.parse_facet_values(
        STAC_CMIP6_COLLECTION,
        {"variable", "reporting_interval", "variant_label", "grid_label"},
    )

    assert res == {
        "variable": {"tas", "pr"},
        "reporting_interval": {"mon", "day"},
    }


def test_stac_parse_facet_values_of_a_dialect_specific_facet():
    """
    Test that a facet only this vocabulary has comes back under its own name

    Same rule as on the Solr side
    (`test_solr_parse_facet_values_of_a_dialect_specific_facet`),
    with the property carrying this vocabulary's prefix on the way in.
    """
    generation = ESGFNGStac(params=StacCMIP6Parameters)

    res = generation.parse_facet_values(
        STAC_CMIP6_COLLECTION, {"variable", "sub_experiment_id"}
    )

    assert res == {"variable": {"tas", "pr"}, "sub_experiment_id": {"none", "s1960"}}


def test_stac_parse_facet_values_uses_the_prefix_it_is_given():
    """Test that a vocabulary only reads the properties which carry its prefix"""
    res = stac_summary_values(STAC_CMIP6_COLLECTION, StacCMIP7Parameters, {"variable"})

    assert res == {"variable": {"should-not-be-read"}}


@pytest.mark.parametrize(
    "raw",
    (
        pytest.param({"type": "Collection", "id": "CMIP6"}, id="no-summaries"),
        pytest.param({}, id="nothing-we-recognise"),
    ),
)
def test_stac_parse_facet_values_without_summaries_raises(raw):
    """
    Test that we say so, loudly, when a response enumerates nothing at all
    """
    generation = ESGFNGStac(params=StacCMIP6Parameters)

    with pytest.raises(NoFacetValuesReturned):
        generation.parse_facet_values(raw, {"variable"})


@pytest.mark.parametrize(
    "canonical, exp",
    (
        pytest.param(
            QueryCanonical(model=("ACCESS-CM2",)),
            pytest.raises(OneProjectRequiredError, match="Received 0"),
            id="no-project",
        ),
        pytest.param(
            QueryCanonical(project=("CMIP6", "CMIP7")),
            pytest.raises(OneProjectRequiredError, match="Received 2"),
            id="two-projects",
        ),
        pytest.param(
            QueryCanonical(project=("CMIP6Plus",)),
            pytest.raises(
                ProjectPrefixMismatchError,
                match=(
                    "StacCMIP6Parameters writes its properties with the 'cmip6' "
                    "prefix, so it cannot describe the 'CMIP6Plus' collection"
                ),
            ),
            id="a-project-this-vocabulary-does-not-describe",
        ),
    ),
)
@pytest.mark.parametrize(
    "build",
    (
        pytest.param(
            lambda generation, canonical: generation.build_search_request(
                canonical, limit=1
            ),
            id="build_search_request",
        ),
        pytest.param(
            lambda generation, canonical: generation.build_get_facet_values_request(
                canonical, {"variable"}
            ),
            id="build_get_facet_values_request",
        ),
    ),
)
def test_stac_requests_need_exactly_one_matching_project(build, canonical, exp):
    """
    Test that we refuse to build a STAC request we know cannot be answered

    The project is the collection, so there has to be exactly one of them,
    and each collection names its properties with its own prefix
    (`cmip6:` for CMIP6, `cmip6plus:` for CMIP6Plus).
    Sending the wrong prefix is not an error the API reports:
    the filter simply matches nothing, which is indistinguishable from
    "nobody has published that", so we have to catch it ourselves.
    """
    generation = ESGFNGStac(params=StacCMIP6Parameters)

    with exp:
        build(generation, canonical)
