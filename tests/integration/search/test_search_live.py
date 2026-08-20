"""
Test the search step end to end against the live ESGF search APIs

These start with a query and run the whole flow through `search`, then read the
result count back. The unit tests in `tests/unit/search/test_search.py` already
pin the step's plumbing and every failure mode, so if those pass and these fail,
the thing that changed is on the other end of the wire.

The whole flow deliberately hides the difference between a node that is down and
a node that refused the request: both come back as "no answer". Telling a bad
request apart from an unwell node is the job of
`tests/integration/search/test_esgf_generations_live.py`, which drives the
generations directly. Here, an empty result means "no node answered", which is a
reason to skip rather than to fail.

These are opt in: they only run with `--run-hits-esgf-search-api`, because they
are slow, they depend on servers we do not run, and a red run here means
something different from a red run anywhere else.
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
    SOLR_CMIP5,
    SOLR_CMIP6,
    SOLR_CMIP7,
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

# The real Solr nodes we compare for the aggregation test. distrib is turned off
# below, so each answers only for the data it holds itself, which is the whole
# point: different nodes hold different data.
AGGREGATION_HOSTS = (
    "esgf.nci.org.au",
    "esgf.ceda.ac.uk",
    "esgf-data.dkrz.de",
    "esg-dn1.nsc.liu.se",
)


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
        for host in AGGREGATION_HOSTS
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
