"""
Test the high-level `search` wrapper end to end against the live ESGF search APIs

The unit tests in `tests/unit/search/test_search_multi_project.py` pin the plumbing
(splitting, per-sub-query saving, the no-project error) against a mock API. This is the
live counterpart: run several project-specific queries through `search` and check that
rows for each project actually land in the database. If the unit tests pass and this
fails, the thing that changed is on the other end of the wire.

Only CMIP5 and CMIP6 are asserted on: both are well populated at NCI, so the queries
below reliably match a handful of datasets. CMIP7 is still sparse, which would turn this
into a near-permanent skip; the multi-project mechanics it would exercise are identical.
"""

from __future__ import annotations

import httpx
import pytest
from sqlmodel import Session, select

from esmporium.db import (
    Dataset,
    build_result_processor_factory,
    configure_sqlite_for_concurrency,
)
from esmporium.db.migrate import upgrade_to_head
from esmporium.query import QueryCMIP5, QueryCMIP6
from esmporium.search import (
    AllSubQueriesFailedError,
    NoFacadeAnsweredError,
    search,
)

pytestmark = pytest.mark.hits_esgf_search_api

TIMEOUT = 60.0
"""How long to wait for a node, in seconds"""

# One narrow query per project: a single model, so each project matches only a handful
# of datasets and the search stays small and fast. The project-specific facet *values*
# differ (CMIP5 and CMIP6 name the same model differently), which is why these are
# written as separate project-specific queries rather than one multi-project query.
QUERIES = (
    QueryCMIP5(
        experiment="historical", variable="tas", time_frequency="mon", model="ACCESS1.0"
    ),
    QueryCMIP6(
        experiment_id="historical",
        variable_id="tas",
        frequency="mon",
        source_id="ACCESS-CM2",
    ),
)


def saved_projects(engine) -> set[str]:
    """The distinct `project` values across every dataset saved in `engine`"""
    with Session(engine) as session:
        return {row.project for row in session.exec(select(Dataset)).all()}


def skip_if_a_node_was_down(outcomes) -> None:
    """
    Skip if any sub-query could not be reached, which says nothing about the wrapper

    A node being down shows up as an outcome with `answered` `False`. A real bug (e.g. a
    facet name we got wrong) is different: it comes back as a *successful, empty* answer
    (`answered` `True`, no rows), so the database assertion still catches it loudly.
    """
    if not all(outcome.answered for outcome in outcomes):
        pytest.skip("a node did not answer, so it is down or unwell")


def test_search_saves_results_for_several_projects(engine):
    """
    Running several project-specific queries through `search` saves each project's rows

    This is the whole point of the wrapper: one call, several queries, every result in
    the database. We assert the datasets are there afterwards, with no reference to the
    searches that found them, just as a real caller would read them back.
    """
    upgrade_to_head(engine)

    with httpx.Client(follow_redirects=True, timeout=TIMEOUT) as client:
        try:
            outcomes = search(
                QUERIES,
                limit=50,
                client=client,
                processor_factory=build_result_processor_factory(engine),
            )
        except (AllSubQueriesFailedError, NoFacadeAnsweredError):
            # Every project's nodes were down, so there is nothing to test today.
            pytest.skip("no node answered, so they are down or unwell")

    # A node down for only one project no longer aborts the run (it comes back as an
    # unanswered outcome), so skip on that too rather than failing the DB assertion.
    skip_if_a_node_was_down(outcomes)

    # One outcome per query came back...
    assert len(outcomes) == len(QUERIES)
    # ...and the same run saved rows for both projects.
    assert {"CMIP5", "CMIP6"} <= saved_projects(engine)


def test_search_saves_results_for_several_projects_in_parallel(engine):
    """
    Running the queries with parallel workers saves the same projects as a serial run

    Same call as above, but with `max_workers > 1` so the sub-queries run concurrently,
    each worker with its own session, all committing to the one SQLite database. The
    engine is configured for concurrency first (WAL + busy_timeout) so the workers'
    commits do not collide. The observable result is identical to the serial run.
    """
    configure_sqlite_for_concurrency(engine)
    upgrade_to_head(engine)

    with httpx.Client(follow_redirects=True, timeout=TIMEOUT) as client:
        try:
            outcomes = search(
                QUERIES,
                limit=50,
                client=client,
                processor_factory=build_result_processor_factory(engine),
                max_workers=len(QUERIES),
            )
        except (AllSubQueriesFailedError, NoFacadeAnsweredError):
            pytest.skip("no node answered, so they are down or unwell")

    skip_if_a_node_was_down(outcomes)

    assert len(outcomes) == len(QUERIES)
    assert {"CMIP5", "CMIP6"} <= saved_projects(engine)
