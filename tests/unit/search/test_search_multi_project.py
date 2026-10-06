"""
Test the high-level `search` against a mock API and an in-memory database

These cover the behaviours `search` adds on top of `search_single_project`: turning a
requirement into one search per leaf, splitting each of those into one search per
project, searching twice-asked-for datasets once, refusing a leaf with no project,
reporting which role and project each result came from, and — the important one —
handing each sub-search's results to a fresh processor as they arrive, so with the
database-saving processor a kill part way through still leaves the finished
sub-searches in the database.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING

import httpx
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select
from tenacity import Retrying, retry_if_exception, stop_after_attempt

from esmporium.db import (
    METADATA,
    Dataset,
    build_result_processor_factory,
    configure_sqlite_for_concurrency,
)
from esmporium.query import NoTargetProjectError, Query
from esmporium.requirements import all_of, leaf, requirement
from esmporium.search import (
    ESGF1_CMIP6_FACADE_PARAMETERS,
    AllSubQueriesFailedError,
    NoFacadeAnsweredError,
    SearchAPIESGF1Solr,
    SearchAPIFacade,
    SolrSingleRowResultParser,
    build_list_selector,
    search,
)
from esmporium.search.retry import _is_transient

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine


class ProcessKilled(Exception):
    """Stand-in for the process being killed part way through a run."""


@pytest.fixture
def engine() -> Iterator[Engine]:
    """
    Get an in-memory SQLite engine with our schema created

    `StaticPool` keeps every connection pointed at the one in-memory database, so the
    fresh session the processor factory opens per sub-query and the session we read back
    with all see the same tables.
    """
    engine = configure_sqlite_for_concurrency(
        create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    )
    METADATA.create_all(engine)
    yield engine
    engine.dispose()


def fast_retrying(attempts: int = 1) -> Retrying:
    """Build a retry policy without backoff sleeps"""
    return Retrying(
        stop=stop_after_attempt(attempts),
        retry=retry_if_exception(_is_transient),
        reraise=True,
    )


def client_for(handler) -> httpx.Client:
    """Build an httpx client whose requests are answered by `handler`"""
    return httpx.Client(transport=httpx.MockTransport(handler))


def never_asked(request):
    """A handler for the tests in which nothing should be sent anywhere."""
    pytest.fail(f"unexpected request to {request.url}")


def make_facade(host: str = "host") -> SearchAPIFacade:
    """A CMIP6-ESGF1 (Solr) facade the mock transport answers for"""
    return SearchAPIFacade(
        parameters=ESGF1_CMIP6_FACADE_PARAMETERS,
        search_api=SearchAPIESGF1Solr(host, fast_retrying(1)),
        result_parser=SolrSingleRowResultParser(),
    )


def solr_doc(doc_id: str) -> dict:
    """A minimal CMIP6 Solr record that parses into one dataset row"""
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
        "retracted": [False],
        "data_node": ["node.example"],
    }


def solr_body(docs: list[dict]) -> httpx.Response:
    """A 200 Solr response carrying `docs` (a single, full page)"""
    return httpx.Response(
        200, json={"response": {"numFound": len(docs), "start": 0, "docs": docs}}
    )


def saved_master_ids(engine: Engine) -> set[str]:
    """Read the `id_project_specific` of every dataset saved in `engine`"""
    with Session(engine) as session:
        return {row.id_project_specific for row in session.exec(select(Dataset)).all()}


def a_requirement(*leaves, where=None):
    """
    Build a requirement from leaves, defaulting what `search` does not look at

    `search` reads the tree and `where` and nothing else, so `group_by` and the rest
    are here only because a requirement needs them.
    """
    tree = leaves[0] if len(leaves) == 1 else all_of(*leaves)
    return requirement(name="an-analysis", tree=tree, group_by=("model",), where=where)


def for_variables(*variables, projects=("CMIP6",)):
    """
    Build a requirement with one leaf per variable, each role named for its variable
    """
    return a_requirement(
        *(
            leaf(Query(variable=variable, reporting_interval="mon"), variable)
            for variable in variables
        ),
        where=Query(project=projects),
    )


def test_finished_leaf_searches_survive_a_kill_mid_run(engine):
    """
    If the process is killed part way through, finished sub-searches are still saved

    This is the point of saving inside `search_single_project` (which commits each page
    as it arrives) rather than collecting everything and saving at the end: the first
    leaf here commits before the second is even started, so when the second blows up
    with an unhandled error, the first leaf's dataset is already in the database.
    """
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return solr_body([solr_doc("survivor")])
        # The second sub-search "kills" the process with an error nobody catches.
        raise ProcessKilled

    with pytest.raises(ProcessKilled):
        search(
            for_variables("tas", "pr"),
            processor_factory=build_result_processor_factory(engine),
            selector=build_list_selector([make_facade()]),
            client=client_for(handler),
        )

    # The first sub-search's result is committed even though the run then died.
    assert saved_master_ids(engine) == {"CMIP6.survivor"}


def test_runs_every_leaf_search_and_saves_all_results(engine):
    """Every leaf runs and saves; one outcome comes back per sub-search"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        # A distinct document per request, so both leaves leave their own row.
        doc = solr_doc(f"d{calls}")
        calls += 1
        return solr_body([doc])

    outcomes = search(
        for_variables("tas", "pr"),
        processor_factory=build_result_processor_factory(engine),
        selector=build_list_selector([make_facade()]),
        client=client_for(handler),
    )

    assert len(outcomes) == 2
    assert saved_master_ids(engine) == {"CMIP6.d0", "CMIP6.d1"}


def test_splits_a_multi_project_leaf_into_one_search_per_project(engine):
    """A leaf naming two projects becomes two searches, one per project"""
    seen_projects: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_projects.append(request.url.params.get_list("project"))
        return solr_body([solr_doc("only")])

    outcomes = search(
        for_variables("tas", projects=("CMIP5", "CMIP6")),
        processor_factory=build_result_processor_factory(engine),
        selector=build_list_selector([make_facade()]),
        client=client_for(handler),
    )

    # One search (and so one outcome) per project, each carrying its own project facet.
    assert len(outcomes) == 2
    assert sorted(seen_projects) == [["CMIP5"], ["CMIP6"]]


def test_two_leaves_asking_the_same_thing_are_searched_once(engine):
    """
    Two roles wanting the same dataset are one search, and one outcome naming both

    Writing the same dataset twice is a real thing to do -- pattern scaling wants the
    same `tas` as both the field and the reference -- and asking the endpoint for it
    twice would be pure waste.
    """
    requests_made = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests_made
        requests_made += 1
        return solr_body([solr_doc("shared")])

    same = Query(project=("CMIP6",), variable="tas", reporting_interval="mon")
    outcomes = search(
        a_requirement(leaf(same, "field"), leaf(same, "reference")),
        processor_factory=build_result_processor_factory(engine),
        selector=build_list_selector([make_facade()]),
        client=client_for(handler),
    )

    assert requests_made == 1
    assert len(outcomes) == 1
    assert outcomes[0].roles == ("field", "reference")


def test_outcomes_carry_the_role_and_project_which_produced_them():
    """
    A result says which leaf asked for it and which project answered

    A requirement fans out into many searches, so an outcome on its own does not say
    which of them it is.
    """
    outcomes = search(
        for_variables("tas", "pr", projects=("CMIP5", "CMIP6")),
        build_list_selector([make_facade()]),
        client=client_for(lambda r: solr_body([solr_doc("d")])),
    )

    assert {(outcome.roles, outcome.project) for outcome in outcomes} == {
        (("tas",), "CMIP5"),
        (("tas",), "CMIP6"),
        (("pr",), "CMIP5"),
        (("pr",), "CMIP6"),
    }
    # The query that was actually sent is there too, in the project's own style.
    for outcome in outcomes:
        assert outcome.query.project == (outcome.project,)


def test_outcomes_come_back_in_tree_then_project_order():
    """
    The documented order: leaves as the tree is written, then projects as named

    Pinned because a caller lining results up against anything else -- a report, a
    previous run -- needs the order to be a promise rather than an accident.
    """
    outcomes = search(
        for_variables("tas", "pr", projects=("CMIP5", "CMIP6")),
        build_list_selector([make_facade()]),
        client=client_for(lambda r: solr_body([solr_doc("d")])),
    )

    assert [(outcome.roles[0], outcome.project) for outcome in outcomes] == [
        ("tas", "CMIP5"),
        ("tas", "CMIP6"),
        ("pr", "CMIP5"),
        ("pr", "CMIP6"),
    ]


def test_the_order_is_the_same_run_in_parallel():
    """`max_workers` changes when the searches run, never the order they come back"""
    kwargs = {
        "selector": build_list_selector([make_facade()]),
        "client": client_for(lambda r: solr_body([solr_doc("d")])),
    }
    req = for_variables("tas", "pr", projects=("CMIP5", "CMIP6"))

    sequential = search(req, **kwargs)
    parallel = search(req, max_workers=4, **kwargs)

    def shape(outcomes):
        return [(outcome.roles, outcome.project) for outcome in outcomes]

    assert shape(sequential) == shape(parallel)


def test_a_leaf_with_no_project_is_refused_before_any_work(engine):
    """
    A leaf that names no project blows up before any endpoint or save happens

    The message has to name the leaf: a requirement can have a dozen of them, and
    "something names no project" is no help in finding which.
    """
    with pytest.raises(NoTargetProjectError, match=r"'pr'.*'an-analysis'"):
        search(
            a_requirement(
                leaf(Query(project=("CMIP6",), variable="tas"), "tas"),
                leaf(Query(variable="pr"), "pr"),
            ),
            processor_factory=build_result_processor_factory(engine),
            selector=build_list_selector([make_facade()]),
            client=client_for(never_asked),
        )

    # Nothing was searched, so nothing was saved -- not even the leaf which was fine.
    assert saved_master_ids(engine) == set()


def test_parallelises_over_the_leaf_searches():
    """
    With `max_workers > 1` the sub-searches are genuinely in flight at the same time

    Both requests must reach the handler together to pass the barrier. A sequential run
    would leave the second unsent while the first blocks, so the barrier would time out
    and the test would fail rather than hang.
    """
    barrier = threading.Barrier(2, timeout=5)

    def handler(request: httpx.Request) -> httpx.Response:
        barrier.wait()
        return solr_body([solr_doc("d")])

    outcomes = search(
        for_variables("tas", projects=("CMIP5", "CMIP6")),
        build_list_selector([make_facade()]),
        client=client_for(handler),
        max_workers=2,
    )

    assert len(outcomes) == 2


def test_a_failing_worker_does_not_stop_the_others_being_processed():
    """
    One worker blowing up does not stop the others' results reaching their processor
    """
    processed = []
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        (project,) = request.url.params.get_list("project")
        if project == "CMIP6":
            # This worker "dies" before returning anything.
            raise ProcessKilled
        return solr_body([solr_doc("survivor")])

    @contextmanager
    def recording_factory():
        def processor(facade, parsed):
            with lock:
                processed.append(parsed)

        yield processor

    with pytest.raises(ProcessKilled):
        search(
            for_variables("tas", projects=("CMIP5", "CMIP6")),
            build_list_selector([make_facade()]),
            client=client_for(handler),
            processor_factory=recording_factory,
            max_workers=2,
        )

    # The surviving (CMIP5) worker's results were still handed to its processor.
    assert len(processed) == 1


def test_parallel_writes_land_in_a_shared_sqlite_db(tmp_path):
    """
    Parallel workers writing distinct datasets to one SQLite database all get saved
    """
    engine = configure_sqlite_for_concurrency(
        create_engine(f"sqlite:///{tmp_path / 'esmporium.db'}")
    )
    METADATA.create_all(engine)

    calls = 0
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        with lock:
            i = calls
            calls += 1
        # A distinct dataset per request, so every worker writes its own row.
        return solr_body([solr_doc(f"d{i}")])

    # Two leaves, each over two projects -> four sub-searches.
    outcomes = search(
        for_variables("tas", "pr", projects=("CMIP5", "CMIP6")),
        build_list_selector([make_facade()]),
        client=client_for(handler),
        processor_factory=build_result_processor_factory(engine),
        max_workers=4,
    )

    try:
        assert len(outcomes) == 4
        assert saved_master_ids(engine) == {f"CMIP6.d{i}" for i in range(4)}
    finally:
        engine.dispose()


def test_parallel_workers_writing_the_same_dataset_keep_one_row(tmp_path):
    """
    Two workers that return the *same* dataset save one row, not a spurious clash

    The configured engine makes the workers' writing transactions take turns,
    so whichever writes second finds the first worker's row and reuses it.

    The two leaves ask for different variables, so the plan keeps them as two
    searches; what collides is the *answer*, because the handler hands both the same
    document. Two identical leaves would be deduplicated into one search and there
    would be nothing to race.
    """
    engine = configure_sqlite_for_concurrency(
        create_engine(f"sqlite:///{tmp_path / 'esmporium.db'}")
    )
    METADATA.create_all(engine)

    barrier = threading.Barrier(2, timeout=5)

    def handler(request: httpx.Request) -> httpx.Response:
        # Both requests reach here together, so both workers go on to write the same
        # dataset at the same time.
        barrier.wait()
        return solr_body([solr_doc("same")])

    outcomes = search(
        for_variables("tas", "pr"),
        build_list_selector([make_facade()]),
        client=client_for(handler),
        processor_factory=build_result_processor_factory(engine),
        max_workers=2,
    )

    try:
        assert len(outcomes) == 2
        assert saved_master_ids(engine) == {"CMIP6.same"}
    finally:
        engine.dispose()


def failing_for(down_project: str):
    """A handler that answers every project but `down_project`, which it 500s"""

    def handler(request: httpx.Request) -> httpx.Response:
        (project,) = request.url.params.get_list("project")
        if project == down_project:
            # Every endpoint for this project errors, so its sub-search fails outright.
            return httpx.Response(500)
        return solr_body([solr_doc(f"ok-{project}")])

    return handler


def test_a_failed_leaf_search_does_not_sink_the_ones_that_answered():
    """
    When one project's endpoints all fail, the others still come back

    The failed project is not dropped: it takes its place in the results as an empty,
    `answered`-False outcome carrying its failures, so the caller can see both what
    succeeded and what could not be reached.
    """
    outcomes = search(
        for_variables("tas", projects=("CMIP5", "CMIP6")),
        build_list_selector([make_facade()]),
        client=client_for(failing_for("CMIP6")),
    )

    assert len(outcomes) == 2
    answered = [outcome for outcome in outcomes if outcome.answered]
    unreachable = [outcome for outcome in outcomes if not outcome.answered]
    assert len(answered) == 1
    assert len(unreachable) == 1
    # The one that answered carries a real result; the one that failed carries why.
    assert any(
        doc.datasets
        for docs in answered[0].outcome.parsed_docs.values()
        for doc in docs
    )
    assert unreachable[0].outcome.parsed_docs == {}
    assert unreachable[0].outcome.failures
    # And the caller can tell which project could not be reached.
    assert unreachable[0].project == "CMIP6"


@pytest.mark.parametrize(
    ("projects", "expected"),
    [
        pytest.param(("CMIP6",), 1, id="one-sub-search"),
        pytest.param(("CMIP5", "CMIP6"), 2, id="several-sub-searches"),
    ],
)
def test_every_leaf_search_failing_raises_an_aggregate_naming_the_roles(
    projects, expected
):
    """
    When every sub-search fails, one error gathers all of them and says which is which

    Raised however many sub-searches there were, including one. A bare
    `NoFacadeAnsweredError` would say an endpoint was down without saying what we
    were asking it for, and with a requirement that is the part worth knowing.
    """
    with pytest.raises(AllSubQueriesFailedError) as excinfo:
        search(
            for_variables("tas", projects=projects),
            build_list_selector([make_facade()]),
            client=client_for(lambda r: httpx.Response(500)),
        )

    assert len(excinfo.value.failures) == expected
    assert all(
        isinstance(failure, NoFacadeAnsweredError) for failure in excinfo.value.failures
    )
    message = str(excinfo.value)
    for project in projects:
        assert f"'tas' ({project})" in message
