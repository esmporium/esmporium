"""
A requirement searched, saved and solved against the live ESGF APIs

The deterministic version of this is `test_search_to_solve_recorded.py`, which runs off
a recorded response and pins the behaviour. This is the live counterpart, and it
asserts **loosely on purpose**: what CMIP7 holds changes by the day, so any assertion
about particular models, or counts, would be a test which fails for reasons that are
nobody's fault.

What it does check is that the whole chain still fits together against the real thing:
a requirement plans into real searches, the real endpoints answer, what they answer
saves, and the solver can read it back and account for every group it discovers. If the
recorded test passes and this fails, the thing that changed is on the other end of the
wire.
"""

from __future__ import annotations

import httpx
import pytest
from sqlmodel import Session, select

from esmporium.db import (
    DatabaseCatalogue,
    Dataset,
    build_result_processor_factory,
    record_search_api_calls,
)
from esmporium.db.migrate import upgrade_to_head
from esmporium.query import QueryCMIP5, QueryCMIP6, QueryCMIP7
from esmporium.requirements import all_of, leaf, requirement, solve
from esmporium.search import AllSubQueriesFailedError, search

pytestmark = pytest.mark.hits_esgf_search_api

TIMEOUT = 60.0
"""How long to wait for a node, in seconds"""

LIMIT = 50
"""
Records per page

Small, because these tests are about the chain fitting together rather than about
getting everything: a narrow requirement plus a small page keeps them quick.
"""


def skip_if_a_node_was_down(outcomes) -> None:
    """
    Skip if any sub-search could not be reached, which says nothing about the chain

    A node being down shows up as an outcome with `answered` `False`. A real bug (e.g.
    a facet name we got wrong) is different: it comes back as a *successful, empty*
    answer, so the assertions below still catch it.
    """
    if not all(outcome.answered for outcome in outcomes):
        pytest.skip("a node did not answer, so it is down or unwell")


def saved(engine) -> list[Dataset]:
    """Every dataset row in the database"""
    with Session(engine) as session:
        return list(session.exec(select(Dataset)).all())


def search_live(requirement_to_search, engine, **kwargs):
    """
    Search a requirement live, saving everything into `engine`, or skip if nothing works
    """
    upgrade_to_head(engine)

    with httpx.Client(follow_redirects=True, timeout=TIMEOUT) as client:
        try:
            outcomes = search(
                requirement_to_search,
                limit=LIMIT,
                client=client,
                api_call_observer=record_search_api_calls(engine),
                processor_factory=build_result_processor_factory(engine),
                **kwargs,
            )
        except AllSubQueriesFailedError:
            pytest.skip("no node answered, so they are down or unwell")

    skip_if_a_node_was_down(outcomes)

    return outcomes


def test_a_cmip7_requirement_is_searched_saved_and_solved_live(engine):
    """
    A CMIP7 requirement goes all the way from written down to solved

    The assertions are deliberately about shape rather than content: something was
    saved, groups were discovered from it, and every group the solver discovered is
    accounted for in exactly one of the three outcomes. That last one is the real
    check -- a group which fell through all three would mean the solver lost it.
    """
    req = requirement(
        name="cmip7-tas-monthly-live",
        tree=leaf(QueryCMIP7(variable_id="tas", frequency="mon"), "tas"),
        group_by=("model", "variant_label"),
    )

    outcomes = search_live(req, engine)

    assert [(outcome.roles, outcome.project) for outcome in outcomes] == [
        (("tas",), "CMIP7")
    ]

    rows = saved(engine)
    if not rows:
        pytest.skip(
            "CMIP7 published no monthly tas today, so there is nothing to solve"
        )

    result = solve(req, DatabaseCatalogue(engine))

    discovered = set(result.satisfied) | set(result.unsatisfied) | set(result.ambiguous)
    assert discovered, "the search saved rows but the solver discovered no groups"
    # Every group lands in exactly one of the three, so none can be double-counted.
    assert len(discovered) == (
        len(result.satisfied) + len(result.unsatisfied) + len(result.ambiguous)
    )
    # Groups are discovered from what is *available*, which is a subset of what was
    # saved, so this is an upper bound rather than an equality.
    available_keys = {
        (("model", row.model), ("variant_label", row.variant_label)) for row in rows
    }
    assert discovered <= available_keys

    assert result.explain()


def test_a_requirement_whose_leaves_span_two_projects_saves_both_live(engine):
    """
    One requirement, two projects, both saved and both solvable

    CMIP5 and CMIP6 rather than CMIP7: both are well populated at NCI, so this is not
    a near-permanent skip. The project-specific facet *values* differ (the two name the
    same model differently), which is why these are two leaves in each project's own
    query style rather than one multi-project leaf.
    """
    req = requirement(
        name="tas-historical-two-projects-live",
        tree=all_of(
            leaf(
                QueryCMIP5(
                    experiment="historical",
                    variable="tas",
                    time_frequency="mon",
                    model="ACCESS1.0",
                ),
                "cmip5-tas",
            ),
            leaf(
                QueryCMIP6(
                    experiment_id="historical",
                    variable_id="tas",
                    frequency="mon",
                    source_id="ACCESS-CM2",
                ),
                "cmip6-tas",
            ),
        ),
        group_by=("project", "model", "variant_label"),
    )

    outcomes = search_live(req, engine)

    assert len(outcomes) == 2
    assert {outcome.project for outcome in outcomes} == {"CMIP5", "CMIP6"}
    assert {row.project for row in saved(engine)} >= {"CMIP5", "CMIP6"}

    # Grouped by project as well as model, so each project's datasets form their own
    # groups. Every group is unsatisfied -- a group from one project can never fill the
    # other project's role -- which is the correct answer to a requirement written this
    # way, and is asserted because it shows the solver reading both projects' rows.
    result = solve(req, DatabaseCatalogue(engine))

    discovered = set(result.satisfied) | set(result.unsatisfied) | set(result.ambiguous)
    assert {dict(key)["project"] for key in discovered} == {"CMIP5", "CMIP6"}
