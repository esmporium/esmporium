"""
Test of the function we actually care about: given a database, get me the summary

This is the shape of call a watched search makes on a server every morning, so it is
tested end to end against a real, migrated database rather than against hand-built
objects. One path through, the default one.

The rendering options and the folding of a window are covered by the unit tests in
`tests/unit/db/test_summaries.py` and the rendering regressions in
`tests/regression/test_summary_rendering.py`, which need no database, so the
expensive test here does not have to be run once per option.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from sqlmodel import Session

from esmporium.db import (
    DatasetChange,
    DatasetChangeType,
    NoRunsInWindowError,
    RequirementNotFoundError,
    SearchRunLeaf,
    record_changes,
    summarise_search,
)
from esmporium.db.migrate import upgrade_to_head
from esmporium.db.schema import Dataset, DatasetVersion, SearchRun, StoredRequirement
from esmporium.db.search_tracking import _upsert_run_version

if TYPE_CHECKING:
    from collections.abc import Callable

REQUIREMENT_NAME = "pattern-effect"


@dataclass(frozen=True)
class Preloaded:
    """The vocabulary the `preloaded` fixture hands a test"""

    new_run: Callable[[], int]
    """Open a run, and get its id"""

    saw: Callable[..., None]
    """Record that a run saw a named dataset, and what it was told about it"""

    leaf: Callable[..., None]
    """Record how one of a run's sub-searches went"""

    close: Callable[..., None]
    """Finish a run and log what it changed, as a real run does when it ends"""


@pytest.fixture
def preloaded(engine, get_dataset_kwargs):
    """
    Get a helper for pre-loading a database the way a tracked search would leave it

    Built with the same low-level helpers
    [search_and_track][esmporium.db.search_and_track] uses, so the rows are the rows a
    real run writes -- without needing the network to write them.

    The schema comes from the migrations rather than from `METADATA.create_all`, so
    this also checks that a summary can be read out of a database built the way a
    user's is.
    """
    upgrade_to_head(engine)

    with Session(engine) as session:
        stored = StoredRequirement(
            requirement_hash="h" * 64,
            name=REQUIREMENT_NAME,
            identity_json="{}",
        )
        session.add(stored)
        session.commit()
        requirement_id = stored.id

    versions: dict[tuple[str, str], int] = {}

    def version_id(model, experiment):
        """Get (making if needed) a real dataset version to point at"""
        key = (model, experiment)
        if key not in versions:
            with Session(engine) as session:
                dataset = Dataset(
                    **get_dataset_kwargs(
                        f"{model}_{experiment}",
                        model=model,
                        experiment=experiment,
                        variable="tas",
                        reporting_interval="mon",
                    )
                )
                session.add(dataset)
                session.commit()
                version = DatasetVersion(
                    dataset_id=dataset.id,
                    version="20261018",
                    is_latest=True,
                    retracted=False,
                )
                session.add(version)
                session.commit()
                versions[key] = version.id

        return versions[key]

    def new_run():
        with Session(engine) as session:
            run = SearchRun(stored_requirement_id=requirement_id)
            session.add(run)
            session.commit()

            return run.id

    def saw(run_id, model, experiment, *, retracted=False, data_nodes=("a.node",)):
        with Session(engine) as session:
            _upsert_run_version(
                session,
                run_id,
                version_id(model, experiment),
                retracted=retracted,
                is_latest=True,
                data_nodes=data_nodes,
            )
            session.commit()

    def leaf(run_id, role, *, answered=True, n_matches=2, n_documents=2):
        with Session(engine) as session:
            session.add(
                SearchRunLeaf(
                    search_run_id=run_id,
                    roles_json=f'["{role}"]',
                    project="CMIP7",
                    answered=answered,
                    host="search.east.esgf.io" if answered else None,
                    n_matches=n_matches,
                    n_documents=n_documents,
                    failure=None if answered else "every endpoint refused",
                )
            )
            session.commit()

    def close(run_id, *, complete=True):
        with Session(engine) as session:
            run = session.get(SearchRun, run_id)
            run.finish(complete=complete)
            session.add(run)
            session.flush()
            record_changes(session, run, stored_requirement_id=requirement_id)
            session.commit()

    return Preloaded(new_run=new_run, saw=saw, leaf=leaf, close=close)


@pytest.fixture
def two_runs(preloaded):
    """
    Pre-load two runs of one search, with something to say about each kind of change

    Between the first run and the second: CESM3 historical arrives, ACCESS-ESM1-6
    piControl is retracted, and UKESM2 historical drops out of the results.
    """
    first = preloaded.new_run()
    preloaded.saw(first, "ACCESS-ESM1-6", "historical")
    preloaded.saw(first, "ACCESS-ESM1-6", "piControl")
    preloaded.saw(first, "UKESM2", "historical")
    preloaded.leaf(first, "historical.tas")
    preloaded.close(first)

    second = preloaded.new_run()
    preloaded.saw(second, "ACCESS-ESM1-6", "historical")
    preloaded.saw(second, "ACCESS-ESM1-6", "piControl", retracted=True)
    preloaded.saw(second, "CESM3", "historical", data_nodes=("a.node", "b.node"))
    preloaded.leaf(second, "historical.tas", n_matches=3, n_documents=3)
    preloaded.close(second)

    return first, second


def test_summarise_a_preloaded_database(engine, two_runs):
    first, second = two_runs

    summary = summarise_search(engine, REQUIREMENT_NAME)

    assert summary.requirement_name == REQUIREMENT_NAME
    assert summary.window_runs == (second,)
    assert summary.baseline_run_id == first
    assert summary.run.run_id == second
    assert summary.run.complete

    assert summary.headline == f"{REQUIREMENT_NAME}: 1 added, 1 removed, 1 modified"
    assert summary.any_changes
    assert summary.net_unchanged == 0

    by_type = {change.change_type: change for change in summary.changes}
    assert by_type[DatasetChangeType.ADDED].version.facets["model"] == "CESM3"
    assert by_type[DatasetChangeType.REMOVED].version.facets["model"] == "UKESM2"

    modified = by_type[DatasetChangeType.MODIFIED]
    assert modified.version.facets["experiment"] == "piControl"
    assert modified.detail == {"retracted": {"old": False, "new": True}}


def test_the_removed_version_is_reported_as_the_last_run_which_saw_it_knew_it(
    engine, two_runs
):
    # The run which removed it has no answer about it -- that is what removed means --
    # so the only answer anyone ever wrote down is the baseline's.
    first, _ = two_runs

    summary = summarise_search(engine, REQUIREMENT_NAME)

    removed = next(
        change
        for change in summary.changes
        if change.change_type is DatasetChangeType.REMOVED
    )

    assert removed.version.seen_in_run_id == first
    assert removed.version.facets["model"] == "UKESM2"
    assert removed.version.version == "20261018"
    assert removed.version.data_nodes == ("a.node",)


def test_current_state_is_what_the_newest_run_returned(engine, two_runs):
    summary = summarise_search(engine, REQUIREMENT_NAME)

    assert [
        (version.facets["model"], version.facets["experiment"])
        for version in summary.current
    ] == [
        ("ACCESS-ESM1-6", "historical"),
        ("ACCESS-ESM1-6", "piControl"),
        ("CESM3", "historical"),
    ]
    # UKESM2 dropped out, so it is not in the current state even though it is in the
    # changes.
    assert "UKESM2" not in {version.facets["model"] for version in summary.current}


def test_the_frames_describe_the_same_thing_as_the_text(engine, two_runs):
    summary = summarise_search(engine, REQUIREMENT_NAME)

    changes = summary.changes_frame()
    current = summary.current_frame()

    assert len(changes) == len(summary.changes)
    assert set(changes["change"]) == {"added", "removed", "modified"}
    assert len(current) == len(summary.current)
    assert current.loc[current["model"] == "CESM3", "data_nodes"].tolist() == [
        "a.node, b.node"
    ]


def test_the_rendered_summary_names_what_changed(engine, two_runs):
    rendered = summarise_search(engine, REQUIREMENT_NAME).render()

    assert rendered.startswith(f"{REQUIREMENT_NAME}: 1 added, 1 removed, 1 modified")
    for expected in ("CESM3", "UKESM2", "retracted: no -> yes", "RUN HEALTH"):
        assert expected in rendered, expected


def test_a_window_covering_both_runs(engine, two_runs):
    first, second = two_runs

    summary = summarise_search(engine, REQUIREMENT_NAME, n_runs=2)

    assert summary.window_runs == (first, second)
    # The first run had nothing to compare against, so neither does the window.
    assert summary.baseline_run_id is None

    # Across the whole window, three datasets arrived: the two the first run found
    # (which it reported as added, there being nothing to compare with) and the one
    # the second run found. ACCESS-ESM1-6 piControl arrived and was then retracted,
    # which is still an arrival rather than an arrival plus a modification.
    assert summary.headline == f"{REQUIREMENT_NAME}: 3 added"
    assert {change.change_type for change in summary.changes} == {
        DatasetChangeType.ADDED
    }

    # UKESM2 was added by the first run and removed by the second, so over the window
    # as a whole it came and went: counted, not reported, because the net news about
    # it is that there is no news.
    assert summary.net_unchanged == 1
    assert "UKESM2" not in {
        change.version.facets["model"] for change in summary.changes
    }

    # The arrival which is already retracted is called out rather than being left to
    # a column nobody reads.
    assert [
        change.version.facets["experiment"]
        for change in summary.changes
        if change.added_but_retracted
    ] == ["piControl"]


def test_a_change_whose_history_has_gone_is_reported_rather_than_fatal(
    engine, two_runs
):
    # A change is written about a version a run saw, so its answer is normally there
    # to be read. If the history rows were ever pruned and the change log was not,
    # losing the whole summary over one unreadable row would be the wrong trade.
    _, second = two_runs

    with Session(engine) as session:
        session.add(
            DatasetChange(
                search_run_id=second,
                baseline_run_id=None,
                dataset_version_id=9999,
                change_type=DatasetChangeType.ADDED,
            )
        )
        session.commit()

    summary = summarise_search(engine, REQUIREMENT_NAME)

    orphan = next(
        change
        for change in summary.changes
        if change.version.dataset_version_id == 9999
    )
    assert orphan.change_type is DatasetChangeType.ADDED
    assert orphan.version.facets["model"] is None
    assert orphan.version.version == ""
    # And the rest of the summary is unharmed.
    assert summary.headline == f"{REQUIREMENT_NAME}: 2 added, 1 removed, 1 modified"


def test_a_search_which_has_never_run(engine, preloaded):
    with pytest.raises(NoRunsInWindowError, match="has never been run"):
        summarise_search(engine, REQUIREMENT_NAME)


def test_a_window_which_selects_nothing(engine, two_runs):
    with pytest.raises(NoRunsInWindowError, match="falls in the window asked for"):
        summarise_search(engine, REQUIREMENT_NAME, since=10_000)


def test_a_search_we_do_not_have(engine, preloaded):
    with pytest.raises(RequirementNotFoundError):
        summarise_search(engine, "not-a-search")
