"""
Tests of how a window of recorded changes is folded, and of what a summary says
"""

from __future__ import annotations

import datetime

import pytest

from esmporium.datasets import DATASET_FACET_COLUMNS
from esmporium.db import (
    DEFAULT_FACETS,
    ConflictingWindowError,
    MisalignedRowError,
    SearchRunInfo,
    UnknownFacetError,
    check_facets,
    collapse_changes,
    describe_moved,
    render_table,
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


# --------------------------------------------------------------------------- rendering
def test_render_caps_each_table(summary_scenes):
    changes = tuple(
        summary_scenes.change(
            ADDED, of=summary_scenes.version(variant_label=f"r{member}i1p1f1")
        )
        for member in range(1, 13)
    )

    rendered = summary_scenes.summary(changes=changes).render(max_rows=4)

    assert "ADDED (12)" in rendered
    assert "... (+8 more)" in rendered
    assert "r4i1p1f1" in rendered
    assert "r5i1p1f1" not in rendered


def test_render_does_not_cap_what_fits(summary_scenes):
    changes = (summary_scenes.change(ADDED), summary_scenes.change(REMOVED))

    rendered = summary_scenes.summary(changes=changes).render(max_rows=20)

    assert "more)" not in rendered


def test_render_leaves_no_trailing_whitespace(summary_scenes):
    # Our own pre-commit strips trailing whitespace, so a renderer which emits it
    # would make every checked-in regression file fail the moment it was committed.
    rendered = summary_scenes.summary(
        changes=(summary_scenes.change(ADDED),),
        current=(summary_scenes.version(),),
    ).render()

    assert rendered.endswith("\n")
    assert not rendered.endswith("\n\n")
    assert [line for line in rendered.splitlines() if line != line.rstrip()] == []


def test_render_says_no_changes_once(summary_scenes):
    rendered = summary_scenes.summary().render()

    assert "NO CHANGES" in rendered
    assert "ADDED (0)" not in rendered


def test_render_names_the_zero_sections_when_something_changed(summary_scenes):
    # A reader needs to know nothing was removed, not merely that removals were not
    # mentioned.
    rendered = summary_scenes.summary(changes=(summary_scenes.change(ADDED),)).render()

    assert "REMOVED (0)" in rendered
    assert "MODIFIED (0)" in rendered


def test_render_says_so_when_a_run_recorded_no_sub_searches(summary_scenes):
    # A run which died before writing its leaves has no health to report, and saying
    # "0/0 answered" would read as a failure rather than as an absence.
    rendered = summary_scenes.summary(health=summary_scenes.run(leaves=())).render()

    assert "no sub-searches recorded for this run" in rendered


def test_render_calls_out_truncation_the_whole_run_agrees_on(summary_scenes):
    rendered = summary_scenes.summary(
        health=summary_scenes.run(
            leaves=(
                summary_scenes.leaf("historical.tas", n_matches=209, n_documents=50),
                summary_scenes.leaf("control.tas", n_matches=100, n_documents=100),
            )
        )
    ).render()

    assert "150/309 documents collected -- TRUNCATED" in rendered
    assert "a removal here may not be real" in rendered


def test_render_calls_out_an_arrival_which_is_already_retracted(summary_scenes):
    rendered = summary_scenes.summary(
        changes=(
            summary_scenes.change(ADDED, of=summary_scenes.version(retracted=True)),
        )
    ).render()

    assert "1 of these arrived already retracted" in rendered


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


# ------------------------------------------------------------------- the columns shown
def test_default_facets_are_dataset_facets():
    # A default which named something no dataset records would be unrenderable.
    assert set(DEFAULT_FACETS) <= set(DATASET_FACET_COLUMNS)


def test_check_facets_defaults():
    assert check_facets(None) == DEFAULT_FACETS


def test_check_facets_can_narrow_and_widen():
    assert check_facets(("model",)) == ("model",)
    assert check_facets((*DEFAULT_FACETS, "grid_label")) == (
        "project",
        "model",
        "experiment",
        "variant_label",
        "variable",
        "reporting_interval",
        "grid_label",
    )


def test_check_facets_sorts_into_dataset_facet_order():
    # So that two tables of the same summary line up, whatever order they were asked
    # for in.
    assert check_facets(("variable", "model", "project")) == (
        "project",
        "model",
        "variable",
    )


def test_check_facets_refuses_a_facet_no_dataset_records():
    with pytest.raises(UnknownFacetError) as excinfo:
        # A real facet, but a document-only one: CORDEX's driving model is not a
        # Dataset column, so a summary cannot show it.
        check_facets(("model", "cordex-cmip6:driving_source_id"))

    assert excinfo.value.unknown == ("cordex-cmip6:driving_source_id",)
    message = str(excinfo.value)
    assert "Cannot show 'cordex-cmip6:driving_source_id'" in message
    assert "only a search document knows" in message


def test_check_facets_names_every_facet_it_cannot_show():
    # All of them at once, rather than one per attempt, so a caller fixing a list
    # does not have to run it once per mistake.
    with pytest.raises(UnknownFacetError) as excinfo:
        check_facets(("model", "nonsense", "also-nonsense"))

    assert excinfo.value.unknown == ("nonsense", "also-nonsense")


def test_the_id_column_tells_rows_apart_whatever_facets_are_shown(summary_scenes):
    # Two datasets can agree on every facet we model and still be different rows, so
    # narrowing the facets must never make them indistinguishable.
    same_facets = (
        summary_scenes.version(dataset_version_id=101),
        summary_scenes.version(dataset_version_id=102),
    )
    rendered = summary_scenes.summary(current=same_facets).render(facets=("model",))

    body = [line for line in rendered.splitlines() if "ACCESS-ESM1-6" in line]
    assert len(body) == 2
    assert body[0] != body[1]
    assert [line.split()[0] for line in body] == ["101", "102"]


def test_what_moved_goes_under_its_row_not_in_a_column(summary_scenes):
    # As a column it set the whole table's width on its own.
    rendered = summary_scenes.summary(
        changes=(summary_scenes.change(MODIFIED, detail=RETRACTED_MOVED),)
    ).render()

    lines = rendered.splitlines()
    header = next(line for line in lines if line.strip().startswith("id"))
    assert "what moved" not in header

    # On its own line, not trailing the row it belongs to
    moved = next(line for line in lines if "retracted: no -> yes" in line)
    assert moved.strip() == "retracted: no -> yes"

    # And the invariant that buys: however long the moved text is, it cannot widen
    # the table, which is what it did as a column.
    wordy = summary_scenes.summary(
        changes=(
            summary_scenes.change(
                MODIFIED,
                detail={
                    "data_nodes": {
                        "old": ["a.very.long.data.node.example.org"],
                        "new": ["a.second.very.long.data.node.example.org"],
                    }
                },
            ),
        )
    ).render()
    wordy_header = next(
        line for line in wordy.splitlines() if line.strip().startswith("id")
    )
    assert len(wordy_header) == len(header)


def test_only_the_rows_with_something_moved_get_a_continuation_line(summary_scenes):
    # collapse_changes never produces a modification with nothing moved, but a
    # summary built by hand can, and it should render as a plain row rather than as
    # a blank continuation line.
    summary = summary_scenes.summary(
        changes=(
            summary_scenes.change(
                MODIFIED,
                of=summary_scenes.version(dataset_version_id=101),
                detail=RETRACTED_MOVED,
            ),
            summary_scenes.change(
                MODIFIED, of=summary_scenes.version(dataset_version_id=102), detail=None
            ),
        )
    )

    lines = [line for line in summary.render().splitlines() if line.strip()]
    body = next(index for index, line in enumerate(lines) if " 101 " in f" {line} ")

    assert lines[body + 1].strip() == "retracted: no -> yes"
    # The row after that is the second version, with no continuation line of its own
    assert " 102 " in f" {lines[body + 2]} "


def test_render_table_refuses_a_row_which_does_not_match_its_headings():
    # The failure this guards against is silent: headings and cells built from two
    # separate lists can be ordered differently, putting a value under the wrong name.
    with pytest.raises(MisalignedRowError) as excinfo:
        render_table(["model", "version"], [["CESM3"], ["CESM3", "20261018", "extra"]])

    assert excinfo.value.n_headers == 2
    assert excinfo.value.row_lengths == (1, 3)
    message = str(excinfo.value)
    assert "A table with 2 headings was given 2 rows which do not match it" in message
    assert "of lengths [1, 3]" in message


def test_render_table_takes_a_table_which_does_match():
    assert render_table(["a"], [["1"]]) == "  a\n  1"


def test_the_scenes_fill_in_every_facet_column(summary_scenes):
    # If a facet column is added and the scene builder is not, the tables render it
    # as a gap and the regression files quietly lose a column.
    assert set(summary_scenes.version().facets) == set(DATASET_FACET_COLUMNS)
    assert "" not in summary_scenes.version().facets.values()
