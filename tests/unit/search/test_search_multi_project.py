"""
Test the high-level `search` against a mock API and an in-memory database

These cover the behaviours `search` adds on top of `search_single_project`: splitting a
query into one search per project, running several queries, refusing a query with no
project, and — the important one — handing each sub-query's results to a fresh processor
as they arrive, so with the database-saving processor a kill part way through still
leaves the finished sub-queries in the database.
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
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
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


CMIP6_QUERY = Query(project=("CMIP6",), variable="tas", reporting_interval="mon")
"""A single-project query that splits into one CMIP6 sub-query"""


def test_finished_subqueries_survive_a_kill_mid_run(engine):
    """
    If the process is killed part way through, finished sub-queries are still saved

    This is the point of saving inside `search_single_project` (which commits each page
    as it arrives) rather than collecting everything and saving at the end: the first
    query here commits before the second is even started, so when the second blows up
    with an unhandled error, the first query's dataset is already in the database.
    """
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return solr_body([solr_doc("survivor")])
        # The second sub-query "kills" the process with an error nobody catches.
        raise ProcessKilled

    first = Query(project=("CMIP6",), variable="tas", reporting_interval="mon")
    second = Query(project=("CMIP6",), variable="pr", reporting_interval="mon")

    with pytest.raises(ProcessKilled):
        search(
            (first, second),
            processor_factory=build_result_processor_factory(engine),
            selector=build_list_selector([make_facade()]),
            client=client_for(handler),
        )

    # The first sub-query's result is committed even though the run then died.
    assert saved_master_ids(engine) == {"CMIP6.survivor"}


def test_runs_every_query_and_saves_all_results(engine):
    """Several queries each run and each save; one outcome comes back per sub-query"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        # A distinct document per request, so both sub-queries leave their own row.
        doc = solr_doc(f"d{calls}")
        calls += 1
        return solr_body([doc])

    first = Query(project=("CMIP6",), variable="tas", reporting_interval="mon")
    second = Query(project=("CMIP6",), variable="pr", reporting_interval="mon")

    outcomes = search(
        (first, second),
        processor_factory=build_result_processor_factory(engine),
        selector=build_list_selector([make_facade()]),
        client=client_for(handler),
    )

    assert len(outcomes) == 2
    assert saved_master_ids(engine) == {"CMIP6.d0", "CMIP6.d1"}


def test_splits_a_multi_project_query_into_one_search_per_project(engine):
    """A query naming two projects becomes two searches, one per project"""
    seen_projects: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_projects.append(request.url.params.get_list("project"))
        return solr_body([solr_doc("only")])

    outcomes = search(
        Query(project=("CMIP5", "CMIP6"), variable="tas", reporting_interval="mon"),
        processor_factory=build_result_processor_factory(engine),
        selector=build_list_selector([make_facade()]),
        client=client_for(handler),
    )

    # One search (and so one outcome) per project, each carrying its own project facet.
    assert len(outcomes) == 2
    assert sorted(seen_projects) == [["CMIP5"], ["CMIP6"]]


def test_a_query_with_no_project_is_refused_before_any_work(engine):
    """A query that names no project blows up before any endpoint or save happens"""
    with pytest.raises(NoTargetProjectError):
        search(
            Query(variable="tas", reporting_interval="mon"),
            processor_factory=build_result_processor_factory(engine),
            selector=build_list_selector([make_facade()]),
            client=client_for(never_asked),
        )

    # Nothing was searched, so nothing was saved.
    assert saved_master_ids(engine) == set()


def test_accepts_a_single_query_as_well_as_a_collection(engine):
    """A bare query is treated as a one-query run, not iterated as a collection"""
    outcomes = search(
        CMIP6_QUERY,
        processor_factory=build_result_processor_factory(engine),
        selector=build_list_selector([make_facade()]),
        client=client_for(lambda r: solr_body([solr_doc("single")])),
    )

    assert len(outcomes) == 1
    assert saved_master_ids(engine) == {"CMIP6.single"}


def test_parallelises_over_the_sub_queries():
    """
    With `max_workers > 1` the sub-queries are genuinely in flight at the same time

    Both requests must reach the handler together to pass the barrier. A sequential run
    would leave the second unsent while the first blocks, so the barrier would time out
    and the test would fail rather than hang.
    """
    barrier = threading.Barrier(2, timeout=5)

    def handler(request: httpx.Request) -> httpx.Response:
        barrier.wait()
        return solr_body([solr_doc("d")])

    outcomes = search(
        Query(project=("CMIP5", "CMIP6"), variable="tas", reporting_interval="mon"),
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
            Query(project=("CMIP5", "CMIP6"), variable="tas", reporting_interval="mon"),
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

    # Two two-project queries -> four single-project sub-queries.
    queries = (
        Query(project=("CMIP5", "CMIP6"), variable="tas", reporting_interval="mon"),
        Query(project=("CMIP5", "CMIP6"), variable="pr", reporting_interval="mon"),
    )

    outcomes = search(
        queries,
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
    """
    engine = configure_sqlite_for_concurrency(
        create_engine(f"sqlite:///{tmp_path / 'esmporium.db'}")
    )
    METADATA.create_all(engine)

    barrier = threading.Barrier(2, timeout=5)

    def handler(request: httpx.Request) -> httpx.Response:
        # Both requests reach here together, so both workers ingest the same dataset at
        # once and race on the insert.
        barrier.wait()
        return solr_body([solr_doc("same")])

    # Two identical single-project queries -> two sub-queries, one same dataset.
    queries = (
        Query(project=("CMIP6",), variable="tas", reporting_interval="mon"),
        Query(project=("CMIP6",), variable="tas", reporting_interval="mon"),
    )

    outcomes = search(
        queries,
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
            # Every endpoint for this project errors, so its sub-query fails outright.
            return httpx.Response(500)
        return solr_body([solr_doc(f"ok-{project}")])

    return handler


def test_a_failed_sub_query_does_not_sink_the_ones_that_answered():
    """
    When one project's endpoints all fail, the others still come back

    The failed project is not dropped: it takes its place in the results as an empty,
    `answered`-False outcome carrying its failures, so the caller can see both what
    succeeded and what could not be reached.
    """
    outcomes = search(
        Query(project=("CMIP5", "CMIP6"), variable="tas", reporting_interval="mon"),
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
        doc.datasets for docs in answered[0].parsed_docs.values() for doc in docs
    )
    assert unreachable[0].parsed_docs == {}
    assert unreachable[0].failures


def test_a_single_failed_sub_query_re_raises_its_own_error():
    """A one-project search that fails raises NoFacadeAnsweredError, wrapper or not"""
    with pytest.raises(NoFacadeAnsweredError):
        search(
            Query(project=("CMIP6",), variable="tas", reporting_interval="mon"),
            build_list_selector([make_facade()]),
            client=client_for(failing_for("CMIP6")),
        )


def test_all_sub_queries_failing_raises_an_aggregate():
    """When every sub-query fails, one error gathers all of them, none lost"""
    with pytest.raises(AllSubQueriesFailedError) as excinfo:
        search(
            Query(project=("CMIP5", "CMIP6"), variable="tas", reporting_interval="mon"),
            build_list_selector([make_facade()]),
            client=client_for(lambda r: httpx.Response(500)),
        )

    assert len(excinfo.value.failures) == 2
    assert all(
        isinstance(failure, NoFacadeAnsweredError) for failure in excinfo.value.failures
    )
