"""
Test the search step end to end against the live ESGF search APIs

The unit tests in `tests/unit/search/test_search.py` already
pin the varous steps plumbing and failure modes,
so if those pass and these fail,
the thing that changed is on the other end of the wire.
"""

from __future__ import annotations

import re

import httpx
import pytest

from esmporium.query import QueryCMIP5, QueryCMIP6, QueryCMIP7, to_canonical
from esmporium.search import (
    ESGF1_CMIP6_FACADE_PARAMETERS,
    INBUILT_SEARCH_API_FACADE_STORE,
    NoAPIWouldAnswerError,
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


def search_or_skip(query, api, client, limit, observer=None):
    """
    Search one live node, skipping the test if that node will not answer

    A node being down says nothing about the behaviour under test,
    so it is a skip rather than a failure.

    If `observer` is given it is passed through to `search`, so a caller can
    assert on the search-API health recorded for the call.
    """
    try:
        return search(
            query,
            build_list_selector([api]),
            limit=limit,
            client=client,
            api_call_observer=observer,
        )
    except NoAPIWouldAnswerError:
        pytest.skip(f"{api.search_api.host} did not answer, so it is down or unwell")


@pytest.mark.parametrize("api, query", LIVE_CASES)
def test_search_returns_results(client, api, query, recorded):
    """A query we expect to match something comes back with matches

    Also checks that the search-API health was recorded: one row per attempt (a
    healthy node answers first try, but a flaky one may be retried), all for this
    host, with the final, successful attempt carrying the result count and timing.
    """
    observer, read_calls = recorded

    outcome = search_or_skip(query, api, client, limit=5, observer=observer)

    assert outcome.n_matches[api.search_api.host] > 0

    # One row per attempt, all for this host, timed; the last is the success.
    calls = read_calls()
    assert calls, "expected at least one recorded call"
    assert all(call.host == api.search_api.host for call in calls)
    assert all(call.response_time_seconds > 0.0 for call in calls)
    assert [call.attempt_number for call in calls] == list(range(1, len(calls) + 1))
    success = calls[-1]
    assert success.success is True
    assert success.response_code == 200
    assert success.num_results == outcome.n_matches[api.search_api.host]


@pytest.mark.parametrize("api, query, poison_field", FACET_NAME_CASES)
def test_search_applies_the_facets_we_send(client, api, query, poison_field, recorded):
    """
    Test that the API understood the facet names we sent it

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
    outcome = search_or_skip(nonsense, api, client, limit=5, observer=observer)

    assert outcome.n_matches[api.search_api.host] == 0

    # A response that matched nothing is still a successful call, and recorded.
    # One row per attempt; the final, successful one carries the zero count.
    calls = read_calls()
    assert calls, "expected at least one recorded call"
    assert all(call.host == api.search_api.host for call in calls)
    assert all(call.response_time_seconds > 0.0 for call in calls)
    success = calls[-1]
    assert success.success is True
    assert success.num_results == 0


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
    except NoAPIWouldAnswerError:
        pytest.skip("no node answered, so there is nothing to aggregate")

    per_host = {
        host: master_ids(documents) for host, documents in outcome.datasets.items()
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


@pytest.mark.parametrize("api, make_query", AND_OR_CASES)
def test_search_ands_across_facets(client, api, make_query):
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
        outcome = search_or_skip(query, api, client, limit=1)
        return outcome.n_matches[api.search_api.host]

    for variable in AND_OR_VARIABLES:
        for experiment in AND_OR_EXPERIMENTS:
            if count((variable,), (experiment,)) == 0:
                pytest.skip(
                    f"{api.search_api.host} has no data for variable={variable}, "
                    f"experiment={experiment}, so this combination cannot show "
                    "whether the facets ANDed"
                )


@pytest.mark.parametrize("api, make_query", AND_OR_CASES)
def test_search_ors_within_a_facet(client, api, make_query):
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
        outcome = search_or_skip(query, api, client, limit=1)
        return outcome.n_matches[api.search_api.host]

    experiment = AND_OR_EXPERIMENTS[:1]
    separately = [count((variable,), experiment) for variable in AND_OR_VARIABLES]
    together = count(AND_OR_VARIABLES, experiment)

    if not all(found > 0 for found in separately):
        # Not a failure of the OR logic: there is simply no data to see it with.
        # `test_search_ands_across_facets` is where a missing combination is
        # reported, so saying it twice here would only be noise.
        pytest.skip(
            f"{api.search_api.host} matched nothing for one of "
            f"{AND_OR_VARIABLES} on their own, so there is nothing to compare "
            "the combined search against"
        )

    assert together >= max(separately), (
        f"asking {api.search_api.host} for {AND_OR_VARIABLES} together "
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
# Our STAC result parsing leans on a few differences between the two ESGF-NG
# deployments (east and west): where the bundle id lives (`base_id` vs the feature id),
# where the project lives (`cmip6:mip_era` vs a `project` property), and where the match
# count lives (`numberMatched` vs `numMatched`/`context.matched`). Those are pinned
# against fabricated docs in `tests/unit/search/test_result_parsers.py` and against
# recorded responses in `tests/unit/search/test_recorded_responses.py`, but a recording
# only notices a change when it is refreshed by hand. ESGF plans to converge east and
# west, so these assert the assumptions against the *live* responses: the day a
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

CMIP6_STAC_CASES = tuple(case for case in NG_STAC_CASES if "cmip6" in case.id)
CMIP7_STAC_CASES = tuple(case for case in NG_STAC_CASES if "cmip7" in case.id)

VERSION_TOKEN = re.compile(r"v\d+")
"""A STAC feature id's trailing version token, e.g. the `v20240101` in `...tas.v20240101`."""  # noqa: E501


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


@pytest.mark.parametrize(
    "project, host, query, expect_base_id",
    [
        pytest.param("CMIP6", EAST_HOST, CMIP6_QUERY, True, id="cmip6-east"),
        pytest.param("CMIP6", WEST_HOST, CMIP6_QUERY, False, id="cmip6-west"),
        pytest.param("CMIP7", EAST_HOST, CMIP7_QUERY, False, id="cmip7-east"),
        pytest.param("CMIP7", WEST_HOST, CMIP7_QUERY, False, id="cmip7-west"),
    ],
)
def test_live_stac_base_id_assumption(client, project, host, query, expect_base_id):
    """Only east's CMIP6 features carry `base_id`; west's do not, nor does either CMIP7.

    We read the bundle id from `base_id` where it exists and recover it from the feature
    id otherwise (see `ESGFNGCMIP6ResultParser` / `ESGFNGCMIP7ResultParser`). If a
    deployment gains or loses `base_id`, that choice silently becomes wrong.
    """
    _, features = fetch_raw_stac_or_skip(client, project, host, query)

    carry_base_id = ["base_id" in feature["properties"] for feature in features]
    if expect_base_id:
        assert all(carry_base_id), (
            f"{host} {project} features no longer all carry `base_id`, "
            "which the parser reads the bundle id from here"
        )
    else:
        assert not any(carry_base_id), (
            f"{host} {project} features now carry `base_id`; here we recover the "
            "bundle id from the feature id and would ignore a `base_id`"
        )


@pytest.mark.parametrize("project, host, query", CMIP6_STAC_CASES)
def test_live_cmip6_project_lives_in_mip_era(client, project, host, query):
    """CMIP6 STAC features carry the project in `cmip6:mip_era`, on both deployments.

    We read the CMIP6 project from `cmip6:mip_era` rather than a plain `project`
    property because east carries no `project` property at all (west does, as of
    writing) -- so `cmip6:mip_era`, which both carry, is the field that works
    everywhere. What breaks us is `cmip6:mip_era` going away; a `project` property
    appearing does not, so we do not assert on it here (it already differs between the
    deployments).
    """
    _, features = fetch_raw_stac_or_skip(client, project, host, query)

    for feature in features:
        assert "cmip6:mip_era" in feature["properties"], (
            f"{host} CMIP6 feature has no `cmip6:mip_era`, which we read as the project"
        )


@pytest.mark.parametrize("project, host, query", CMIP7_STAC_CASES)
def test_live_cmip7_project_is_a_plain_property(client, project, host, query):
    """CMIP7 STAC features carry the project as a plain `project` property.

    Unlike CMIP6 (which has none), so if it disappears our CMIP7 project reading breaks.
    """
    _, features = fetch_raw_stac_or_skip(client, project, host, query)

    for feature in features:
        assert "project" in feature["properties"], (
            f"{host} CMIP7 feature has no `project` property, "
            "which we read as the project"
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


@pytest.mark.parametrize("project, host, query", NG_STAC_CASES)
def test_live_stac_feature_id_ends_with_a_version_token(client, project, host, query):
    """A STAC feature id ends in a `.vYYYYMMDD` token.

    We recover a bundle id by dropping that token (west, and both CMIP7); if the id
    shape changes, that recovery silently produces the wrong id.
    """
    _, features = fetch_raw_stac_or_skip(client, project, host, query)

    for feature in features:
        last_segment = feature["id"].rsplit(".", 1)[-1]
        assert VERSION_TOKEN.fullmatch(last_segment), (
            f"{host} feature id {feature['id']!r} does not end with a version token"
        )
