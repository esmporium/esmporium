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
    search_single_project,
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


def count_or_skip(client, facade, query):
    """
    Ask one live node how many records match, in a single request

    `search` cannot do this: its `limit` is the page size, not a cap, so it follows
    the endpoint's pagination to the end however small `limit` is. A test which only
    wants the total would therefore fetch the whole result set to learn it, which on
    a large query is thousands of requests. The total is in the first response, so we
    send one request and read it.

    A node which will not answer skips the test, as in `search_or_skip`: it is down or
    unwell, which says nothing about the behaviour under test. A node which answers
    without a count we can read still raises, because that is a change in response
    shape, which is exactly what the live tests exist to notice.

    Parameters
    ----------
    client
        The HTTP client to ask with

    facade
        The facade to ask

    query
        The query to count the matches for

    Returns
    -------
    :
        How many records the node reported matched `query`
    """
    request = facade.build_search_request(to_canonical(query), 1)
    try:
        raw = fire(client, facade.search_api, request)
    except SearchAPIRequestError:
        pytest.skip(f"{facade.search_api.host} did not answer, so it is down or unwell")

    return facade.get_n_matches(raw)


@pytest.fixture
def search_or_skip(skip_or_fail):
    """
    Get a function which searches one live node, skipping if that node will not answer

    A node which failed in any other way,
    e.g. by answering with something we could not read,
    fails the test instead.

    The function takes `(query, facade, client, limit, observer=None)`.
    If `observer` is given it is passed through to `search`.

    Note that `limit` is the page size, not a cap: a search which matches more than
    `limit` pages through the rest. Use `count_or_skip` where only the total is wanted.
    """

    def search_one(query, facade, client, limit, observer=None):
        try:
            return search_single_project(
                query,
                build_list_selector([facade]),
                limit=limit,
                client=client,
                api_call_observer=observer,
            )
        except NoFacadeAnsweredError as exc:
            skip_or_fail(
                exc.failures,
                skippable=CouldNotGetSearchResponseError,
                reason=(
                    f"{facade.search_api.host} did not answer, so it is down or unwell"
                ),
            )

    return search_one


@pytest.mark.parametrize("facade, query", LIVE_CASES)
def test_search_returns_results(client, facade, query, recorded, search_or_skip):
    """
    A query we expect to match something comes back with matches

    Also checks that the search-API health was recorded: one row per request, all for
    this host and all timed, with the last one the success that carries the count.

    A request is not the same as a row. A row is recorded per *HTTP request*, which is
    one per page plus one per retry within a page, and `attempt_number` counts only
    the retries, restarting at 1 for each new page. So the rows here are not a `1..N`
    run: how many there are, and which numbers they carry, is up to how the host
    chose to page and whether it had to be retried. `tests/unit/search/test_health.py`
    pins both shapes against a stub, which is where that belongs; here we assert only
    what holds however the live host behaved.
    """
    observer, read_calls = recorded

    # A page big enough for the whole result set, so this is normally one request.
    # Paging is not what this test is about, and at a small page size a popular
    # query is hundreds of requests.
    outcome = search_or_skip(query, facade, client, limit=10_000, observer=observer)

    facade_key = (
        facade.search_api.host,
        type(facade.search_api).__name__,
        facade.parameters.base_query_style.__name__,
    )
    assert outcome.n_matches[facade_key] > 0

    # All for this host, all timed, all numbered; the last is the success.
    calls = read_calls()
    assert calls, "expected at least one recorded call"
    assert all(call.host == facade.search_api.host for call in calls)
    assert all(call.response_time_seconds > 0.0 for call in calls)
    assert all(call.attempt_number >= 1 for call in calls)
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

    The unpoisoned query is counted first, as a control. Without it this test passes
    on any host which matches nothing anyway, and so proves nothing: `0 == 0` whether
    the facet was applied or ignored. That is not hypothetical -- the CMIP6 bridge
    facade spent a while asking ORNL for CMIP5's facet names, which matched nothing at
    all, and this test passed throughout.

    This also exercises the health path where a request succeeds but matches
    nothing: the call is recorded as a success with a zero result count.
    """
    observer, read_calls = recorded

    if count_or_skip(client, facade, query) == 0:
        pytest.skip(
            f"{facade.search_api.host} matches nothing for this query even unpoisoned, "
            "so a poisoned facet matching nothing cannot show the facet was applied"
        )

    nonsense = query.model_copy(update={poison_field: (NOT_A_REAL_VALUE,)})
    outcome = search_or_skip(nonsense, facade, client, limit=5, observer=observer)

    facade_key = (
        facade.search_api.host,
        type(facade.search_api).__name__,
        facade.parameters.base_query_style.__name__,
    )
    assert outcome.n_matches[facade_key] == 0

    # A response that matched nothing is still a successful call, and recorded.
    # Matching nothing is one page, so the last row is the only one unless the host
    # had to be retried; either way it is the success, and it carries the zero count.
    calls = read_calls()
    assert calls, "expected at least one recorded call"
    assert all(call.host == facade.search_api.host for call in calls)
    assert all(call.response_time_seconds > 0.0 for call in calls)
    success = calls[-1]
    assert success.success is True
    assert success.num_results == 0


PAGING_DRIFT_TOLERANCE = 0.02
"""
How far the records collected by paging may fall from the total reported, as a fraction

The total is read from the first page, but the index keeps being published to while we
page through the rest, so the two disagree by however much moved during the scan. We
have seen this live: a CMIP7 scan reported 569 and collected 571, and that same query
reported 664 a fortnight later.

The tolerance has to stay well under one page, because the bugs this is guarding
against are page-sized: paging that stops early loses a whole page, and paging that
re-walks loses or repeats one. We page in about four, so a page is ~25% of the total
and a couple of percent of drift cannot hide one.
"""


def assert_paging_covered_the_result_set(
    host: str, *, collected: int, total: int | None
) -> None:
    """
    Check that paging collected the whole result set, give or take a moving index

    Parameters
    ----------
    host
        The host that was paged through, named in the failure

    collected
        How many records paging actually collected, across every page

    total
        How many records the first page reported matched,
        or `None` if that response carried no count we could read
    """
    if total is None:
        pytest.skip(f"{host} reported no total, so there is nothing to compare against")

    allowed = max(1, round(total * PAGING_DRIFT_TOLERANCE))

    assert abs(collected - total) <= allowed, (
        f"{host} said {total} matched but paging collected {collected}, "
        f"which is more than the {allowed} record(s) of drift we allow for the index "
        "shifting mid-scan: paging is losing or repeating records"
    )


@pytest.mark.parametrize("api, query", LIVE_CASES)
def test_search_pages_through_all_the_results(client, api, query, skip_or_fail):
    """
    Paging reassembles the whole result set from the live APIs
    """
    host = api.search_api.host
    facade_key = (
        api.search_api.host,
        type(api.search_api).__name__,
        api.parameters.base_query_style.__name__,
    )

    # Ask how much matches in one request, so we can size the page to need only a
    # few. A search cannot tell us this cheaply: its `limit` is the page size, not a
    # cap, so sizing the page from a search would mean fetching everything first.
    matched = count_or_skip(client, api, query)
    if matched < 2:
        pytest.skip(
            f"{host} matched {matched} for this query, "
            "too few to need more than one page"
        )

    # Aim for ~4 pages, but always at least two (page smaller than the total).
    page_size = min(max(1, matched // 4), matched - 1)

    pages: list[int] = []

    try:
        outcome = search_single_project(
            query,
            build_list_selector([api]),
            limit=page_size,
            client=client,
            processor=lambda _facade, parsed: pages.append(len(parsed)),
        )
    except NoFacadeAnsweredError as exc:
        skip_or_fail(
            exc.failures,
            skippable=CouldNotGetSearchResponseError,
            reason=f"{host} stopped answering part way through paging",
        )

    # The count this scan itself was told, rather than the one the sizing request got:
    # the two are separate requests, so on a live index they can already disagree.
    total = outcome.n_matches[facade_key]

    # More than one page was actually fetched...
    assert len(pages) > 1, (
        f"{host} was paged at {page_size} of {total}, but only one page was fetched"
    )
    # ...and the pages between them covered the whole result set.
    assert_paging_covered_the_result_set(
        host, collected=len(outcome.parsed_docs[facade_key]), total=total
    )


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
        outcome = search_single_project(
            CMIP6_QUERY,
            build_list_selector(nodes),
            stop_at_first_result=False,
            client=client,
        )
    except NoFacadeAnsweredError as exc:
        skip_or_fail(
            exc.failures,
            skippable=CouldNotGetSearchResponseError,
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
def test_search_ands_across_facets(client, facade, make_query):
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
        return count_or_skip(client, facade, make_query(variables, experiments))

    for variable in AND_OR_VARIABLES:
        for experiment in AND_OR_EXPERIMENTS:
            if count((variable,), (experiment,)) == 0:
                pytest.skip(
                    f"{facade.search_api.host} has no data for variable={variable}, "
                    f"experiment={experiment}, so this combination cannot show "
                    "whether the facets ANDed"
                )


@pytest.mark.parametrize("facade, make_query", AND_OR_CASES)
def test_search_ors_within_a_facet(client, facade, make_query):
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
        return count_or_skip(client, facade, make_query(variables, experiments))

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
# and the match count in `numberMatched`.
# Those are pinned against fabricated docs in `tests/unit/search/test_result_parsers.py`
# and against recorded responses in `tests/unit/search/test_recorded_responses.py`,
# but a recording only notices a change when it is refreshed by hand.
# So these assert the assumptions against the *live* responses: the day a
# deployment changes shape, the relevant test fails loudly rather
# than our parsers silently reading the wrong field.
#
# The deployments used to differ on the count, and these tests are what told us they
# had stopped: west used to write `numMatched` and `context.matched` and no
# `numberMatched` at all, which is why there was a reader per deployment. It now
# writes the STAC spelling like east does, so one reader serves both and the
# assumption below is the same for every case.

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
def test_live_stac_match_count_key_reports_the_stac_spelling(
    client, project, host, query
):
    """Every deployment reports the count as `numberMatched`, which is STAC's spelling

    `stac_n_matches` reads that key first for all of them, and only falls back to the
    spellings west used to send. A deployment dropping it would mean we are reading
    the count out of a fallback without knowing, or not at all.
    """
    raw, _ = fetch_raw_stac_or_skip(client, project, host, query)

    assert "numberMatched" in raw, (
        f"{host} does not report the count as `numberMatched`, only: "
        f"{sorted(key for key in raw if key != 'features')}"
    )
