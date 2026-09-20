"""
The full search-and-save workflow

This is the layer that ties the search step to the database. It sits *above* both
[`esmporium.search`][] and [`esmporium.db`][]: the search package deliberately does not
import the database (it saves through the injected
[`ResultProcessor`][esmporium.search.result_parsing.ResultProcessor] seam instead), so
the "every result is saved" coupling lives here, where importing both is fine.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

import httpx
from sqlmodel import Session

from esmporium.db import build_result_processor
from esmporium.query import QueryProtocol, translate_to_projects
from esmporium.search import (
    DEFAULT_SELECTOR,
    SearchAPICallObserver,
    SearchAPIFacadeSelector,
    SearchOutcome,
    search_single_project,
)

if TYPE_CHECKING:
    from sqlalchemy import Engine


def _as_query_iterable(
    queries: QueryProtocol | Iterable[QueryProtocol],
) -> tuple[QueryProtocol, ...]:
    """
    Normalise the `queries` argument to a tuple of queries

    A single query is wrapped in a one-tuple; an iterable of queries is materialised.
    We tell the two apart by duck typing rather than `isinstance`, because
    [QueryProtocol][esmporium.query.QueryProtocol] is not `runtime_checkable`: a single
    query carries `other_terms`, an iterable of queries does not.

    Parameters
    ----------
    queries
        A single query, or an iterable of queries

    Returns
    -------
    :
        The queries as a tuple
    """
    if hasattr(queries, "other_terms"):
        # A single query, not an iterable of them.
        return (queries,)  # type: ignore[return-value]

    return tuple(queries)  # type: ignore[arg-type]


def search(  # noqa: PLR0913 - the keyword-only extras are deliberate injection seams
    queries: QueryProtocol | Iterable[QueryProtocol],
    *,
    engine: Engine,
    selector: SearchAPIFacadeSelector = DEFAULT_SELECTOR,
    stop_at_first_result: bool = True,
    limit: int = 10_000,
    max_results: int | None = 1_000_000,
    client: httpx.Client | None = None,
    api_call_observer: SearchAPICallObserver | None = None,
    project_query_map: Mapping[str, type[QueryProtocol]] | None = None,
) -> tuple[SearchOutcome, ...]:
    """
    Search one or more queries over one or more projects, saving every result

    This is the high-level entry point. Each query may name one *or more* projects; we
    split every query into one single-project query per project (via
    [translate_to_projects][esmporium.query.translate.translate_to_projects]) and run
    each through [search_single_project][esmporium.search.search.search_single_project],
    saving that sub-query's results to the database as they arrive. Saving is the point:
    the results land in the database whether or not you inspect the returned outcomes.

    A query which names no project is refused: splitting raises
    [NoTargetProjectError][esmporium.query.translate.NoTargetProjectError] before any
    endpoint is contacted or any row is written, so a bad query in the batch stops the
    whole call before it does anything.

    We do not parallelise here (that is deferred): the sub-queries run one after
    another. Because each sub-query saves and commits its results before the next one
    starts, if the process is killed part way through, the ones that already finished
    are still in the database.

    ## Running several queries to control facet logic

    Passing several queries is (once we filter results against the query, in a later
    step) the only way to express certain "this but not that" logic. The ESGF Solr API
    ORs the values within a facet, so a single query for
    `variable=[tas, ts], frequency=[mon, day]` matches all four combinations
    (tas-monthly, tas-daily, ts-monthly, ts-daily). To get, say, tas-monthly and
    ts-daily but *not* the cross terms, you have to send two queries:
    `variable=tas, frequency=mon` and `variable=ts, frequency=day`.
    (This is definitely how Solr behaves; ESGF-NG's CQL2 may allow finer AND/OR control,
    which we may support directly in future.)

    Parameters
    ----------
    queries
        A single query, or an iterable of queries. Each query may name one or more
        projects. A query that names no project is an error (see above).

    engine
        The database engine to save into. A fresh session is opened per sub-query, so
        each sub-query commits in its own transaction. Taking an engine (rather than a
        session) is what lets a later version run the sub-queries in parallel, each
        worker with its own session, without changing this signature.

    selector
        Passed straight through to
        [search_single_project][esmporium.search.search.search_single_project] for every
        sub-query. The default picks facades by the sub-query's (single) project.

    stop_at_first_result
        Passed through to each
        [search_single_project][esmporium.search.search.search_single_project] call.

    limit
        Passed through to each
        [search_single_project][esmporium.search.search.search_single_project] call.

    max_results
        Passed through to each
        [search_single_project][esmporium.search.search.search_single_project] call.

    client
        The HTTP client to search with, shared across every sub-query. If `None`, one is
        built for the call and closed at the end.

    api_call_observer
        Passed through to each
        [search_single_project][esmporium.search.search.search_single_project] call. See
        [esmporium.search.health][] for how to build one; the database-backed one is
        [record_search_api_calls][esmporium.db.search_health.record_search_api_calls].

    project_query_map
        Passed through to
        [translate_to_projects][esmporium.query.translate.translate_to_projects] when
        splitting a query, to control which query class each project uses. If `None`,
        the default mapping is used.

    Returns
    -------
    :
        One [SearchOutcome][esmporium.search.search.SearchOutcome] per sub-query, in the
        order the sub-queries ran (queries in input order; within a query, the projects
        in the order
        [translate_to_projects][esmporium.query.translate.translate_to_projects]
        yields them). Two sub-queries can answer from the same endpoint, which is why
        these are kept apart rather than merged into one endpoint-keyed outcome.

    Raises
    ------
    NoTargetProjectError
        A query in `queries` names no project. Raised while splitting, before any search
        or save happens.

    Notes
    -----
    A sub-query that fails outright (e.g. every endpoint it was offered failed) raises,
    just as [search_single_project][esmporium.search.search.search_single_project] does;
    that error propagates and stops the run. Aggregating partial failures across
    sub-queries is deliberately left for later.
    """
    # Split every query up front, so a query with no project blows up before we contact
    # any endpoint or write any row.
    sub_queries: list[QueryProtocol] = []
    for query in _as_query_iterable(queries):
        by_project = translate_to_projects(query, project_query_map=project_query_map)
        sub_queries.extend(by_project.values())

    owns_client = client is None
    client = client if client is not None else httpx.Client(follow_redirects=True)

    outcomes: list[SearchOutcome] = []
    try:
        for sub_query in sub_queries:
            # A session (and so a transaction) per sub-query: results are committed by
            # the processor as they arrive, so an earlier sub-query's results survive a
            # later one failing. This is also the seam a parallel version would give
            # each worker its own copy of.
            with Session(engine) as session:
                processor = build_result_processor(session)
                outcomes.append(
                    search_single_project(
                        sub_query,
                        selector,
                        stop_at_first_result=stop_at_first_result,
                        limit=limit,
                        max_results=max_results,
                        client=client,
                        api_call_observer=api_call_observer,
                        processor=processor,
                    )
                )
    finally:
        if owns_client:
            client.close()

    return tuple(outcomes)
