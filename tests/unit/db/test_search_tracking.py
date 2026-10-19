"""
Tests of recording search runs and of comparing one with the last

Two halves, and the first is the one that matters most: the comparison is read off the
runs' own records of what they were told, so it can be driven directly, without any
searching, and every case a daily watch would hit can be written down cheaply.

The second half drives the whole thing through `search_and_track` against a mock ESGF,
to check that the recording really does happen where the searching happens.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
import pytest
from sqlmodel import Session, create_engine, select
from tenacity import Retrying, retry_if_exception, stop_after_attempt

from esmporium.db import (
    METADATA,
    DatasetChangeType,
    RequirementNotFoundError,
    SearchRunLeaf,
    SearchRunVersion,
    UnconfiguredSQLiteEngineError,
    compute_changes,
    get_changes,
    get_search_runs,
    record_changes,
    rerun_search_esgf,
    run_is_complete,
    search_and_track,
)
from esmporium.db.schema import Dataset, DatasetVersion, SearchRun, StoredRequirement
from esmporium.db.search_tracking import _upsert_run_version
from esmporium.query import Query
from esmporium.requirements import all_of, leaf, requirement
from esmporium.search import (
    ESGF1_CMIP5_FACADE_PARAMETERS,
    ESGF1_CMIP6_FACADE_PARAMETERS,
    AllSubQueriesFailedError,
    SearchAPIESGF1Solr,
    SearchAPIFacade,
    SolrSingleRowResultParser,
    SolrVariableBundleResultParser,
    build_list_selector,
    build_project_list_selector,
)
from esmporium.search.retry import _is_transient

if TYPE_CHECKING:
    from collections.abc import Callable

    from esmporium.db import RunChanges


def a_requirement(name="gregory-method", projects="CMIP6", variable="tas"):
    """A requirement over one leaf, which is all a tracking test needs"""
    return requirement(
        name=name,
        tree=all_of(leaf(Query(variable=variable, project=projects), "field")),
        group_by=("model",),
    )


# -- the comparison, driven directly ------------------------------------------------


@dataclass(frozen=True)
class Tracked:
    """The vocabulary the `tracked` fixture hands a test"""

    new_run: Callable[[], int]
    """Open a run, and get its id"""

    saw: Callable[..., None]
    """Record that a run saw a labelled version, and what it was told about it"""

    close: Callable[..., RunChanges]
    """Finish a run, compare it with the last complete one, and log the result"""

    version_id: Callable[[str], int]
    """Get the version id a label stands for"""


@pytest.fixture
def tracked(engine, get_dataset_kwargs):
    """
    Get a helper for building runs by hand

    `new_run` opens a run, `saw` says what it found, `close` finishes it and compares
    it with the last complete one. That is the whole vocabulary the comparison works
    in, so the cases below read as what happened rather than as rows. `version_id`
    gives the id a label was given, for asserting on.
    """
    with Session(engine) as session:
        stored = StoredRequirement(
            requirement_hash="h" * 64, name="gregory-method", identity_json="{}"
        )
        session.add(stored)
        session.commit()
        requirement_id = stored.id

    versions: dict[str, int] = {}

    def version_id(label):
        """Get (making if needed) a real dataset version to point at"""
        if label not in versions:
            with Session(engine) as session:
                dataset = Dataset(**get_dataset_kwargs(label))
                session.add(dataset)
                session.commit()
                version = DatasetVersion(
                    dataset_id=dataset.id,
                    version="20200101",
                    is_latest=True,
                    retracted=False,
                )
                session.add(version)
                session.commit()
                versions[label] = version.id

        return versions[label]

    def new_run():
        with Session(engine) as session:
            run = SearchRun(stored_requirement_id=requirement_id)
            session.add(run)
            session.commit()

            return run.id

    def saw(run_id, label, *, retracted=False, is_latest=True, data_nodes=("a.node",)):
        with Session(engine) as session:
            _upsert_run_version(
                session,
                run_id,
                version_id(label),
                retracted=retracted,
                is_latest=is_latest,
                data_nodes=data_nodes,
            )
            session.commit()

    def close(run_id, *, complete=True):
        """Close the run, compare it with the last complete one, and log the result"""
        with Session(engine) as session:
            run = session.get(SearchRun, run_id)
            run.finish(complete=complete)
            session.add(run)
            session.flush()
            changes = record_changes(session, run, stored_requirement_id=requirement_id)
            session.commit()

            return changes

    return Tracked(new_run=new_run, saw=saw, close=close, version_id=version_id)


def test_the_first_run_reports_everything_it_found_as_added(tracked):
    # There is nothing to compare with, so "added" reads as "first seen in this
    # series"
    run = tracked.new_run()
    tracked.saw(run, "a")
    tracked.saw(run, "b")
    changes = tracked.close(run)

    assert changes.baseline_run_id is None
    assert changes.added == tuple(
        sorted([tracked.version_id("a"), tracked.version_id("b")])
    )
    assert changes.removed == ()
    assert changes.modified == ()


def test_nothing_changing_reports_nothing(tracked):
    first = tracked.new_run()
    tracked.saw(first, "a")
    tracked.close(first)

    second = tracked.new_run()
    tracked.saw(second, "a")
    changes = tracked.close(second)

    assert changes.baseline_run_id == first
    assert not changes.any_changes


def test_a_version_becoming_retracted_is_a_change(tracked, engine):
    # The case the whole step exists for. Note that the version row itself has already
    # been overwritten by the second run, so this can only be read off the run records.
    first = tracked.new_run()
    tracked.saw(first, "a", retracted=False)
    tracked.close(first)

    second = tracked.new_run()
    tracked.saw(second, "a", retracted=True)
    changes = tracked.close(second)

    assert changes.modified == (tracked.version_id("a"),)

    (change,) = get_changes(engine, second)

    assert change.change_type == DatasetChangeType.MODIFIED
    assert change.detail == {"retracted": {"old": False, "new": True}}
    assert change.baseline_run_id == first


def test_a_version_no_longer_being_latest_is_a_change(tracked, engine):
    first = tracked.new_run()
    tracked.saw(first, "a", is_latest=True)
    tracked.close(first)

    second = tracked.new_run()
    tracked.saw(second, "a", is_latest=False)
    tracked.close(second)

    (change,) = get_changes(engine, second)

    assert change.detail == {"is_latest": {"old": True, "new": False}}


def test_a_version_gaining_a_data_node_is_a_change(tracked, engine):
    first = tracked.new_run()
    tracked.saw(first, "a", data_nodes=("a.node",))
    tracked.close(first)

    second = tracked.new_run()
    tracked.saw(second, "a", data_nodes=("a.node", "b.node"))
    tracked.close(second)

    (change,) = get_changes(engine, second)

    assert change.detail == {
        "data_nodes": {"old": ["a.node"], "new": ["a.node", "b.node"]}
    }


def test_several_changes_to_one_version_are_one_record(tracked, engine):
    first = tracked.new_run()
    tracked.saw(first, "a", retracted=False, is_latest=True)
    tracked.close(first)

    second = tracked.new_run()
    tracked.saw(second, "a", retracted=True, is_latest=False)
    tracked.close(second)

    (change,) = get_changes(engine, second)

    assert change.detail == {
        "retracted": {"old": False, "new": True},
        "is_latest": {"old": True, "new": False},
    }


def test_a_version_appearing_and_disappearing(tracked, engine):
    first = tracked.new_run()
    tracked.saw(first, "a")
    tracked.close(first)

    second = tracked.new_run()
    tracked.saw(second, "a")
    tracked.saw(second, "b")
    assert tracked.close(second).added == (tracked.version_id("b"),)

    third = tracked.new_run()
    tracked.saw(third, "a")
    assert tracked.close(third).removed == (tracked.version_id("b"),)

    assert [change.change_type for change in get_changes(engine, third)] == [
        DatasetChangeType.REMOVED
    ]


def test_an_incomplete_run_records_no_changes(tracked, engine):
    # The false-alarm case: a run which half happened saw less, which is
    # indistinguishable from datasets having gone away. Calling that a change would be
    # inventing one.
    first = tracked.new_run()
    tracked.saw(first, "a")
    tracked.saw(first, "b")
    tracked.close(first)

    second = tracked.new_run()
    tracked.saw(second, "a")
    changes = tracked.close(second, complete=False)

    assert not changes.any_changes
    assert changes.baseline_run_id is None
    assert get_changes(engine, second) == ()


def test_an_incomplete_run_is_not_used_as_a_baseline(tracked):
    # And the other half of it: the next good run has to compare with the last run
    # that answered in full, or the datasets the bad run missed come back as additions.
    first = tracked.new_run()
    tracked.saw(first, "a")
    tracked.saw(first, "b")
    tracked.close(first)

    second = tracked.new_run()
    tracked.saw(second, "a")
    tracked.close(second, complete=False)

    third = tracked.new_run()
    tracked.saw(third, "a")
    tracked.saw(third, "b")
    changes = tracked.close(third)

    assert changes.baseline_run_id == first
    assert not changes.any_changes


def test_an_incomplete_run_still_records_what_it_saw(tracked, engine):
    # Worth keeping even though nothing is computed from it: it is what says what the
    # run managed before it fell over.
    run = tracked.new_run()
    tracked.saw(run, "a")
    tracked.close(run, complete=False)

    with Session(engine) as session:
        seen = session.exec(
            select(SearchRunVersion).where(SearchRunVersion.search_run_id == run)
        ).all()

    assert [row.dataset_version_id for row in seen] == [tracked.version_id("a")]


def test_comparing_does_not_write(tracked, engine):
    # `compute_changes` answers the question; `record_changes` is what logs it.
    first = tracked.new_run()
    tracked.saw(first, "a")
    tracked.close(first)

    second = tracked.new_run()
    tracked.saw(second, "a", retracted=True)

    with Session(engine) as session:
        run = session.get(SearchRun, second)
        changes = compute_changes(session, run, stored_requirement_id=1)

    assert changes.modified
    assert get_changes(engine, second) == ()


# -- what one run was told, folded together -----------------------------------------


def test_two_documents_disagreeing_about_retraction_take_the_last_word(tracked, engine):
    # Replicas lag on retraction, so two documents in one run can disagree. Every
    # document asserts a value, so there is no "first" to prefer: last write wins, the
    # same way the version row itself is refreshed.
    run = tracked.new_run()
    tracked.saw(run, "a", retracted=False)
    tracked.saw(run, "a", retracted=True)

    with Session(engine) as session:
        (row,) = session.exec(
            select(SearchRunVersion).where(SearchRunVersion.search_run_id == run)
        ).all()

    assert row.retracted is True


def test_two_documents_naming_different_nodes_are_unioned(tracked, engine):
    # Unlike the booleans: a document reports only the nodes it knows about, so the
    # last one to arrive is not the most complete and overwriting would lose nodes.
    run = tracked.new_run()
    tracked.saw(run, "a", data_nodes=("a.node",))
    tracked.saw(run, "a", data_nodes=("b.node",))

    with Session(engine) as session:
        (row,) = session.exec(
            select(SearchRunVersion).where(SearchRunVersion.search_run_id == run)
        ).all()

    assert row.data_nodes_json == '["a.node", "b.node"]'


# -- completeness --------------------------------------------------------------------


def a_leaf(*, answered=True, n_matches=2, n_documents=2):
    return SearchRunLeaf(
        search_run_id=1,
        roles_json='["field"]',
        project="CMIP6",
        answered=answered,
        n_matches=n_matches,
        n_documents=n_documents,
    )


def test_a_run_whose_sub_searches_all_answered_is_complete():
    assert run_is_complete([a_leaf(), a_leaf()])


def test_a_run_with_a_failed_sub_search_is_incomplete():
    assert not run_is_complete([a_leaf(), a_leaf(answered=False)])


def test_a_truncated_sub_search_makes_a_run_incomplete():
    # Cut short by `limit` or `max_results`: fewer documents than the host said
    # matched, which is a partial answer wearing a complete one's clothes.
    assert not run_is_complete([a_leaf(n_matches=5, n_documents=2)])


def test_a_host_which_reported_no_total_is_taken_at_its_word():
    # Nothing to compare against, and refusing to ever call such a run complete would
    # make tracking impossible against the hosts which do not count.
    assert run_is_complete([a_leaf(n_matches=None, n_documents=2)])


def test_a_sub_search_which_matched_nothing_is_still_an_answer():
    # "This project has nothing for you" is a real answer, and not the same thing as
    # "this project could not be reached".
    assert run_is_complete([a_leaf(n_matches=0, n_documents=0)])


# -- the whole thing, against a mock ESGF --------------------------------------------


def solr_doc(doc_id, *, retracted=False, data_node="a.node"):
    """A minimal CMIP6 Solr record, which parses into one dataset row"""
    return {
        "master_id": [f"CMIP6.{doc_id}"],
        "id": [doc_id],
        "project": ["CMIP6"],
        "source_id": ["ACCESS"],
        "institution_id": ["CSIRO"],
        "experiment_id": ["historical"],
        "variant_label": ["r1i1p1f1"],
        "variable_id": ["tas"],
        "frequency": ["mon"],
        "table_id": ["Amon"],
        "grid_label": ["gn"],
        "version": ["20200101"],
        "latest": [True],
        "retracted": [retracted],
        "data_node": [data_node],
    }


def cmip5_solr_doc(doc_id, *, variables=("tas", "pr"), data_node="a.node"):
    """
    A minimal CMIP5 Solr record, which parses into one dataset row *per variable*

    CMIP5 is the one project whose documents bundle several datasets together, so this
    is the shape which makes one document become several
    [SearchRunVersion][esmporium.db.schema.SearchRunVersion] rows. Its facets carry
    CMIP5's own names (`institute`, `ensemble`, `time_frequency`, `cmor_table`), and
    there is no `grid_label`, because CMIP5 has no grid concept.
    """
    return {
        "master_id": [f"cmip5.{doc_id}"],
        "id": [doc_id],
        "project": ["CMIP5"],
        "model": ["ACCESS1-0"],
        "institute": ["CSIRO-BOM"],
        "experiment": ["historical"],
        "ensemble": ["r1i1p1"],
        "variable": list(variables),
        "time_frequency": ["mon"],
        "cmor_table": ["Amon"],
        "version": ["20200101"],
        "latest": [True],
        "retracted": [False],
        "data_node": [data_node],
    }


def facade(host, parameters=ESGF1_CMIP6_FACADE_PARAMETERS, result_parser=None):
    return SearchAPIFacade(
        parameters=parameters,
        search_api=SearchAPIESGF1Solr(
            host,
            Retrying(
                stop=stop_after_attempt(1),
                retry=retry_if_exception(_is_transient),
                reraise=True,
            ),
        ),
        result_parser=(
            SolrSingleRowResultParser() if result_parser is None else result_parser
        ),
    )


def cmip5_facade(host):
    """Build a CMIP5 facade, which is the bundling one -- see [cmip5_solr_doc][(m).]"""
    return facade(
        host,
        parameters=ESGF1_CMIP5_FACADE_PARAMETERS,
        result_parser=SolrVariableBundleResultParser(),
    )


def serving(docs, *, num_found=None):
    """A client which answers every search with `docs`"""

    def handler(request):
        return httpx.Response(
            200,
            json={
                "response": {
                    "numFound": len(docs) if num_found is None else num_found,
                    "start": 0,
                    "docs": docs,
                }
            },
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def track(engine, requirement_to_search, docs, **kwargs):
    with serving(docs) as client:
        return search_and_track(
            engine,
            requirement_to_search,
            build_list_selector([facade("a.example.io")]),
            client=client,
            warn_on_pagination=False,
            **kwargs,
        )


def rerun(engine, name_or_id, docs, **kwargs):
    with serving(docs) as client:
        return rerun_search_esgf(
            engine,
            name_or_id,
            build_list_selector([facade("a.example.io")]),
            client=client,
            warn_on_pagination=False,
            **kwargs,
        )


def test_a_tracked_search_records_the_requirement_and_the_run(engine):
    result = track(engine, a_requirement(), [solr_doc("a")])

    runs = get_search_runs(engine, "gregory-method")

    assert [run.id for run in runs] == [result.search_run_id]
    assert runs[0].complete
    assert runs[0].finished_at is not None


def test_a_tracked_search_records_one_row_per_sub_search(engine):
    # A requirement fans out, so a run is not one request. This is what says which
    # part of the analysis came back with what.
    def answer(request):
        docs = (
            [cmip5_solr_doc("a")]
            if "cmip5" in str(request.url.host)
            else [solr_doc("a")]
        )

        return httpx.Response(
            200, json={"response": {"numFound": len(docs), "start": 0, "docs": docs}}
        )

    with httpx.Client(transport=httpx.MockTransport(answer)) as client:
        result = search_and_track(
            engine,
            a_requirement(projects=("CMIP5", "CMIP6")),
            build_project_list_selector(
                {
                    "CMIP5": [cmip5_facade("cmip5.example.io")],
                    "CMIP6": [facade("cmip6.example.io")],
                }
            ),
            client=client,
            warn_on_pagination=False,
        )

    with Session(engine) as session:
        leaves = session.exec(
            select(SearchRunLeaf).where(
                SearchRunLeaf.search_run_id == result.search_run_id
            )
        ).all()

    assert sorted(leaf.project for leaf in leaves) == ["CMIP5", "CMIP6"]
    assert all(leaf.roles_json == '["field"]' for leaf in leaves)
    assert all(leaf.host is not None for leaf in leaves)
    # One document each. The CMIP5 one counts as one document even though it described
    # two datasets, which is what keeps `n_documents` comparable with the host's own
    # `n_matches`.
    assert all(leaf.n_documents == 1 for leaf in leaves)


def test_one_bundling_document_is_recorded_as_several_versions(engine):
    # CMIP5's documents describe several datasets at once, so what a run saw is not one
    # row per document. Nothing else here exercises that fan-out.
    with serving([cmip5_solr_doc("a", variables=("tas", "pr"))]) as client:
        result = search_and_track(
            engine,
            a_requirement(projects="CMIP5"),
            build_list_selector([cmip5_facade("cmip5.example.io")]),
            client=client,
            warn_on_pagination=False,
        )

    with Session(engine) as session:
        seen = session.exec(
            select(SearchRunVersion).where(
                SearchRunVersion.search_run_id == result.search_run_id
            )
        ).all()

    assert len(seen) == 2
    assert len(result.changes.added) == 2


def test_asking_for_the_runs_of_an_unknown_search_says_so(engine):
    # `get_search_runs` has its own copy of this refusal, so it needs its own test:
    # the one in `test_requirement_tracking.py` covers `load_requirement`'s.
    with pytest.raises(RequirementNotFoundError) as excinfo:
        get_search_runs(engine, "never-stored")

    assert str(excinfo.value) == (
        "No requirement is stored with name 'never-stored'. "
        "Store one by searching for it with esmporium.db.search_and_track."
    )


def test_an_unconfigured_engine_is_refused_before_anything_is_searched_for(tmp_path):
    # `search_and_track` promises this in its docstring, and the promise is about
    # *ordering*: a search contacts ESGF before it writes anything, so an engine which
    # cannot save has to be refused up front rather than after the network has been
    # used. The client is the assertion -- it fails the test if it is ever reached.
    def never_asked(request):
        pytest.fail(f"searched {request.url} before checking the engine")

    engine = create_engine(f"sqlite:///{tmp_path / 'unconfigured.db'}")
    METADATA.create_all(engine)

    with httpx.Client(transport=httpx.MockTransport(never_asked)) as client:
        with pytest.raises(UnconfiguredSQLiteEngineError):
            search_and_track(
                engine,
                a_requirement(),
                build_list_selector([facade("a.example.io")]),
                client=client,
                warn_on_pagination=False,
            )


def test_a_tracked_search_saves_the_results_as_well(engine):
    # Tracking is in addition to saving, not instead of it.
    track(engine, a_requirement(), [solr_doc("a"), solr_doc("b")])

    with Session(engine) as session:
        assert len(session.exec(select(DatasetVersion)).all()) == 2


def test_re_running_by_name_finds_the_same_requirement(engine):
    first = track(engine, a_requirement(), [solr_doc("a")])

    second = rerun(engine, "gregory-method", [solr_doc("a")])

    assert second.stored_requirement_id == first.stored_requirement_id
    assert second.search_run_id != first.search_run_id
    assert second.requirement == first.requirement


def test_re_running_by_id_finds_the_same_requirement(engine):
    first = track(engine, a_requirement(), [solr_doc("a")])

    second = rerun(engine, first.stored_requirement_id, [solr_doc("a")])

    assert second.stored_requirement_id == first.stored_requirement_id


def test_re_running_sees_a_retraction(engine):
    # The end-to-end version of the case the step is for.
    track(engine, a_requirement(), [solr_doc("a", retracted=False)])

    second = rerun(engine, "gregory-method", [solr_doc("a", retracted=True)])

    (change,) = get_changes(engine, second.search_run_id)

    assert change.change_type == DatasetChangeType.MODIFIED
    assert change.detail == {"retracted": {"old": False, "new": True}}


def test_re_running_sees_a_new_dataset(engine):
    track(engine, a_requirement(), [solr_doc("a")])

    second = rerun(engine, "gregory-method", [solr_doc("a"), solr_doc("b")])

    assert len(second.changes.added) == 1
    assert second.changes.modified == ()


def test_searching_again_with_the_same_requirement_tracks_it_the_same_way(engine):
    # `rerun_search_esgf` is a convenience, not a second code path, so this has to
    # behave identically.
    track(engine, a_requirement(), [solr_doc("a", retracted=False)])

    second = track(engine, a_requirement(), [solr_doc("a", retracted=True)])

    (change,) = get_changes(engine, second.search_run_id)

    assert change.detail == {"retracted": {"old": False, "new": True}}


def test_a_truncated_run_records_no_changes(engine):
    track(engine, a_requirement(), [solr_doc("a"), solr_doc("b")])

    with serving([solr_doc("a")], num_found=5) as client:
        second = rerun_search_esgf(
            engine,
            "gregory-method",
            build_list_selector([facade("a.example.io")]),
            client=client,
            warn_on_pagination=False,
            max_results=1,
        )

    assert not second.complete
    assert get_changes(engine, second.search_run_id) == ()


def test_a_run_whose_search_raised_is_left_incomplete(engine):
    # So that it cannot become a baseline. The run is still recorded: that it was
    # attempted and failed is worth knowing.
    track(engine, a_requirement(), [solr_doc("a")])

    def refuse(request):
        return httpx.Response(500)

    with httpx.Client(transport=httpx.MockTransport(refuse)) as client:
        with pytest.raises(
            AllSubQueriesFailedError,
            match=re.escape("sub-queries failed to return anything we could use"),
        ):
            rerun_search_esgf(
                engine,
                "gregory-method",
                build_list_selector([facade("a.example.io")]),
                client=client,
                warn_on_pagination=False,
            )

    runs = get_search_runs(engine, "gregory-method")

    assert len(runs) == 2
    assert not runs[0].complete
    assert runs[0].finished_at is not None
