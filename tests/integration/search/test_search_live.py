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

from esmporium.query import QueryCMIP5, QueryCMIP6, QueryCMIP7, to_canonical
from esmporium.search import (
    ESGF1_CMIP6_FACADE_PARAMETERS,
    INBUILT_SEARCH_API_FACADE_STORE,
    CouldNotGetSearchResponseError,
    NoFacadeAnsweredError,
    ParsedDocument,
    SearchAPIESGF1Solr,
    SearchAPIFacade,
    SearchAPIRequestError,
    SolrSingleRowResultParser,
    build_list_selector,
    build_transient_retrying,
    fire,
    search,
)

pytestmark = pytest.mark.hits_esgf_search_api

TIMEOUT = 60.0
"""How long to wait for a node, in seconds"""


CMIP6_QUERY = QueryCMIP6(experiment_id="historical", variable_id="tas", frequency="mon")
"""A CMIP6 query we expect every CMIP6 node to have data for"""

LIVE_CASES = (
    pytest.param(
        INBUILT_SEARCH_API_FACADE_STORE.get_api_facade_for_project_from_host(
            "CMIP5", "esgf.nci.org.au"
        ),
        QueryCMIP5(experiment="historical", variable="tas", time_frequency="mon"),
        id="solr-cmip5",
    ),
    pytest.param(
        INBUILT_SEARCH_API_FACADE_STORE.get_api_facade_for_project_from_host(
            "CMIP6", "esgf.nci.org.au"
        ),
        CMIP6_QUERY,
        id="solr-cmip6",
    ),
    pytest.param(
        INBUILT_SEARCH_API_FACADE_STORE.get_api_facade_for_project_from_host(
            "CMIP6", "search.east.esgf.io"
        ),
        CMIP6_QUERY,
        id="esgf-ng-cmip6-east",
    ),
    pytest.param(
        INBUILT_SEARCH_API_FACADE_STORE.get_api_facade_for_project_from_host(
            "CMIP6", "search.west.esgf.io"
        ),
        CMIP6_QUERY,
        id="esgf-ng-cmip6-west",
    ),
    pytest.param(
        INBUILT_SEARCH_API_FACADE_STORE.get_api_facade_for_project_from_host(
            "CMIP7", "search.east.esgf.io"
        ),
        QueryCMIP7(variable_id="tas"),
        id="esgf-ng-cmip7-east",
    ),
    pytest.param(
        INBUILT_SEARCH_API_FACADE_STORE.get_api_facade_for_project_from_host(
            "CMIP7", "search.west.esgf.io"
        ),
        QueryCMIP7(variable_id="tas"),
        id="esgf-ng-cmip7-west",
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
        INBUILT_SEARCH_API_FACADE_STORE.get_api_facade_for_project_from_host(
            "CMIP5", "esgf.nci.org.au"
        ),
        QueryCMIP5(experiment="historical", variable="tas", time_frequency="mon"),
        "variable",
        id="solr-cmip5",
    ),
    pytest.param(
        INBUILT_SEARCH_API_FACADE_STORE.get_api_facade_for_project_from_host(
            "CMIP6", "esgf.nci.org.au"
        ),
        CMIP6_QUERY,
        "variable_id",
        id="solr-cmip6",
    ),
    pytest.param(
        INBUILT_SEARCH_API_FACADE_STORE.get_api_facade_for_project_from_host(
            "CMIP6", "esgf-node.ornl.gov"
        ),
        CMIP6_QUERY,
        "variable_id",
        id="bridge-cmip6",
    ),
    pytest.param(
        INBUILT_SEARCH_API_FACADE_STORE.get_api_facade_for_project_from_host(
            "CMIP6", "search.east.esgf.io"
        ),
        CMIP6_QUERY,
        "variable_id",
        id="esgf-ng-cmip6-east",
    ),
    pytest.param(
        INBUILT_SEARCH_API_FACADE_STORE.get_api_facade_for_project_from_host(
            "CMIP7", "search.east.esgf.io"
        ),
        QueryCMIP7(variable_id="tas"),
        "variable_id",
        id="esgf-ng-cmip7-east",
    ),
    pytest.param(
        INBUILT_SEARCH_API_FACADE_STORE.get_api_facade_for_project_from_host(
            "CMIP7", "search.west.esgf.io"
        ),
        QueryCMIP7(variable_id="tas"),
        "variable_id",
        id="esgf-ng-cmip7-west",
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
        INBUILT_SEARCH_API_FACADE_STORE.get_api_facade_for_project_from_host(
            "CMIP5", "esgf.nci.org.au"
        ),
        and_or_query(QueryCMIP5, "variable", "experiment"),
        id="solr-cmip5",
    ),
    pytest.param(
        INBUILT_SEARCH_API_FACADE_STORE.get_api_facade_for_project_from_host(
            "CMIP6", "esgf-node.ornl.gov"
        ),
        and_or_query(QueryCMIP6, "variable_id", "experiment_id"),
        id="bridge-cmip6",
    ),
    pytest.param(
        INBUILT_SEARCH_API_FACADE_STORE.get_api_facade_for_project_from_host(
            "CMIP7", "search.east.esgf.io"
        ),
        and_or_query(QueryCMIP7, "variable_id", "experiment_id"),
        id="esgf-ng-cmip7-east",
    ),
    pytest.param(
        INBUILT_SEARCH_API_FACADE_STORE.get_api_facade_for_project_from_host(
            "CMIP7", "search.west.esgf.io"
        ),
        and_or_query(QueryCMIP7, "variable_id", "experiment_id"),
        id="esgf-ng-cmip7-west",
    ),
)
"""A search API and a maker for queries in that case's project query style"""


@pytest.fixture(scope="module")
def client():
    """Get an HTTP client for talking to the live APIs"""
    with httpx.Client(follow_redirects=True, timeout=TIMEOUT) as res:
        yield res


@pytest.fixture
def search_or_skip(skip_or_fail):
    """
    Get a function which searches one live node, skipping if that node will not answer

    A node which failed in any other way,
    e.g. by answering with something we could not read,
    fails the test instead.

    The function takes `(query, api, client, limit, observer=None)`.
    If `observer` is given it is passed through to `search`.
    """

    def search_one(query, facade, client, limit, observer=None):
        try:
            return search(
                query,
                build_list_selector([facade]),
                limit=limit,
                client=client,
                api_call_observer=observer,
            )
        except NoFacadeAnsweredError as exc:
            skip_or_fail(
                exc.failures,
                did_not_answer=CouldNotGetSearchResponseError,
                reason=(
                    f"{facade.search_api.host} did not answer, so it is down or unwell"
                ),
            )

    return search_one


@pytest.mark.parametrize("facade, query", LIVE_CASES)
def test_search_returns_results(client, facade, query, recorded, search_or_skip):
    """
    A query we expect to match something comes back with matches

    Also checks that the search-API health was recorded: one row per attempt (a
    healthy node answers first try, but a flaky one may be retried), all for this
    host, with the final, successful attempt carrying the result count and timing.
    """
    observer, read_calls = recorded

    outcome = search_or_skip(query, facade, client, limit=5, observer=observer)

    facade_key = (
        facade.search_api.host,
        type(facade.search_api).__name__,
        facade.parameters.base_query_style.__name__,
    )
    assert outcome.n_matches[facade_key] > 0

    # One row per attempt, all for this host, timed; the last is the success.
    calls = read_calls()
    assert calls, "expected at least one recorded call"
    assert all(call.host == facade.search_api.host for call in calls)
    assert all(call.response_time_seconds > 0.0 for call in calls)
    assert [call.attempt_number for call in calls] == list(range(1, len(calls) + 1))
    success = calls[-1]
    assert success.success is True
    assert success.response_code == 200
    assert success.num_results == outcome.n_matches[facade_key]


@pytest.mark.parametrize("facade, query, poison_field", FACET_NAME_CASES)
def test_search_applies_the_facets_we_send(  # noqa: PLR0913 - parametrised, plus fixtures
    client, facade, query, poison_field, recorded, search_or_skip
):
    """
    Test that the facade understood the facet names we sent it

    We take the query which does match data and change one facet
    to a value nothing can have.
    If the API is applying that facet, nothing comes back.
    If it came back with matches, it ignored the name we used,
    which means our name for that facet is wrong
    and every search we build with it is quietly unfiltered.

    This also exercises the health path where a request succeeds but matches
    nothing: the call is recorded as a success with a zero result count.
    """
    observer, read_calls = recorded

    nonsense = query.model_copy(update={poison_field: (NOT_A_REAL_VALUE,)})
    outcome = search_or_skip(nonsense, facade, client, limit=5, observer=observer)

    facade_key = (
        facade.search_api.host,
        type(facade.search_api).__name__,
        facade.parameters.base_query_style.__name__,
    )
    assert outcome.n_matches[facade_key] == 0

    # A response that matched nothing is still a successful call, and recorded.
    # One row per attempt; the final, successful one carries the zero count.
    calls = read_calls()
    assert calls, "expected at least one recorded call"
    assert all(call.host == facade.search_api.host for call in calls)
    assert all(call.response_time_seconds > 0.0 for call in calls)
    success = calls[-1]
    assert success.success is True
    assert success.num_results == 0


@pytest.mark.parametrize("api, query", LIVE_CASES)
def test_search_pages_through_all_the_results(
    client, api, query, search_or_skip, skip_or_fail
):
    """
    Paging reassembles the whole result set from the live APIs
    """
    host = api.search_api.host
    facade_key = (
        api.search_api.host,
        type(api.search_api).__name__,
        api.parameters.base_query_style.__name__,
    )

    # Probe with the smallest page to learn how much matches, so we can size the
    # real page to need only a few requests.
    probe = search_or_skip(query, api, client, limit=1)
    total = probe.n_matches[facade_key]
    if total is None or total < 2:
        pytest.skip(
            f"{host} matched {total} for this query, too few to need more than one page"
        )

    # Aim for ~4 pages, but always at least two (page smaller than the total).
    page_size = min(max(1, total // 4), total - 1)

    pages: list[int] = []

    try:
        outcome = search(
            query,
            build_list_selector([api]),
            limit=page_size,
            client=client,
            processor=lambda _facade, parsed: pages.append(len(parsed)),
        )
    except NoFacadeAnsweredError as exc:
        skip_or_fail(
            exc.failures,
            did_not_answer=CouldNotGetSearchResponseError,
            reason=f"{host} stopped answering part way through paging",
        )

    # More than one page was actually fetched...
    assert len(pages) > 1, (
        f"{host} was paged at {page_size} of {total}, but only one page was fetched"
    )
    # ...and every matched record was collected across those pages.
    assert len(outcome.parsed_docs[facade_key]) == total, (
        f"{host} said {total} matched but paging collected "
        f"{len(outcome.parsed_docs[facade_key])} (the index may have shifted mid-scan)"
    )
    assert outcome.n_matches[facade_key] == total


def master_ids(documents: tuple[ParsedDocument, ...]) -> set[str]:
    """
    Read the unique dataset identifiers out of a host's parsed documents

    `id_project_specific` (the Solr `master_id`) is the identity of a dataset across
    the nodes that hold it and the versions it has had, so it is what "the same
    dataset" means here.

    Parameters
    ----------
    documents
        The parsed documents one host answered with

    Returns
    -------
    :
        The native ids of the datasets in `documents`
    """
    return {document.id_project_specific for document in documents}


def test_aggregating_over_nodes_finds_more_than_one_node(client, skip_or_fail):
    """
    Searching several nodes finds more unique datasets than searching one

    The nodes do not all hold the same data, so the union of what they each hold
    is larger than any single one of them. This is the reason the fan-out search
    (and, later, a merge across nodes) exists.

    With `distrib` off, each node answers only for the data it holds itself, so
    the comparison is between genuinely different holdings rather than between
    federation-wide sweeps that would mirror one another.
    """
    nodes = [
        SearchAPIFacade(
            parameters=ESGF1_CMIP6_FACADE_PARAMETERS,
            search_api=SearchAPIESGF1Solr(
                host, build_transient_retrying(2), distrib=False
            ),
            result_parser=SolrSingleRowResultParser(),
        )
        for host in (
            "esgf.nci.org.au",
            "esgf.ceda.ac.uk",
            "esgf-data.dkrz.de",
            "esg-dn1.nsc.liu.se",
        )
    ]

    try:
        outcome = search(
            CMIP6_QUERY,
            build_list_selector(nodes),
            stop_at_first_result=False,
            client=client,
        )
    except NoFacadeAnsweredError as exc:
        skip_or_fail(
            exc.failures,
            did_not_answer=CouldNotGetSearchResponseError,
            reason="no node answered, so there is nothing to aggregate",
        )

    per_host = {
        host: master_ids(documents) for host, documents in outcome.parsed_docs.items()
    }
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


@pytest.mark.parametrize("facade, make_query", AND_OR_CASES)
def test_search_ands_across_facets(client, facade, make_query, search_or_skip):
    """
    Test that facets AND across each other

    Every one of the four (variable, experiment) combinations returns data.
    Each combination that comes back is a variable ANDed with an experiment,
    so seeing all four means each variable is usable with each experiment.

    This says nothing about how values combine *within* a facet;
    `test_search_ors_within_a_facet` is where that is tested.

    A combination nobody has published yet is skipped rather than failed:
    an empty answer to a query for data which does not exist tells us nothing
    about how facets combine, which is the only thing this test is asking.
    CMIP7 is the live example -- it is new, and much of it is still unpublished.
    """

    def count(variables, experiments):
        query = make_query(variables, experiments)
        outcome = search_or_skip(query, facade, client, limit=1)
        facade_key = (
            facade.search_api.host,
            type(facade.search_api).__name__,
            facade.parameters.base_query_style.__name__,
        )
        return outcome.n_matches[facade_key]

    for variable in AND_OR_VARIABLES:
        for experiment in AND_OR_EXPERIMENTS:
            if count((variable,), (experiment,)) == 0:
                pytest.skip(
                    f"{facade.search_api.host} has no data for variable={variable}, "
                    f"experiment={experiment}, so this combination cannot show "
                    "whether the facets ANDed"
                )


@pytest.mark.parametrize("facade, make_query", AND_OR_CASES)
def test_search_ors_within_a_facet(client, facade, make_query, search_or_skip):
    """
    Test that the values within a facet OR rather than one of them being dropped

    Asking for both variables at once has to match at least as much as asking
    for either alone: if a value were being dropped, or the values were being
    ANDed, the combined search would match no more than one of them
    (and, for an AND, almost certainly nothing at all).

    Counts are compared rather than equated because a dataset could in principle
    carry both variables, which would make the union smaller than the sum.

    Note: CMIP7 is new, so some of its combinations may not be published yet;
    this case can legitimately fail until that data exists.
    """

    def count(variables, experiments):
        query = make_query(variables, experiments)
        outcome = search_or_skip(query, facade, client, limit=1)
        facade_key = (
            facade.search_api.host,
            type(facade.search_api).__name__,
            facade.parameters.base_query_style.__name__,
        )
        return outcome.n_matches[facade_key]

    experiment = AND_OR_EXPERIMENTS[:1]
    separately = [count((variable,), experiment) for variable in AND_OR_VARIABLES]
    together = count(AND_OR_VARIABLES, experiment)

    if not all(found > 0 for found in separately):
        # Not a failure of the OR logic: there is simply no data to see it with.
        # `test_search_ands_across_facets` is where a missing combination is
        # reported, so saying it twice here would only be noise.
        pytest.skip(
            f"{facade.search_api.host} matched nothing for one of "
            f"{AND_OR_VARIABLES} on their own, so there is nothing to compare "
            "the combined search against"
        )

    assert together >= max(separately), (
        f"asking {facade.search_api.host} for {AND_OR_VARIABLES} together "
        f"matched {together}, "
        f"fewer than the {max(separately)} matched by one of them alone: "
        "the values are not being ORed within the facet"
    )


# TODO: eventually will test AND/OR logic again once populating Dataset. Testing
# what is returning (rather than simply results > 0).
# Eventually also will have higher level wrappers for more sophisticated
# search logic -> i.e. Malte's search example.


# --- Raw ESGF-NG east/west shape assumptions ---------------------------------
#
# Our STAC result parsing leans on a few things the two ESGF-NG deployments (east and
# west) publish: the project specific id in `properties.title`,
# the project in a top-level `collection` key
# and the match count in `numberMatched` or `numMatched`/`context.matched`.
# Those are pinned against fabricated docs in `tests/unit/search/test_result_parsers.py`
# and against recorded responses in `tests/unit/search/test_recorded_responses.py`,
# but a recording only notices a change when it is refreshed by hand.
# ESGF plans to converge east and west,
# so these assert the assumptions against the *live* responses: the day a
# deployment changes shape (or the two converge), the relevant test fails loudly rather
# than our parsers silently reading the wrong field.

EAST_HOST = "search.east.esgf.io"
WEST_HOST = "search.west.esgf.io"

CMIP7_QUERY = QueryCMIP7(variable_id="tas")
"""A CMIP7 query kept broad, because CMIP7 data is still sparse"""

# (project, host, query) for every live ESGF-NG STAC deployment we parse.
NG_STAC_CASES = (
    pytest.param("CMIP6", EAST_HOST, CMIP6_QUERY, id="cmip6-east"),
    pytest.param("CMIP6", WEST_HOST, CMIP6_QUERY, id="cmip6-west"),
    pytest.param("CMIP7", EAST_HOST, CMIP7_QUERY, id="cmip7-east"),
    pytest.param("CMIP7", WEST_HOST, CMIP7_QUERY, id="cmip7-west"),
)


def fetch_raw_stac_or_skip(client, project, host, query):
    """Fetch one live STAC response as raw JSON, skipping if there is nothing to check.

    Skips (rather than fails) if the node will not answer or returns no features: a node
    being down, or a project having no data yet, says nothing about whether our shape
    assumptions still hold.
    """
    facade = INBUILT_SEARCH_API_FACADE_STORE.get_api_facade_for_project_from_host(
        project, host
    )
    request = facade.build_search_request(to_canonical(query), 5)
    try:
        raw = fire(client, facade.search_api, request)
    except SearchAPIRequestError:
        pytest.skip(f"{host} did not answer, so it is down or unwell")

    features = raw.get("features") or []
    if not features:
        pytest.skip(f"{host} returned no {project} features, nothing to check")

    return raw, features


@pytest.mark.parametrize("project, host, query", NG_STAC_CASES)
def test_live_stac_project_specific_id_lives_in_title(client, project, host, query):
    """STAC features carry the project specific id in `properties.title`, everywhere."""
    _, features = fetch_raw_stac_or_skip(client, project, host, query)

    for feature in features:
        assert feature["properties"].get("title"), (
            f"{host} {project} feature {feature['id']!r} has no `properties.title`, "
            "which we read as the project specific id"
        )


@pytest.mark.parametrize("project, host, query", NG_STAC_CASES)
def test_live_stac_project_lives_in_collection(client, project, host, query):
    """STAC features carry the project in the top-level `collection` key, everywhere."""
    _, features = fetch_raw_stac_or_skip(client, project, host, query)

    for feature in features:
        assert feature.get("collection") == project, (
            f"{host} {project} feature {feature['id']!r} has `collection` "
            f"{feature.get('collection')!r}, which we read as the project"
        )


@pytest.mark.parametrize("project, host, query", NG_STAC_CASES)
def test_live_stac_match_count_key_matches_the_deployment(client, project, host, query):
    """East reports the count as `numberMatched`; west uses `numMatched`/`context`.

    The count is read by a deployment-specific reader (`stac_east_n_matches` vs
    `stac_west_n_matches`). If east dropped `numberMatched`, or west started sending it,
    that split -- and the readers that depend on it -- would be wrong.
    """
    raw, _ = fetch_raw_stac_or_skip(client, project, host, query)

    if host == EAST_HOST:
        assert "numberMatched" in raw, (
            f"{host} no longer reports the count as `numberMatched`"
        )
    else:
        assert "numberMatched" not in raw, (
            f"{host} now reports `numberMatched` (have east and west converged?)"
        )
        context = raw.get("context")
        has_west_count = "numMatched" in raw or (
            isinstance(context, dict) and "matched" in context
        )
        assert has_west_count, (
            f"{host} reports neither `numMatched` nor `context.matched`"
        )
