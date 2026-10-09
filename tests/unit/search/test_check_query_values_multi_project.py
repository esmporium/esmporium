"""
Test the high-level, requirement-driven `check_query_values` against a mock API

These cover the behaviours the wrapper adds on top of
`check_query_values_single_project`: turning a requirement into one check per leaf,
splitting each of those per project, checking a twice-asked-for query once, refusing a
leaf with no project, saying which leaf each outcome is about, and that a real finding
still surfaces through the wrapper. The single-project checking behaviour itself is
covered in `test_check_query_values.py`.

It mirrors `test_search_multi_project.py` test for test, because
`check_query_values` mirrors `search`: both plan the same requirement the same way.
"""

from __future__ import annotations

import threading

import httpx
import pytest
from tenacity import Retrying, stop_after_attempt

from esmporium.query import NoTargetProjectError, Query
from esmporium.requirements import all_of, leaf, requirement
from esmporium.search import (
    ESGF1_CMIP6_FACADE_PARAMETERS,
    AllSubQueriesFailedError,
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


def a_requirement(*leaves, where=None):
    """Build a requirement from leaves, defaulting what the checker does not look at"""
    tree = leaves[0] if len(leaves) == 1 else all_of(*leaves)
    return requirement(name="an-analysis", tree=tree, group_by=("model",), where=where)


def for_experiments(*experiments, projects=("CMIP6",)):
    """A requirement with one leaf per experiment, each role named for its experiment"""
    return a_requirement(
        *(leaf(Query(experiment=experiment), experiment) for experiment in experiments),
        where=Query(project=projects),
    )


def test_splits_a_multi_project_leaf_into_one_check_per_project():
    """A leaf naming two projects becomes two checks, one per project"""
    seen_projects: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_projects.append(request.url.params.get_list("project"))
        return facet_values(experiment_id=["historical", 5])

    outcomes = check_query_values(
        for_experiments("historical", projects=("CMIP5", "CMIP6")),
        build_list_selector([make_facade()]),
        client=client_for(handler),
    )

    # One check (and so one outcome) per project, each carrying its own project facet.
    assert len(outcomes) == 2
    assert sorted(seen_projects) == [["CMIP5"], ["CMIP6"]]


def test_runs_every_leaf_and_returns_one_outcome_each():
    """Every leaf is checked and gets its own outcome, in tree order"""
    outcomes = check_query_values(
        a_requirement(
            leaf(Query(project=("CMIP6",), experiment="historical"), "historical"),
            leaf(Query(project=("CMIP6",), variable="tas"), "tas"),
        ),
        build_list_selector([make_facade()]),
        client=client_for(
            lambda r: facet_values(
                experiment_id=["historical", 5], variable_id=["tas", 9]
            )
        ),
    )

    assert [outcome.roles for outcome in outcomes] == [("historical",), ("tas",)]


def test_two_leaves_asking_the_same_thing_are_checked_once():
    """Checking the same query twice would be pure waste, so it happens once"""
    requests_made = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests_made
        requests_made += 1
        return facet_values(experiment_id=["historical", 5])

    same = Query(project=("CMIP6",), experiment="historical")
    outcomes = check_query_values(
        a_requirement(leaf(same, "field"), leaf(same, "reference")),
        build_list_selector([make_facade()]),
        client=client_for(handler),
    )

    assert requests_made == 1
    assert len(outcomes) == 1
    assert outcomes[0].roles == ("field", "reference")


def test_outcomes_carry_the_role_and_project_which_produced_them():
    """A report says which leaf it is about and which project answered"""
    outcomes = check_query_values(
        for_experiments("historical", projects=("CMIP5", "CMIP6")),
        build_list_selector([make_facade()]),
        client=client_for(lambda r: facet_values(experiment_id=["historical", 5])),
    )

    assert {(outcome.roles, outcome.project) for outcome in outcomes} == {
        (("historical",), "CMIP5"),
        (("historical",), "CMIP6"),
    }


def test_a_leaf_with_no_project_is_refused_before_any_work():
    """
    A leaf that names no project blows up before any endpoint is contacted

    And says which leaf, exactly as `search` does: the point of checking a whole
    requirement is not having to work out which part of it to go and look at.
    """
    with pytest.raises(NoTargetProjectError, match=r"'tas'.*'an-analysis'"):
        check_query_values(
            a_requirement(
                leaf(Query(project=("CMIP6",), experiment="historical"), "historical"),
                leaf(Query(variable="tas"), "tas"),
            ),
            build_list_selector([make_facade()]),
            client=client_for(never_asked),
        )


def test_a_finding_surfaces_through_the_wrapper():
    """
    A real value problem still comes back through the requirement entry point

    The source lists 'historical'; the requirement asks for 'Historical', so the
    outcome should carry the wrong-case finding, proving results are not lost in the
    plan-and-fan-out.
    """
    (only,) = check_query_values(
        for_experiments("Historical"),
        build_list_selector([make_facade()]),
        client=client_for(lambda r: facet_values(experiment_id=["historical", 5])),
    )

    (report,) = only.outcome.reports.values()
    (finding,) = report.findings
    assert finding.value == "Historical"
    assert finding.kind == "case"
    assert finding.suggestions == ("historical",)
    # ...and says which leaf asked for it, which is the whole reason to check a
    # requirement rather than a loose query.
    assert only.roles == ("Historical",)


def test_parallelises_over_the_sub_checks():
    """
    With `max_workers > 1` the sub-checks are genuinely in flight at the same time

    Both facet-value requests must reach the handler together to pass the barrier. A
    sequential run would leave the second unsent while the first blocks, so the barrier
    would time out and the test would fail rather than hang.
    """
    barrier = threading.Barrier(2, timeout=5)

    def handler(request: httpx.Request) -> httpx.Response:
        barrier.wait()
        return facet_values(experiment_id=["historical", 5])

    outcomes = check_query_values(
        for_experiments("historical", projects=("CMIP5", "CMIP6")),
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


def test_a_failed_sub_check_does_not_sink_the_ones_that_answered():
    """One project's endpoints all failing does not stop the others being checked"""
    outcomes = check_query_values(
        for_experiments("historical", projects=("CMIP5", "CMIP6")),
        build_list_selector([make_facade()]),
        client=client_for(failing_for("CMIP6")),
    )

    assert len(outcomes) == 2
    answered = [outcome for outcome in outcomes if outcome.answered]
    unreachable = [outcome for outcome in outcomes if not outcome.answered]
    assert len(answered) == 1
    assert len(unreachable) == 1
    assert unreachable[0].outcome.reports == {}
    assert unreachable[0].outcome.failures
    assert unreachable[0].project == "CMIP6"


@pytest.mark.parametrize(
    ("projects", "expected"),
    [
        pytest.param(("CMIP6",), 1, id="one-sub-check"),
        pytest.param(("CMIP5", "CMIP6"), 2, id="several-sub-checks"),
    ],
)
def test_every_sub_check_failing_raises_an_aggregate_naming_the_roles(
    projects, expected
):
    """
    When nothing answers, one error gathers every failure and says which is which

    Raised however many sub-checks there were, mirroring `search`: each failure
    belongs to a particular leaf and project, and a bare `NoFacadeAnsweredError` would
    throw that away.
    """
    with pytest.raises(AllSubQueriesFailedError) as excinfo:
        check_query_values(
            for_experiments("historical", projects=projects),
            build_list_selector([make_facade()]),
            client=client_for(lambda r: httpx.Response(500)),
        )

    assert len(excinfo.value.failures) == expected
    message = str(excinfo.value)
    for project in projects:
        assert f"'historical' ({project})" in message
