"""
Tests of how a window of recorded changes is folded, and of what a summary says
"""

from __future__ import annotations

import datetime

import pytest

from esmporium.datasets import DATASET_FACET_COLUMNS
from esmporium.db import (
    ConflictingWindowError,
    SearchRunInfo,
    SearchRunLeafInfo,
    collapse_changes,
    describe_moved,
    summarise_search,
)
from esmporium.db.schema import DatasetChangeType
from esmporium.db.summaries import _window

ADDED = DatasetChangeType.ADDED
REMOVED = DatasetChangeType.REMOVED
MODIFIED = DatasetChangeType.MODIFIED

RETRACTED_MOVED = {"retracted": {"old": False, "new": True}}
UNRETRACTED_MOVED = {"retracted": {"old": True, "new": False}}


def test_a_single_addition_is_an_addition(summary_scenes):
    net, net_unchanged = collapse_changes([(7, summary_scenes.recorded(ADDED))])

    assert net_unchanged == 0
    assert len(net) == 1
    assert net[0].change_type is ADDED
    assert net[0].detail is None


def test_added_then_modified_is_still_an_addition(summary_scenes):
    # The modification is folded into the arrival: the version's current state is
    # reported in the row, so saying "added" and "modified" would be saying it twice.
    net, net_unchanged = collapse_changes(
        [
            (3, summary_scenes.recorded(ADDED)),
            (4, summary_scenes.recorded(MODIFIED, detail=RETRACTED_MOVED)),
        ]
    )

    assert net_unchanged == 0
    assert len(net) == 1
    assert net[0].change_type is ADDED
    assert net[0].detail is None
    assert (net[0].first_run_id, net[0].last_run_id) == (3, 4)


def test_added_then_removed_is_no_net_change(summary_scenes):
    net, net_unchanged = collapse_changes(
        [(3, summary_scenes.recorded(ADDED)), (4, summary_scenes.recorded(REMOVED))]
    )

    assert net == ()
    assert net_unchanged == 1


def test_removed_then_added_is_no_net_change(summary_scenes):
    # It was there when the window opened and it is there now, so there is no news,
    # even though two changes were recorded about it.
    net, net_unchanged = collapse_changes(
        [(3, summary_scenes.recorded(REMOVED)), (4, summary_scenes.recorded(ADDED))]
    )

    assert net == ()
    assert net_unchanged == 1


def test_removal_is_a_removal(summary_scenes):
    net, net_unchanged = collapse_changes([(7, summary_scenes.recorded(REMOVED))])

    assert net_unchanged == 0
    assert net[0].change_type is REMOVED
    assert net[0].detail is None


def test_modified_twice_keeps_the_oldest_old_and_the_newest_new(summary_scenes):
    net, net_unchanged = collapse_changes(
        [
            (
                3,
                summary_scenes.recorded(
                    MODIFIED, detail={"is_latest": {"old": True, "new": False}}
                ),
            ),
            (
                4,
                summary_scenes.recorded(
                    MODIFIED, detail={"is_latest": {"old": False, "new": True}}
                ),
            ),
        ]
    )

    # Straight back to where it started, so nothing is left moved.
    assert net == ()
    assert net_unchanged == 1


def test_modified_twice_in_the_same_direction_reads_end_to_end(summary_scenes):
    net, net_unchanged = collapse_changes(
        [
            (
                3,
                summary_scenes.recorded(
                    MODIFIED,
                    detail={"data_nodes": {"old": ["a"], "new": ["a", "b"]}},
                ),
            ),
            (
                4,
                summary_scenes.recorded(
                    MODIFIED,
                    detail={"data_nodes": {"old": ["a", "b"], "new": ["a", "b", "c"]}},
                ),
            ),
        ]
    )

    assert net_unchanged == 0
    assert net[0].change_type is MODIFIED
    assert net[0].detail == {"data_nodes": {"old": ["a"], "new": ["a", "b", "c"]}}


def test_different_things_moving_in_different_runs_are_merged(summary_scenes):
    net, net_unchanged = collapse_changes(
        [
            (3, summary_scenes.recorded(MODIFIED, detail=RETRACTED_MOVED)),
            (
                4,
                summary_scenes.recorded(
                    MODIFIED, detail={"is_latest": {"old": True, "new": False}}
                ),
            ),
        ]
    )

    assert net_unchanged == 0
    assert net[0].detail == {
        "retracted": {"old": False, "new": True},
        "is_latest": {"old": True, "new": False},
    }


def test_versions_are_folded_independently(summary_scenes):
    net, net_unchanged = collapse_changes(
        [
            (3, summary_scenes.recorded(ADDED, dataset_version_id=1)),
            (3, summary_scenes.recorded(REMOVED, dataset_version_id=2)),
            (4, summary_scenes.recorded(REMOVED, dataset_version_id=1)),
            (
                4,
                summary_scenes.recorded(
                    MODIFIED, dataset_version_id=3, detail=RETRACTED_MOVED
                ),
            ),
        ]
    )

    assert net_unchanged == 1
    assert {change.dataset_version_id: change.change_type for change in net} == {
        2: REMOVED,
        3: MODIFIED,
    }


def test_nothing_recorded_is_no_changes():
    assert collapse_changes([]) == ((), 0)


# ------------------------------------------------------------------- what moved reads
@pytest.mark.parametrize(
    ("detail", "expected"),
    (
        pytest.param(None, "", id="nothing"),
        pytest.param({}, "", id="empty"),
        pytest.param(RETRACTED_MOVED, "retracted: no -> yes", id="retracted"),
        pytest.param(UNRETRACTED_MOVED, "retracted: yes -> no", id="unretracted"),
        pytest.param(
            {"data_nodes": {"old": ["a"], "new": ["a", "b"]}},
            "data_nodes: +b",
            id="node-joined",
        ),
        pytest.param(
            {"data_nodes": {"old": ["a", "b"], "new": ["a"]}},
            "data_nodes: -b",
            id="node-left",
        ),
        pytest.param(
            {"data_nodes": {"old": ["a"], "new": ["b"]}},
            "data_nodes: +b, -a",
            id="node-swapped",
        ),
        pytest.param(
            {
                "retracted": {"old": False, "new": True},
                "is_latest": {"old": True, "new": False},
            },
            "is_latest: yes -> no; retracted: no -> yes",
            id="two-things",
        ),
        pytest.param(
            # Only `retracted`, `is_latest` and `data_nodes` can move today. This is
            # the fallback for whatever we start recording next, so that adding a
            # tracked field does not silently render as nothing.
            {"something_new": {"old": "before", "new": "after"}},
            "something_new: before -> after",
            id="fallback-for-a-field-we-do-not-track-yet",
        ),
    ),
)
def test_describe_moved(detail, expected):
    assert describe_moved(detail) == expected


# ----------------------------------------------------------------------- the headline
def test_headline_says_nothing_when_nothing_changed(summary_scenes):
    assert summary_scenes.summary().headline == "pattern-effect: no changes"


def test_headline_counts_each_type_in_a_fixed_order(summary_scenes):
    summary = summary_scenes.summary(
        changes=(
            summary_scenes.change(MODIFIED, detail=RETRACTED_MOVED),
            summary_scenes.change(REMOVED),
            summary_scenes.change(ADDED),
            summary_scenes.change(ADDED),
        )
    )

    assert summary.headline == "pattern-effect: 2 added, 1 removed, 1 modified"
    assert summary.any_changes


def test_headline_says_so_when_the_run_was_incomplete(summary_scenes):
    summary = summary_scenes.summary(
        health=summary_scenes.run(complete=False),
        changes=(summary_scenes.change(REMOVED),),
    )

    assert summary.headline == "pattern-effect: 1 removed (run incomplete)"


def test_headline_says_so_when_an_incomplete_run_changed_nothing(summary_scenes):
    summary = summary_scenes.summary(health=summary_scenes.run(complete=False))

    assert summary.headline == "pattern-effect: no changes (run incomplete)"


# ------------------------------------------------------------------ what a run reports
def test_a_run_totals_its_sub_searches(summary_scenes):
    health = summary_scenes.run(
        leaves=(
            summary_scenes.leaf("historical.tas", n_matches=209, n_documents=50),
            summary_scenes.leaf("control.tas", n_matches=100, n_documents=100),
        )
    )

    assert health.n_leaves == 2
    assert health.n_answered == 2
    assert (health.n_documents, health.n_matches) == (150, 309)
    # Which of them fell short is carried, not just that one did: a caller reporting
    # this has to be able to name the sub-search whose answer is incomplete.
    assert [leaf.label for leaf in health.truncated] == ["'historical.tas' (CMIP7)"]


def test_a_run_whose_host_reported_no_total_has_no_total(summary_scenes):
    # Not "short by the missing part": one silent host makes the run's total unknown.
    health = summary_scenes.run(
        leaves=(
            summary_scenes.leaf("historical.tas", n_matches=None, n_documents=50),
            summary_scenes.leaf("control.tas", n_matches=100, n_documents=100),
        )
    )

    assert health.n_matches is None
    assert health.n_documents == 150


def test_a_run_carries_which_sub_searches_failed(summary_scenes):
    health = summary_scenes.run(
        leaves=(
            summary_scenes.leaf("historical.tas"),
            summary_scenes.leaf("control.tas", answered=False, failure="timed out"),
        )
    )

    assert health.n_answered == 1
    assert [leaf.label for leaf in health.failures] == ["'control.tas' (CMIP7)"]
    # Only the hosts whose answers were kept, so a failed leaf contributes none.
    assert health.hosts == ("search.east.esgf.io",)


def test_a_run_which_recorded_no_sub_searches_reports_nothing_rather_than_zero(
    summary_scenes,
):
    # A run which died before writing its leaves has no health to report. The
    # distinction a caller needs is "no sub-searches recorded" rather than "none
    # answered", which is why `n_leaves` is worth asking separately.
    health = summary_scenes.run(leaves=())

    assert health.n_leaves == 0
    assert health.n_answered == 0
    assert health.failures == ()
    assert health.truncated == ()


def test_an_arrival_which_is_already_retracted_says_so(summary_scenes):
    # "There is new data" and "there is new data you must not use" are different
    # pieces of news, so this is carried rather than left to be worked out.
    arrived = summary_scenes.change(ADDED, of=summary_scenes.version(retracted=True))

    assert arrived.added_but_retracted


def test_only_an_arrival_can_be_added_but_retracted(summary_scenes):
    retracted_later = summary_scenes.change(
        MODIFIED, of=summary_scenes.version(retracted=True), detail=RETRACTED_MOVED
    )

    assert not retracted_later.added_but_retracted


# --------------------------------------------------------------------------- the frames
def test_changes_frame_has_its_columns_even_when_empty(summary_scenes):
    frame = summary_scenes.summary().changes_frame()

    assert frame.empty
    assert "change" in frame.columns
    assert "model" in frame.columns


def test_changes_frame_has_one_row_per_change(summary_scenes):
    summary = summary_scenes.summary(
        changes=(
            summary_scenes.change(ADDED, of=summary_scenes.version(model="CESM3")),
            summary_scenes.change(MODIFIED, detail=RETRACTED_MOVED),
        )
    )

    frame = summary.changes_frame()

    assert list(frame["change"]) == ["added", "modified"]
    assert list(frame["model"]) == ["CESM3", "ACCESS-ESM1-6"]
    assert frame["moved"].tolist() == ["", "retracted: no -> yes"]


def test_current_frame_has_one_row_per_version(summary_scenes):
    summary = summary_scenes.summary(
        current=(
            summary_scenes.version(model="CESM3", dataset_version_id=1),
            summary_scenes.version(
                model="MIROC7", dataset_version_id=2, data_nodes=("a", "b")
            ),
        )
    )

    frame = summary.current_frame()

    assert list(frame["model"]) == ["CESM3", "MIROC7"]
    assert list(frame["data_nodes"]) == ["esgf.nci.org.au", "a, b"]


# ------------------------------------------------- what a run's sub-searches report
def leaf_info(roles=("historical.tas",), *, n_matches=209, n_documents=209):
    """One sub-search's outcome, built directly so the dataclass is what is tested."""
    return SearchRunLeafInfo(
        roles=roles,
        project="CMIP7",
        answered=True,
        host="search.east.esgf.io",
        n_matches=n_matches,
        n_documents=n_documents,
        failure=None,
    )


def test_a_sub_search_is_truncated_when_it_collected_fewer_than_matched():
    assert leaf_info(n_matches=209, n_documents=50).truncated


def test_a_sub_search_which_collected_everything_is_not_truncated():
    assert not leaf_info(n_matches=209, n_documents=209).truncated


def test_a_host_which_reported_no_total_is_not_truncated():
    # The decision this pins: "the host did not say how many matched" is not the same
    # as "documents are missing". Calling it truncated would turn every host which
    # reports no total into a permanent warning.
    assert not leaf_info(n_matches=None, n_documents=50).truncated


def test_a_sub_search_is_labelled_by_its_roles_and_project():
    assert leaf_info().label == "'historical.tas' (CMIP7)"


def test_a_sub_search_asked_for_under_several_roles_names_them_all():
    # Two leaves asking for the same dataset are searched for once, so a failure has
    # to say which roles it cost.
    assert (
        leaf_info(roles=("historical.tas", "chain.0.tas")).label
        == "'historical.tas' and 'chain.0.tas' (CMIP7)"
    )


# ---------------------------------------------------------------- the window arguments
def run_info(run_id, *, day=20, complete=True):
    """Get one stored run, as `get_search_runs` hands it back."""
    return SearchRunInfo(
        id=run_id,
        stored_requirement_id=1,
        started_at=datetime.datetime(2026, 10, day, tzinfo=datetime.timezone.utc),
        finished_at=datetime.datetime(2026, 10, day, tzinfo=datetime.timezone.utc),
        complete=complete,
    )


NEWEST_FIRST = (
    run_info(7, day=20),
    run_info(6, day=19),
    run_info(5, day=18),
    run_info(4, day=17),
)


def test_window_defaults_to_the_newest_run():
    assert [run.id for run in _window(NEWEST_FIRST, None, None)] == [7]


def test_window_counts_back_n_runs_oldest_first():
    assert [run.id for run in _window(NEWEST_FIRST, None, 3)] == [5, 6, 7]


def test_window_asking_for_more_runs_than_there_are_gives_them_all():
    assert [run.id for run in _window(NEWEST_FIRST, None, 99)] == [4, 5, 6, 7]


def test_window_since_a_run_id():
    assert [run.id for run in _window(NEWEST_FIRST, 5, None)] == [5, 6, 7]


def test_window_since_a_time():
    since = datetime.datetime(2026, 10, 18, tzinfo=datetime.timezone.utc)

    assert [run.id for run in _window(NEWEST_FIRST, since, None)] == [5, 6, 7]


def test_window_since_a_naive_time_is_read_as_utc():
    # SQLite gives recorded-at columns back without their offset, so both sides of
    # the comparison have to be read the same way.
    since = datetime.datetime(2026, 10, 18)

    assert [run.id for run in _window(NEWEST_FIRST, since, None)] == [5, 6, 7]


def test_window_can_select_nothing():
    since = datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc)

    assert _window(NEWEST_FIRST, since, None) == ()


def test_since_and_n_runs_together_is_refused():
    # The engine is never touched: the window is checked before anything is read,
    # which is what makes this a test of parameter parsing rather than of reading.
    with pytest.raises(ConflictingWindowError) as excinfo:
        summarise_search("not-an-engine", "pattern-effect", since=3, n_runs=2)

    # Carried as well as said, so a caller can branch on them rather than read prose.
    assert excinfo.value.since == 3
    assert excinfo.value.n_runs == 2
    assert str(excinfo.value) == (
        "A summary covers one window, but both since=3 and n_runs=2 were given. "
        "Pass since to count from a time or from a run id, or n_runs to count back "
        "a number of runs, not both."
    )


def test_n_runs_must_be_at_least_one():
    with pytest.raises(ValueError, match="n_runs must be at least one"):
        summarise_search("not-an-engine", "pattern-effect", n_runs=0)


# ------------------------------------------------------------- telling rows apart
def test_the_id_column_tells_rows_apart_whatever_facets_are_shown(summary_scenes):
    # Two datasets can agree on every facet we model and still be different rows, so
    # a caller which narrows the columns it shows must still have something which
    # distinguishes them. That is `dataset_version_id`, which every frame carries.
    summary = summary_scenes.summary(
        current=(
            summary_scenes.version(dataset_version_id=101),
            summary_scenes.version(dataset_version_id=102),
        )
    )

    frame = summary.current_frame()

    assert frame["dataset_version_id"].tolist() == [101, 102]
    assert frame["model"].tolist() == ["ACCESS-ESM1-6", "ACCESS-ESM1-6"]


def test_the_scenes_fill_in_every_facet_column(summary_scenes):
    # If a facet column is added and the scene builder is not, every test below reads
    # a blank where a facet should be and nothing says so.
    assert set(summary_scenes.version().facets) == set(DATASET_FACET_COLUMNS)
    assert "" not in summary_scenes.version().facets.values()
