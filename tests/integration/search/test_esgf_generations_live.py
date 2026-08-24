"""
Test the search API generations against the live ESGF APIs

These run the whole flow: a query, translated, sent, and read back.
The unit tests already pin what we build and what we parse,
so if those pass and these fail,
the thing that changed is on the other end of the wire.

A node which is down is not a failure of ours,
so an unreachable or unwell node skips.
A node which answers with a 4xx does fail,
because that is the API telling us that it did not understand our request.

These are opt in. They are skipped unless `--run-hits-esgf-search-api` is given,
because they are slow, they depend on servers we do not run,
and a failure here means something different from a failure anywhere else.
"""

from __future__ import annotations

import httpx
import pytest

from esmporium.query import (
    CANONICAL_FACETS,
    QueryCMIP5,
    QueryCMIP6,
    QueryCMIP7,
    facet_spec,
    to_canonical,
)
from esmporium.search import (
    ESGF1Solr,
    ESGF15Bridge,
    ESGFNGStac,
    SolrCMIP5Parameters,
    SolrCMIP6Parameters,
    SolrCMIP7Parameters,
    StacCMIP5Parameters,
    StacCMIP6Parameters,
    StacCMIP7Parameters,
    native_facet_names,
)

pytestmark = pytest.mark.hits_esgf_search_api

TIMEOUT = 60.0
"""How long to wait for a node, in seconds"""

SERVER_ERROR_FLOOR = 500
"""The status code at and above which the problem is the node's, not ours"""

NOT_A_REAL_VALUE = "esmporium-not-a-real-facet-value"
"""
A facet value which no project will ever have

Used to check that the API actually applied the facet we sent it.
If we had the name wrong, the API would ignore it
and the search would come back with everything rather than with nothing.
"""

LIVE_CASES = (
    pytest.param(
        "esgf.nci.org.au",
        ESGF1Solr(params=SolrCMIP5Parameters),
        QueryCMIP5(experiment="historical", variable="tas", time_frequency="mon"),
        "variable",
        id="esgf1-solr-cmip5",
    ),
    pytest.param(
        "esgf.nci.org.au",
        ESGF1Solr(params=SolrCMIP6Parameters),
        QueryCMIP6(experiment_id="historical", variable_id="tas", frequency="mon"),
        "variable_id",
        id="esgf1-solr-cmip6",
    ),
    pytest.param(
        "esgf.nci.org.au",
        ESGF1Solr(params=SolrCMIP7Parameters),
        QueryCMIP7(
            variable_id="tas",
        ),
        "variable_id",
        id="esgf1-solr-cmip7",
    ),
    pytest.param(
        "esgf-node.ornl.gov",
        ESGF15Bridge(params=SolrCMIP6Parameters),
        QueryCMIP6(experiment_id="historical", variable_id="tas", frequency="mon"),
        "variable_id",
        id="esgf15-bridge-cmip6",
    ),
    pytest.param(
        "search.east.esgf.io",
        ESGFNGStac(params=StacCMIP5Parameters),
        QueryCMIP5(experiment="historical", variable="tas", time_frequency="mon"),
        "variable_id",
        id="esgf-ng-stac-cmip5",
    ),
    pytest.param(
        "search.east.esgf.io",
        ESGFNGStac(params=StacCMIP6Parameters),
        QueryCMIP6(experiment_id="historical", variable_id="tas", frequency="mon"),
        "variable_id",
        id="esgf-ng-stac-cmip6",
    ),
    pytest.param(
        "search.east.esgf.io",
        ESGFNGStac(params=StacCMIP7Parameters),
        QueryCMIP7(variable_id="tas"),
        "variable_id",
        id="esgf-ng-stac-cmip7",
    ),
    #
    # TODO Anna: please add tests that check the AND/OR logic of the queries
    # for different generations.
    # Let's do a search for [tas, rsdt] for [piControl and historical].
    # We expect to get results for all combinations
    # i.e. it is OR logic within a facet and AND across facets
    # (but OR over combinations of facets).
    # Let's do esgf1-solr-cmip5, esgf15-bridge-cmip6 and esgf-ng-stac-cmip7
    # for these tests.
)
"""
A host, the generation it speaks, a query we expect it to match,
and the name of one field of that query in the query class's own vocabulary
"""


STAC_LIVE_CASES = tuple(case for case in LIVE_CASES if "stac" in str(case.id))
"""The live cases whose API describes its facet values in a STAC collection"""


AND_OR_VARIABLES = ("tas", "rsdt")
"""The two variables we probe the AND/OR logic with"""

AND_OR_EXPERIMENTS = ("piControl", "historical")
"""The two experiments we probe the AND/OR logic with"""


def and_or_query(query_cls, variable_field, experiment_field):
    """
    Build a query maker for a query class's own native facet names

    Parameters
    ----------
    query_cls
        The query class to build

    variable_field
        The name that class uses for the variable facet

    experiment_field
        The name that class uses for the experiment facet

    Returns
    -------
    :
        A function of `(variables, experiments)` returning a query
    """

    def make(variables, experiments):
        return query_cls(**{variable_field: variables, experiment_field: experiments})

    return make


AND_OR_CASES = (
    pytest.param(
        "esgf.nci.org.au",
        ESGF1Solr(params=SolrCMIP5Parameters),
        and_or_query(QueryCMIP5, "variable", "experiment"),
        id="esgf1-solr-cmip5",
    ),
    pytest.param(
        "esgf-node.ornl.gov",
        ESGF15Bridge(params=SolrCMIP6Parameters),
        and_or_query(QueryCMIP6, "variable_id", "experiment_id"),
        id="esgf15-bridge-cmip6",
    ),
    pytest.param(
        "search.east.esgf.io",
        ESGFNGStac(params=StacCMIP7Parameters),
        and_or_query(QueryCMIP7, "variable_id", "experiment_id"),
        id="esgf-ng-stac-cmip7",
    ),
)
"""
A host, the generation it speaks, and a maker for queries in that query class's
own vocabulary
"""


@pytest.fixture(scope="module")
def client():
    """Get an HTTP client for talking to the live APIs"""
    with httpx.Client(follow_redirects=True, timeout=TIMEOUT) as res:
        yield res


def fire(client, host, request):
    """
    Send a request to a host and hand back the JSON it answered with

    Parameters
    ----------
    client
        The client to send with

    host
        The host to send to

    request
        The request to send, as built by a generation

    Returns
    -------
    :
        The raw JSON the host answered with
    """
    try:
        response = client.request(
            request.method,
            f"https://{host}{request.path}",
            params=request.params,
            json=request.json_body,
        )
    except httpx.TransportError as exc:
        pytest.skip(f"Could not reach {host}: {exc!r}")

    if response.status_code >= SERVER_ERROR_FLOOR:
        pytest.skip(f"{host} answered {response.status_code}, so it is unwell")

    assert response.status_code == httpx.codes.OK, (
        f"{host} did not accept the request we built: "
        f"{response.status_code}, {response.text[:1000]}"
    )

    return response.json()


@pytest.mark.parametrize("host, generation, query, native_facet", LIVE_CASES)
def test_search_finds_data(client, host, generation, query, native_facet):
    """Test that a query we expect to match something does match something"""
    request = generation.build_search_request(to_canonical(query), limit=5)

    raw = fire(client, host, request)

    assert generation.result_count(raw) > 0


@pytest.mark.parametrize("host, generation, query, native_facet", LIVE_CASES)
def test_search_applies_the_facets_we_send(
    client, host, generation, query, native_facet
):
    """
    Test that the API understood the facet names we sent it

    We take the query which does match data and change one facet
    to a value nothing can have.
    If the API is applying that facet, nothing comes back.
    If it came back with matches, it ignored the name we used,
    which means our name for that facet is wrong
    and every search we build with it is quietly unfiltered.
    """
    nonsense = query.model_copy(update={native_facet: (NOT_A_REAL_VALUE,)})
    request = generation.build_search_request(to_canonical(nonsense), limit=5)

    raw = fire(client, host, request)

    assert generation.result_count(raw) == 0


@pytest.mark.parametrize("host, generation, query, native_facet", LIVE_CASES)
def test_facet_values_can_be_listed(client, host, generation, query, native_facet):
    """
    Test that we can ask an API which values a facet has, and read the answer

    `tas` is used as the probe because every project publishes it.
    """
    facets = {"variable", "reporting_interval", "model"}
    request = generation.build_get_facet_values_request(to_canonical(query), facets)

    raw = fire(client, host, request)
    res = generation.parse_facet_values(raw, facets)

    assert set(res) <= facets, "we were told about a facet we did not ask about"
    assert "tas" in res["variable"]
    assert res["model"]
    assert res["reporting_interval"]


def every_facet(generation):
    """
    Get every facet a generation's vocabulary can express

    Asking about one it cannot is an error rather than a request,
    so "everything" has to mean everything this vocabulary has a name for:
    the canonical facets it maps, plus the ones which are its own
    (`product` on CMIP5, `sub_experiment_id` on CMIP6, and so on).
    Including the second kind is the point of asking for everything here:
    the dialect-specific names are the ones we guessed at,
    so these are the tests which find out whether we guessed right.

    Parameters
    ----------
    generation
        The generation whose vocabulary to read

    Returns
    -------
    :
        The facets it can express, named the way they are asked for
    """
    spec = facet_spec(generation.params)

    canonical = set(spec.canonical_to_native)
    assert canonical <= CANONICAL_FACETS

    return canonical | set(spec.query_specific_facets)


@pytest.mark.parametrize("host, generation, query, native_facet", LIVE_CASES)
def test_facet_values_are_well_formed(client, host, generation, query, native_facet):
    """
    Test the shape of what we hand back, against whatever the APIs are serving today

    Every facet the vocabulary can express is asked about, so this covers the
    facets which the APIs describe in ways that are not a list of values,
    as well as those they do.

    A facet we report has to have at least one value.
    Reporting a facet with nothing in it would be read as
    "this facet has no valid values", which is never what we mean:
    if we cannot list a facet's values, we leave it out.
    """
    facets = every_facet(generation)
    request = generation.build_get_facet_values_request(to_canonical(query), facets)

    raw = fire(client, host, request)
    res = generation.parse_facet_values(raw, facets)

    assert res, "no facet was reported at all"
    for facet, values in res.items():
        assert values, f"{facet} was reported with no values"
        assert all(isinstance(value, str) and value for value in values), (
            f"{facet} was reported with a value which is not a non-empty string"
        )


@pytest.mark.parametrize("host, generation, query, native_facet", STAC_LIVE_CASES)
def test_facets_which_are_not_enumerated_are_left_out(
    client, host, generation, query, native_facet
):
    """
    Test that a facet the API describes without listing its values is left out

    A STAC collection summarises some facets as a regular expression or as a
    range rather than as a list, because their values are generated rather than
    chosen from a vocabulary (`variant_label` is the standing example).

    What counts as "not enumerated" is read out of the response itself rather
    than hard coded, so this keeps testing the right thing
    if the API starts listing something it used to describe as a pattern.
    """
    facets = every_facet(generation)
    request = generation.build_get_facet_values_request(to_canonical(query), facets)

    raw = fire(client, host, request)
    res = generation.parse_facet_values(raw, facets)

    prefix = f"{generation.params.prefix}:"
    asked_for = {
        native: asked
        for asked, native in native_facet_names(generation.params, facets).items()
    }
    not_enumerated = {
        asked
        for property_name, summary in raw["summaries"].items()
        if property_name.startswith(prefix)
        and (asked := asked_for.get(property_name[len(prefix) :])) is not None
        and not (
            isinstance(summary, list)
            and any(isinstance(value, str) for value in summary)
        )
    }

    if not not_enumerated:
        pytest.skip(f"{host} now lists a value for every facet we asked about")

    assert not (not_enumerated & set(res)), (
        "a facet whose values the collection does not list was reported anyway"
    )


@pytest.mark.parametrize("host, generation, make_query", AND_OR_CASES)
def test_query_ors_within_a_facet_and_ands_across_facets(
    client, host, generation, make_query
):
    """
    Test that facet values OR within a facet and facets AND across each other

    We search for [tas, rsdt] over [piControl, historical] and expect data for
    every one of the four (variable, experiment) combinations. Each combination
    that comes back with data is a variable ANDed with an experiment, so seeing
    all four means each variable is usable with each experiment: the facets AND
    across each other, and both values in each facet are honoured rather than
    one being dropped.

    Note: CMIP7 is new, so some of its combinations may not be published yet;
    this case can legitimately fail until that data exists.
    """

    def count(variables, experiments):
        query = make_query(variables, experiments)
        request = generation.build_search_request(to_canonical(query), limit=1)
        return generation.result_count(fire(client, host, request))

    for variable in AND_OR_VARIABLES:
        for experiment in AND_OR_EXPERIMENTS:
            found = count((variable,), (experiment,))
            assert found > 0, (
                f"expected data for variable={variable}, experiment={experiment}, "
                f"but {host} matched none"
            )


# TODO: eventually will test AND/OR logic again once populating Dataset. Testing
# what is returning (rather than simply results > 0).
# Eventually also will have higher level wrappers for more sophisticated
# search logic -> i.e. Malte's search example.
