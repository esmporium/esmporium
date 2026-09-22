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

from esmporium.db import Dataset, build_result_processor_factory
from esmporium.db.migrate import upgrade_to_head
from esmporium.query import QueryCMIP5, QueryCMIP6
from esmporium.search import search

# The error raised when no endpoint gave us anything is being renamed
# (NoAPIAnsweredError -> NoFacadeAnsweredError) on another branch. Bind whichever exists
# so this test needs no edit when that branch merges; we only skip on it, we do not look
# inside it, so its changing shape does not reach us either.
try:
    from esmporium.search import NoFacadeAnsweredError as NobodyAnsweredError
except ImportError:  # pragma: no cover - depends on which branch is checked out
    from esmporium.search import NoAPIAnsweredError as NobodyAnsweredError

pytestmark = pytest.mark.hits_esgf_search_api

TIMEOUT = 60.0
"""How long to wait for a node, in seconds"""

# One narrow query per project: a single model, so each project matches only a handful
# of datasets and the search stays small and fast. The project-specific facet *values*
# differ (CMIP5 and CMIP6 name the same model differently), which is why these are
# written as separate project-specific queries rather than one multi-project query.
QUERIES = (
    QueryCMIP5(
        experiment="historical", variable="tas", time_frequency="mon", model="ACCESS1-0"
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
        except NobodyAnsweredError:
            # A node being down is the common cause here and says nothing about the
            # wrapper, so skip. A real bug (e.g. a facet name we got wrong) does not
            # look like this: it comes back as a successful, empty answer, so the DB
            # assertion below still catches it loudly.
            pytest.skip("a node did not answer, so it is down or unwell")

    # One outcome per query came back...
    assert len(outcomes) == len(QUERIES)
    # ...and the same run saved rows for both projects.
    assert {"CMIP5", "CMIP6"} <= saved_projects(engine)
