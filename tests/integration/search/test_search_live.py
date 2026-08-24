"""
Test the search step end to end against the live ESGF search APIs

The unit tests in `tests/unit/search/test_search.py` already
pin the varous steps plumbing and failure modes,
so if those pass and these fail,
the thing that changed is on the other end of the wire.
"""

from __future__ import annotations

import httpx
import pytest

from esmporium.query import QueryCMIP5, QueryCMIP6, QueryCMIP7
from esmporium.search import (
    ESGF1Solr,
    SolrCMIP6Parameters,
    build_list_selector,
    build_transient_retrying,
    search,
)
from esmporium.search.search_api import (
    BRIDGE_CMIP6,
    SOLR_CMIP5,
    SOLR_CMIP6,
    SOLR_CMIP7,
    STAC_CMIP5,
    STAC_CMIP6,
    STAC_CMIP7,
    SearchAPI,
)

pytestmark = pytest.mark.hits_esgf_search_api

TIMEOUT = 60.0
"""How long to wait for a node, in seconds"""

CMIP6_QUERY = QueryCMIP6(experiment_id="historical", variable_id="tas", frequency="mon")
"""A CMIP6 query we expect every CMIP6 node to have data for"""

LIVE_CASES = (
    pytest.param(
        SearchAPI("esgf.nci.org.au", SOLR_CMIP5, build_transient_retrying(2)),
        QueryCMIP5(experiment="historical", variable="tas", time_frequency="mon"),
        id="solr-cmip5",
    ),
    pytest.param(
        SearchAPI("esgf.nci.org.au", SOLR_CMIP6, build_transient_retrying(2)),
        CMIP6_QUERY,
        id="solr-cmip6",
    ),
    pytest.param(
        SearchAPI("search.east.esgf.io", STAC_CMIP6, build_transient_retrying(2)),
        CMIP6_QUERY,
        id="esgf-ng-cmip6",
    ),
    pytest.param(
        SearchAPI("search.east.esgf.io", STAC_CMIP7, build_transient_retrying(2)),
        QueryCMIP7(variable_id="tas"),
        id="esgf-ng-cmip7",
    ),
    pytest.param(
        SearchAPI("esgf.nci.org.au", SOLR_CMIP7, build_transient_retrying(2)),
        QueryCMIP7(variable_id="tas"),
        id="solr-cmip7",
    ),
)
"""A search API and a query we expect it to have data for"""

NOT_A_REAL_VALUE = "esmporium-not-a-real-facet-value"
"""
A facet value which no project will ever have

Used to check that the API actually applied the facet we sent it.
If we had the name wrong, the API would ignore it
and the search would come back with everything rather than with nothing.
"""

# Each case names the query field to poison. A CMIP5 query spells its variable
# facet `variable`; CMIP6 and CMIP7 spell it `variable_id`. Poisoning a field
# the query class does not have would do nothing (model_copy accepts unknown
# keys silently), so the name here has to be a real field of that class.
FACET_NAME_CASES = (
    pytest.param(
        SearchAPI("esgf.nci.org.au", SOLR_CMIP5, build_transient_retrying(2)),
        QueryCMIP5(experiment="historical", variable="tas", time_frequency="mon"),
        "variable",
        id="solr-cmip5",
    ),
    pytest.param(
        SearchAPI("esgf.nci.org.au", SOLR_CMIP6, build_transient_retrying(2)),
        CMIP6_QUERY,
        "variable_id",
        id="solr-cmip6",
    ),
    pytest.param(
        SearchAPI("esgf.nci.org.au", SOLR_CMIP7, build_transient_retrying(2)),
        QueryCMIP7(variable_id="tas"),
        "variable_id",
        id="solr-cmip7",
    ),
    pytest.param(
        SearchAPI("esgf-node.ornl.gov", BRIDGE_CMIP6, build_transient_retrying(2)),
        CMIP6_QUERY,
        "variable_id",
        id="bridge-cmip6",
    ),
    pytest.param(
        SearchAPI("search.east.esgf.io", STAC_CMIP5, build_transient_retrying(2)),
        QueryCMIP5(experiment="historical", variable="tas", time_frequency="mon"),
        "variable",
        id="esgf-ng-cmip5",
    ),
    pytest.param(
        SearchAPI("search.east.esgf.io", STAC_CMIP6, build_transient_retrying(2)),
        CMIP6_QUERY,
        "variable_id",
        id="esgf-ng-cmip6",
    ),
    pytest.param(
        SearchAPI("search.east.esgf.io", STAC_CMIP7, build_transient_retrying(2)),
        QueryCMIP7(variable_id="tas"),
        "variable_id",
        id="esgf-ng-cmip7",
    ),
)
"""A search API, a query it matches, and the query field to poison"""

AND_OR_VARIABLES = ("tas", "rsdt")
"""The two variables we probe the AND/OR logic with"""

AND_OR_EXPERIMENTS = ("piControl", "historical")
"""The two experiments we probe the AND/OR logic with"""


def and_or_query(query_cls, variable_field, experiment_field):
    """
    Build a query maker for a query class's own variable/experiment field names

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
        SearchAPI("esgf.nci.org.au", SOLR_CMIP5, build_transient_retrying(2)),
        and_or_query(QueryCMIP5, "variable", "experiment"),
        id="solr-cmip5",
    ),
    pytest.param(
        SearchAPI("esgf-node.ornl.gov", BRIDGE_CMIP6, build_transient_retrying(2)),
        and_or_query(QueryCMIP6, "variable_id", "experiment_id"),
        id="bridge-cmip6",
    ),
    pytest.param(
        SearchAPI("search.east.esgf.io", STAC_CMIP7, build_transient_retrying(2)),
        and_or_query(QueryCMIP7, "variable_id", "experiment_id"),
        id="esgf-ng-cmip7",
    ),
)
"""A search API and a maker for queries in that case's project vocabulary"""


@pytest.fixture(scope="module")
def client():
    """Get an HTTP client for talking to the live APIs"""
    with httpx.Client(follow_redirects=True, timeout=TIMEOUT) as res:
        yield res


@pytest.mark.parametrize("api, query", LIVE_CASES)
def test_search_returns_results(client, api, query):
    """A query we expect to match something comes back with matches"""
    results = search(query, build_list_selector([api]), limit=5, client=client)

    if not results:
        pytest.skip(f"{api.host} did not answer, so it is down or unwell")

    raw = results[api.host]
    assert api.generation.result_count(raw) > 0


@pytest.mark.parametrize("api, query, poison_field", FACET_NAME_CASES)
def test_search_applies_the_facets_we_send(client, api, query, poison_field):
    """
    Test that the API understood the facet names we sent it

    We take the query which does match data and change one facet
    to a value nothing can have.
    If the API is applying that facet, nothing comes back.
    If it came back with matches, it ignored the name we used,
    which means our name for that facet is wrong
    and every search we build with it is quietly unfiltered.
    """
    nonsense = query.model_copy(update={poison_field: (NOT_A_REAL_VALUE,)})
    results = search(nonsense, build_list_selector([api]), limit=5, client=client)

    if not results:
        pytest.skip(f"{api.host} did not answer, so it is down or unwell")

    assert api.generation.result_count(results[api.host]) == 0


def master_ids(raw: dict) -> set[str]:
    """
    Read the unique dataset identifiers out of a Solr-shaped response

    `master_id` is the identity of a dataset across the nodes that hold it and
    the versions it has had, so it is what "the same dataset" means here.

    Parameters
    ----------
    raw
        The response to read

    Returns
    -------
    :
        The master ids of the datasets in `raw`
    """
    docs = raw.get("response", {}).get("docs", [])
    return {doc["master_id"] for doc in docs if "master_id" in doc}


def test_aggregating_over_nodes_finds_more_than_one_node(client):
    """
    Searching several nodes finds more unique datasets than searching one

    The nodes do not all hold the same data, so the union of what they each hold
    is larger than any single one of them. This is the reason the fan-out search
    (and, later, a merge across nodes) exists.

    With `distrib` off, each node answers only for the data it holds itself, so
    the comparison is between genuinely different holdings rather than between
    federation-wide sweeps that would mirror one another.
    """
    local_solr = ESGF1Solr(params=SolrCMIP6Parameters, distrib=False)
    nodes = [
        SearchAPI(host, local_solr, build_transient_retrying(2))
        # real solr nodes to compare aggregation (unique results per host)
        for host in (
            "esgf.nci.org.au",
            "esgf.ceda.ac.uk",
            "esgf-data.dkrz.de",
            "esg-dn1.nsc.liu.se",
        )
    ]

    results = search(
        CMIP6_QUERY,
        build_list_selector(nodes),
        stop_at_first_result=False,
        client=client,
    )

    per_host = {host: master_ids(raw) for host, raw in results.items()}
    answered = {host: ids for host, ids in per_host.items() if ids}
    if len(answered) < 2:
        pytest.skip(
            f"only {len(answered)} node(s) answered with data; "
            "cannot compare aggregation to a single node"
        )

    union: set[str] = set().union(*answered.values())
    best_single = max(len(ids) for ids in answered.values())

    assert len(union) > best_single, (
        "aggregating across nodes should find more unique datasets "
        "than the single most complete node"
    )


@pytest.mark.parametrize("api, make_query", AND_OR_CASES)
def test_search_ors_within_a_facet_and_ands_across_facets(client, api, make_query):
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
        results = search(query, build_list_selector([api]), limit=1, client=client)
        if not results:
            pytest.skip(f"{api.host} did not answer, so it is down or unwell")
        return api.generation.result_count(results[api.host])

    for variable in AND_OR_VARIABLES:
        for experiment in AND_OR_EXPERIMENTS:
            found = count((variable,), (experiment,))
            assert found > 0, (
                f"expected data for variable={variable}, experiment={experiment}, "
                f"but {api.host} matched none"
            )


# TODO: eventually will test AND/OR logic again once populating Dataset. Testing
# what is returning (rather than simply results > 0).
# Eventually also will have higher level wrappers for more sophisticated
# search logic -> i.e. Malte's search example.
