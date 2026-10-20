"""
Regression tests on what a search summary looks like

The value here is not that the text is right, it's that it is *visible*. A change to
a heading, a column or the order of sections is invisible in a diff of the renderer
and obvious in a diff of its output, and these summaries are the thing a person
actually reads every morning.

Every summary is built by hand, so none of this needs a database. What is being
checked is the rendering, and the rendering is a function of a
[SearchSummary][esmporium.db.SearchSummary] alone.

When one of these fails, that is often correct and expected. Regenerate the files
with the `--force-regen` flag (from pytest-regressions), then read the resulting
diff and check it is a change you meant to make.
"""

from __future__ import annotations

import pytest

from esmporium.db.schema import DatasetChangeType

ADDED = DatasetChangeType.ADDED
REMOVED = DatasetChangeType.REMOVED
MODIFIED = DatasetChangeType.MODIFIED


@pytest.fixture
def everything(summary_scenes):
    """Get the datasets a pattern-effect search returns, as the newest run saw them."""
    return (
        summary_scenes.version(
            "ACCESS-ESM1-6",
            "historical",
            "r1i1p1f1",
            101,
            data_nodes=("a.org", "b.gov"),
        ),
        summary_scenes.version("ACCESS-ESM1-6", "piControl", "r1i1p1f1", 102),
        summary_scenes.version("CESM3", "historical", "r1i1p1f1", 103),
        summary_scenes.version("CESM3", "piControl", "r1i1p1f1", 104),
        summary_scenes.version("MIROC7", "piControl", "r1i1p1f1", 105, retracted=True),
    )


def test_nothing_changed(file_regression, summary_scenes, everything):
    """
    What a quiet morning looks like

    The most common summary by a long way, so it is worth it being short.
    """
    summary = summary_scenes.summary(current=everything)

    file_regression.check(summary.render(), extension=".txt")


def test_every_kind_of_change(file_regression, summary_scenes, everything):
    """
    What a morning with real news looks like

    Arrivals (one of them already retracted), something gone, and two datasets whose
    metadata moved.
    """
    summary = summary_scenes.summary(
        changes=(
            summary_scenes.change(
                ADDED, of=summary_scenes.version("CESM3", "historical", "r1i1p1f1", 103)
            ),
            summary_scenes.change(
                ADDED, of=summary_scenes.version("CESM3", "piControl", "r1i1p1f1", 104)
            ),
            summary_scenes.change(
                ADDED,
                of=summary_scenes.version(
                    "MIROC7", "piControl", "r1i1p1f1", 105, retracted=True
                ),
            ),
            summary_scenes.change(
                REMOVED,
                of=summary_scenes.version(
                    "UKESM2", "historical", "r1i1p1f1", 107, version_string="20260801"
                ),
            ),
            summary_scenes.change(
                MODIFIED,
                of=summary_scenes.version(
                    "ACCESS-ESM1-6", "piControl", "r1i1p1f1", 102, retracted=True
                ),
                detail={"retracted": {"old": False, "new": True}},
            ),
            summary_scenes.change(
                MODIFIED,
                of=summary_scenes.version(
                    "ACCESS-ESM1-6",
                    "historical",
                    "r1i1p1f1",
                    101,
                    data_nodes=("a.org", "b.gov"),
                ),
                detail={"data_nodes": {"old": ["a.org"], "new": ["a.org", "b.gov"]}},
            ),
        ),
        current=everything,
    )

    file_regression.check(summary.render(), extension=".txt")


def test_a_run_which_must_not_be_misread(file_regression, summary_scenes, everything):
    """
    What a broken run looks like

    This is the summary the health block exists for: one sub-search failed outright
    and another was truncated, so every removal below it is suspect. Without the
    health block, "piControl has disappeared" reads as news.
    """
    summary = summary_scenes.summary(
        health=summary_scenes.run(
            8,
            complete=False,
            leaves=(
                summary_scenes.leaf("historical.tas", n_matches=209, n_documents=50),
                summary_scenes.leaf(
                    "control.tas",
                    answered=False,
                    n_matches=None,
                    n_documents=0,
                    failure="AllSubQueriesFailedError: every endpoint refused",
                ),
            ),
        ),
        changes=(
            summary_scenes.change(
                REMOVED,
                of=summary_scenes.version("CESM3", "piControl", "r1i1p1f1", 104),
            ),
            summary_scenes.change(
                REMOVED,
                of=summary_scenes.version(
                    "ACCESS-ESM1-6", "piControl", "r1i1p1f1", 102
                ),
            ),
        ),
        current=everything[:1],
        window_runs=(8,),
        baseline_run_id=7,
    )

    file_regression.check(summary.render(), extension=".txt")


def test_a_window_of_several_runs(file_regression, summary_scenes, everything):
    """
    What a week in one summary looks like

    Nothing appears twice, the two versions which came and went again are counted
    rather than listed, and there is no baseline because the window starts at the
    requirement's first run.
    """
    summary = summary_scenes.summary(
        changes=(
            summary_scenes.change(
                ADDED,
                of=summary_scenes.version("CESM3", "historical", "r1i1p1f1", 103),
                first_run_id=3,
                last_run_id=3,
            ),
            summary_scenes.change(
                MODIFIED,
                of=summary_scenes.version(
                    "ACCESS-ESM1-6", "historical", "r1i1p1f1", 101, is_latest=False
                ),
                detail={
                    "is_latest": {"old": True, "new": False},
                    "data_nodes": {"old": ["a.org", "b.gov"], "new": ["a.org"]},
                },
                first_run_id=2,
                last_run_id=7,
            ),
        ),
        churned=2,
        current=everything,
        window_runs=(1, 2, 3, 4, 5, 6, 7),
        baseline_run_id=None,
    )

    file_regression.check(summary.render(), extension=".txt")


def test_more_rows_than_anyone_can_read(file_regression, summary_scenes):
    """
    What `max_rows` does when a model publishes every member at once

    The counts are still the real counts; it is only the listing which is cut.
    """
    members = tuple(
        summary_scenes.version("CESM3", "historical", f"r{member}i1p1f1", 200 + member)
        for member in range(1, 13)
    )
    summary = summary_scenes.summary(
        changes=tuple(summary_scenes.change(ADDED, of=member) for member in members),
        current=members,
    )

    file_regression.check(summary.render(max_rows=4), extension=".txt")


def test_a_search_which_returns_nothing(file_regression, summary_scenes):
    """
    What the first run of a search for data which does not exist yet looks like

    Worth pinning: this is what a watch on CMIP7 data looks like before the data
    arrives, so it is the first thing anyone setting one up will see.
    """
    summary = summary_scenes.summary(
        health=summary_scenes.run(
            1,
            leaves=(summary_scenes.leaf("historical.tas", n_matches=0, n_documents=0),),
        ),
        window_runs=(1,),
        baseline_run_id=None,
    )

    file_regression.check(summary.render(), extension=".txt")
