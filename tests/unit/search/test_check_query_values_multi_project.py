"""
Test the high-level multi-project `check_query_values` against a mock API

These cover the behaviours the wrapper adds on top of
`check_query_values_single_project`: splitting a query into one check per project,
running several queries, refusing a query with no project, returning one outcome per
sub-query, and that a real finding still surfaces through the wrapper. The
single-project checking behaviour itself is covered in `test_check_query_values.py`.
"""

from __future__ import annotations

import threading

import httpx
import pytest
from tenacity import Retrying, stop_after_attempt

from esmporium.query import NoTargetProjectError, Query
from esmporium.search import (
    ESGF1_CMIP6_FACADE_PARAMETERS,
    AllSubQueriesFailedError,
    NoFacadeAnsweredError,
    SearchAPIESGF1Solr,
    SearchAPIFacade,
    SolrSingleRowResultParser,
    build_list_selector,
    check_query_values,
)


def once() -> Retrying:
    """A retry policy which tries a single time and never sleeps."""
    return Retrying(stop=stop_after_attempt(1), reraise=True)


def client_for(handler) -> httpx.Client:
    """Build an httpx client whose requests are answered by `handler`"""
    return httpx.Client(transport=httpx.MockTransport(handler))


def never_asked(request):
    """A handler for the tests in which nothing should be sent anywhere."""
    pytest.fail(f"unexpected request to {request.url}")


def make_facade(host: str = "node") -> SearchAPIFacade:
    """A CMIP6-ESGF1 (Solr) facade the mock transport answers for"""
    return SearchAPIFacade(
        parameters=ESGF1_CMIP6_FACADE_PARAMETERS,
        search_api=SearchAPIESGF1Solr(host, once()),
        result_parser=SolrSingleRowResultParser(),
    )


def facet_values(**fields: object) -> httpx.Response:
    """
    Build a 200 Solr facet-values response

    Each keyword is a Solr facet-field name mapped to its blob (values interleaved with
    their counts), e.g. ``facet_values(experiment_id=["historical", 5])``.
    """
    return httpx.Response(200, json={"facet_counts": {"facet_fields": dict(fields)}})


def test_splits_a_multi_project_query_into_one_check_per_project():
    """A query naming two projects becomes two checks, one per project"""
    seen_projects: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_projects.append(request.url.params.get_list("project"))
        return facet_values(experiment_id=["historical", 5])

    outcomes = check_query_values(
        Query(project=("CMIP5", "CMIP6"), experiment="historical"),
        build_list_selector([make_facade()]),
        client=client_for(handler),
    )

    # One check (and so one outcome) per project, each carrying its own project facet.
    assert len(outcomes) == 2
    assert sorted(seen_projects) == [["CMIP5"], ["CMIP6"]]


def test_accepts_a_single_query_as_well_as_a_collection():
    """A bare query is treated as a one-query run, not iterated as a collection"""
    outcomes = check_query_values(
        Query(project=("CMIP6",), experiment="historical"),
        build_list_selector([make_facade()]),
        client=client_for(lambda r: facet_values(experiment_id=["historical", 5])),
    )

    assert len(outcomes) == 1


def test_runs_every_query_and_returns_one_outcome_each():
    """Several queries each run and each return their own outcome, in input order"""
    first = Query(project=("CMIP6",), experiment="historical")
    second = Query(project=("CMIP6",), variable="tas")

    outcomes = check_query_values(
        (first, second),
        build_list_selector([make_facade()]),
        client=client_for(
            lambda r: facet_values(
                experiment_id=["historical", 5], variable_id=["tas", 9]
            )
        ),
    )

    assert len(outcomes) == 2


def test_a_query_with_no_project_is_refused_before_any_work():
    """A query that names no project blows up before any endpoint is contacted"""
    with pytest.raises(NoTargetProjectError):
        check_query_values(
            Query(experiment="historical"),
            build_list_selector([make_facade()]),
            client=client_for(never_asked),
        )


def test_a_finding_surfaces_through_the_wrapper():
    """
    A real value problem still comes back through the multi-project entry point

    The source lists 'historical'; the query asks for 'Historical', so the wrapper's
    outcome should carry the wrong-case finding, proving results are not lost in the
    split-and-fan-out.
    """
    (outcome,) = check_query_values(
        Query(project=("CMIP6",), experiment="Historical"),
        build_list_selector([make_facade()]),
        client=client_for(lambda r: facet_values(experiment_id=["historical", 5])),
    )

    (report,) = outcome.reports.values()
    (finding,) = report.findings
    assert finding.value == "Historical"
    assert finding.kind == "case"
    assert finding.suggestions == ("historical",)


def test_parallelises_over_the_sub_queries():
    """
    With `max_workers > 1` the sub-queries are genuinely in flight at the same time

    Both facet-value requests must reach the handler together to pass the barrier. A
    sequential run would leave the second unsent while the first blocks, so the barrier
    would time out and the test would fail rather than hang.
    """
    barrier = threading.Barrier(2, timeout=5)

    def handler(request: httpx.Request) -> httpx.Response:
        barrier.wait()
        return facet_values(experiment_id=["historical", 5])

    outcomes = check_query_values(
        Query(project=("CMIP5", "CMIP6"), experiment="historical"),
        build_list_selector([make_facade()]),
        client=client_for(handler),
        max_workers=2,
    )

    assert len(outcomes) == 2


def failing_for(down_project: str):
    """A handler that answers every project but `down_project`, which it 500s"""

    def handler(request: httpx.Request) -> httpx.Response:
        (project,) = request.url.params.get_list("project")
        if project == down_project:
            return httpx.Response(500)
        return facet_values(experiment_id=["historical", 5])

    return handler


def test_a_failed_sub_query_does_not_sink_the_ones_that_answered():
    """One project's endpoints all failing does not stop the others being checked"""
    outcomes = check_query_values(
        Query(project=("CMIP5", "CMIP6"), experiment="historical"),
        build_list_selector([make_facade()]),
        client=client_for(failing_for("CMIP6")),
    )

    assert len(outcomes) == 2
    answered = [outcome for outcome in outcomes if outcome.answered]
    unreachable = [outcome for outcome in outcomes if not outcome.answered]
    assert len(answered) == 1
    assert len(unreachable) == 1
    assert unreachable[0].reports == {}
    assert unreachable[0].failures


def test_a_single_failed_sub_query_re_raises_its_own_error():
    """A one-project check that fails raises NoFacadeAnsweredError, wrapper or not"""
    with pytest.raises(NoFacadeAnsweredError):
        check_query_values(
            Query(project=("CMIP6",), experiment="historical"),
            build_list_selector([make_facade()]),
            client=client_for(failing_for("CMIP6")),
        )


def test_all_sub_queries_failing_raises_an_aggregate():
    """When every sub-query fails, one error gathers all of them, none lost"""
    with pytest.raises(AllSubQueriesFailedError) as excinfo:
        check_query_values(
            Query(project=("CMIP5", "CMIP6"), experiment="historical"),
            build_list_selector([make_facade()]),
            client=client_for(lambda r: httpx.Response(500)),
        )

    assert len(excinfo.value.failures) == 2
