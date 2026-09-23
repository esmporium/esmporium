"""
The search entry points for single project and multi-project queries
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from typing import TypeAlias, cast

import httpx

from esmporium.query import (
    QueryProtocol,
    as_query_iterable,
    to_canonical,
    translate_to_projects,
)
from esmporium.search.health import SearchAPICallObserver
from esmporium.search.result_parsing import ParsedDocument, ResultProcessor
from esmporium.search.search.errors import (
    AllSubQueriesFailedError,
    ClashingFacetsForFacadeError,
    CouldNotSearchError,
    NoFacadeAnsweredError,
)
from esmporium.search.search.keys import FacadeKey, get_facade_key
from esmporium.search.search.pagination import collect_all_pages
from esmporium.search.search_api_facade import (
    DEFAULT_SELECTOR,
    ClashingFacetsError,
    SearchAPIFacadeSelector,
    SelectorOfferedNoAPIFacadeError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchOutcome:
    """
    What came of a search: the datasets found, how many matched, and what failed

    We deliberately do not carry the raw JSON here.
    Each host's raw documents are kept on the parsed `ParsedDocument.raw_json`,
    so re-exposing the whole response envelope would be redundant.
    The one envelope value worth keeping, the total number of records that matched,
    is surfaced explicitly as `n_matches`.
    """

    parsed_docs: dict[FacadeKey, tuple[ParsedDocument, ...]]
    """The parsed documents each facade answered with, keyed by the facade"""

    n_matches: dict[FacadeKey, int | None]
    """
    How many records each facade reported matched the search, keyed by the facade

    This is the total matched,
    which can exceed the number of documents returned in one page.
    `None` for a facade whose response carried no count we could read.
    """
    # TODO: consider raising if we can't get the number of matches in future.

    failures: dict[FacadeKey, CouldNotSearchError]
    """Reasons we failed to get search results, keyed by the facade"""

    @property
    def answered(self) -> bool:
        """
        Whether any facade answered, i.e. this is a real answer and not a pure failure

        `True` for any outcome a search actually returned (even one where every facade
        matched zero records). `False` only for the empty, failures-only outcome that
        the multi-query [search][(m).] puts in place of a sub-query whose endpoints all
        failed, so a caller can tell "this project came back empty" from "this project
        could not be reached" -- the latter has `answered` `False` and a populated
        `failures`.
        """
        return bool(self.parsed_docs)


def search_single_project(  # noqa: PLR0913 - the keyword-only extras are deliberate injection seams
    query: QueryProtocol,
    selector: SearchAPIFacadeSelector = DEFAULT_SELECTOR,
    *,
    stop_at_first_result: bool = True,
    limit: int = 10_000,
    max_results: int | None = None,
    warn_on_pagination: bool = True,
    client: httpx.Client | None = None,
    api_call_observer: SearchAPICallObserver | None = None,
    processor: ResultProcessor | None = None,
) -> SearchOutcome:
    """
    Search the facades the selector yields, and parse their answers into datasets

    This is the low-level, single-project building block: `query` must name exactly
    one project (the selector and facades enforce that). To search several projects,
    or to run several queries through one call, use the higher-level [search][(m).],
    which splits multi-project queries and calls this function once per project.

    Parameters
    ----------
    query
        The query to use for the search

    selector
        Chooses which facade to try at each attempt, and when to stop.

    stop_at_first_result
        If `True` (the default), return as soon as one endpoint answers.
        The index nodes largely mirror one another, so one good answer can be enough.

        If `False`, work through every endpoint the selector yields
        and keep each one's answer.
        The nodes do not hold exactly the same data,
        so the union across them is more complete than any single node
        (callers must handle merging and de-duplicating the union themselves).

    limit
        The page size to ask each endpoint for,
        i.e. the most records to get in one response, not the total matched.
        The total matched comes back in the response itself.

        When more records match than fit in one page, we page through the rest:
        every page is fetched, parsed and handed to `processor` as it arrives, and
        the parsed documents from all pages end up in the outcome's `parsed_docs`.

    max_results
        The most records to collect from one endpoint before stopping with a
        [PaginationLimitError][esmporium.search.search.PaginationLimitError].

        This is an optional safety guardrail against a runaway search over a huge
        result set: set it to a number to cap how much one endpoint may return.
        It defaults to `None` (no cap), so a search fetches every matching record
        unless you ask it not to.

        Turning this off does not remove the loop protection: an endpoint that asks
        us to re-request a page we already fetched still stops with a
        [PaginationLimitError][esmporium.search.search.PaginationLimitError], because
        that would otherwise page forever.

    warn_on_pagination
        Whether to warn (with a
        [PaginationWarning][esmporium.search.search.PaginationWarning]) when a search
        matches more records than fit in one page, so you know it will make several
        requests and may take a while.

        On by default; pass `False` to silence it (e.g. once you already expect the
        search to be large). The warning is its own category, so it can also be
        silenced or escalated on its own through the [warnings][] machinery.

    client
        The HTTP client to search with.
        If `None`, one is built for the call and closed at the end.

    api_call_observer
        Told about each request to each API.

        If `None` (the default), nothing is recorded.
        See [esmporium.search.health][] for how to build one.

    processor
        Called with `(facade, parsed_documents)` as soon as each page is parsed,
        so its results can be acted on (e.g. saved) the moment they arrive
        rather than at the end.
        If `None` (the default),
        the parsed documents are still collected into the returned outcome,
        they are just not handed anywhere.
        See [`esmporium.db.build_result_processor`][] for the database-saving one.

    Returns
    -------
    :
        Results of the search

    Raises
    ------
    SelectorOfferedNoAPIFacadeError
        `selector` had no facade to offer for this query at all

    NoFacadeAnsweredError
        The selector offered at least one facade
        and none of them gave us results we could use

    PaginationLimitError
        Paging through an endpoint hit the `max_results` cap, or the endpoint asked
        us to re-request a page we had already requested. Pages fetched before this
        have already been handed to `processor`.
    """
    canonical = to_canonical(query)

    parsed_docs: dict[FacadeKey, tuple[ParsedDocument, ...]] = {}
    n_matches: dict[FacadeKey, int | None] = {}
    failures: dict[FacadeKey, CouldNotSearchError] = {}

    owns_client = client is None
    client = client if client is not None else httpx.Client(follow_redirects=True)

    selector_offered_an_option = False

    try:
        attempt = 0
        while (facade := selector(canonical, attempt)) is not None:
            selector_offered_an_option = True
            facade_key = get_facade_key(facade)

            try:
                request = facade.build_search_request(canonical, limit)
            except ClashingFacetsError as exc:
                # Explain the clash in terms of the query the user actually wrote.
                # When `search` split a multi-project query, `query` here is the
                # translated, project-specific one and `source_query` is the original
                # (e.g. the generic `Query`, or a `QueryCMIP6` re-projected to CMIP7),
                # so its facet names are the ones the user can act on. A query that was
                # not translated has no `source_query`, so we fall back to it.
                report_query = (
                    query.source_query if query.source_query is not None else query
                )
                failures[facade_key] = ClashingFacetsForFacadeError(
                    query=report_query, facade=facade, clashing=exc.clashing
                )
            else:
                # The facade knows this host's format and project, so it fetches and
                # turns that answer into datasets here, the moment it arrives, paging
                # through the rest of the results as it goes.
                # Note: if the selector offers the same host twice,
                # the second answer simply replaces the first here.
                # That is wasteful, because we run the query again, but it is not wrong:
                # the answers are for the same query from the same host,
                # so either will do.
                # This way of handling results is only safe
                # because we only handle a single query in this function
                # and our facades only support searching a single project at a time.
                # If either of those assumptions changed, this would break.
                # Queries over multiple projects (and multiple queries at once) are
                # handled a level up, in `search`, which splits them into
                # single-project queries and calls this function for each.
                pages = collect_all_pages(
                    client,
                    facade,
                    request,
                    limit=limit,
                    max_results=max_results,
                    warn_on_pagination=warn_on_pagination,
                    api_call_observer=api_call_observer,
                    processor=processor,
                )
                # Keep whatever pages we got, even if paging then failed: an empty
                # but completed answer (no matches) belongs here too, so we key on
                # "we parsed at least the first page", not "we collected something".
                if pages.completed or pages.collected:
                    parsed_docs[facade_key] = pages.collected
                    n_matches[facade_key] = pages.n_matches
                if pages.failure is not None:
                    failures[facade_key] = pages.failure

                # Only a facade we fully paged through counts as "a result": a partial
                # answer should not stop us asking the next endpoint for a complete one.
                if pages.completed and stop_at_first_result:
                    break

            attempt += 1

    finally:
        if owns_client:
            client.close()

    if not selector_offered_an_option:
        raise SelectorOfferedNoAPIFacadeError(canonical, selector)

    if not parsed_docs and failures:
        raise NoFacadeAnsweredError(failures)

    return SearchOutcome(parsed_docs, n_matches, failures)


ProcessorFactory: TypeAlias = Callable[
    [], AbstractContextManager[ResultProcessor | None]
]
"""
Makes a fresh result processor for one sub-query, as a context manager

[search][(m).] calls this once per sub-query and enters the context around that
sub-query's search, so each sub-query gets its own processor and anything that
processor holds (e.g. a database session and its transaction) is set up before the
search and torn down after.
"""


def search(  # noqa: PLR0913 - the keyword-only extras are deliberate injection seams
    queries: QueryProtocol | Iterable[QueryProtocol],
    selector: SearchAPIFacadeSelector = DEFAULT_SELECTOR,
    *,
    stop_at_first_result: bool = True,
    limit: int = 10_000,
    max_results: int | None = None,
    warn_on_pagination: bool = True,
    client: httpx.Client | None = None,
    api_call_observer: SearchAPICallObserver | None = None,
    processor_factory: ProcessorFactory | None = None,
    project_query_map: Mapping[str, type[QueryProtocol]] | None = None,
    max_workers: int | None = None,
) -> tuple[SearchOutcome, ...]:
    """
    Search one or more queries over one or more projects

    This is the high-level entry point. Each query may name one *or more* projects; we
    split every query into one single-project query per project (via
    [translate_to_projects][esmporium.query.translate.translate_to_projects]) and run
    each through [search_single_project][(m).], handing that sub-query's results to a
    fresh processor as they arrive.

    Each query must still name at least one project, else we raise an error.

    By default the sub-queries run sequentially. Pass `max_workers > 1` to run them
    concurrently in a thread pool: the work is network-bound, so this is usually a large
    win when there are several sub-queries.

    Parameters
    ----------
    queries
        A single query, or an iterable of queries. Each query may name one or more
        projects. A query that names no project is an error (see above).

    selector
        Passed straight through to [search_single_project][(m).] for every sub-query.
        The default picks facades by the sub-query's (single) project.

    stop_at_first_result
        Passed through to each [search_single_project][(m).] call.

    limit
        Passed through to each [search_single_project][(m).] call.

    max_results
        Passed through to each [search_single_project][(m).] call.

    warn_on_pagination
        Passed through to each [search_single_project][(m).] call.

    client
        The HTTP client to search with, shared across every sub-query. If `None`, one is
        built for the call and closed at the end.

    api_call_observer
        Passed through to each [search_single_project][(m).] call. See
        [esmporium.search.health][] for how to build one; the database-backed one is
        [record_search_api_calls][esmporium.db.search_health.record_search_api_calls].

    processor_factory
        Called once per sub-query to make the processor for that sub-query, as a
        context manager entered around the sub-query's search (see
        [ProcessorFactory][(m).]). If `None` (the default), no processor is used: the
        results are still returned in the outcomes, they are just not handed anywhere.
        To save every result to the database, pass
        [esmporium.db.build_result_processor_factory][].

    project_query_map
        Passed through to
        [translate_to_projects][esmporium.query.translate.translate_to_projects] when
        splitting a query, to control which query class each project uses. If `None`,
        the default mapping is used.

    max_workers
        How many sub-queries to run at once. `None` (the default) or `1` runs them
        sequentially, one after another. A value greater than `1` runs them concurrently
        in a thread pool of that size (see "Running sub-queries in parallel" above).

    Returns
    -------
    :
        One [SearchOutcome][(m).] per sub-query, in the order the sub-queries ran
        (queries in input order; within a query, the projects in the order
        [translate_to_projects][esmporium.query.translate.translate_to_projects] yields
        them). Two sub-queries can answer from the same endpoint, which is why these are
        kept apart rather than merged into one endpoint-keyed outcome.

        A sub-query whose endpoints all failed still gets an entry, in place, so the
        result stays aligned with the sub-queries: an empty outcome carrying that
        sub-query's `failures` and reporting `answered` as `False`. This is only reached
        if at least one sub-query answered (see `Raises`).

    Raises
    ------
    NoTargetProjectError
        A query in `queries` names no project. Raised while splitting, before any search
        or processor runs.

    NoFacadeAnsweredError
        Every sub-query failed and there was exactly one, so its failure is re-raised
        unchanged (a single-project search fails the same way with or without this
        wrapper).

    AllSubQueriesFailedError
        Every sub-query failed and there were several, so all their failures are
        gathered into one error rather than any being lost.
    """
    # Split every query up front, so a query with no project blows up before we contact
    # any endpoint or run any processor.
    sub_queries: list[QueryProtocol] = []
    for query in as_query_iterable(queries):
        by_project = translate_to_projects(query, project_query_map=project_query_map)
        sub_queries.extend(by_project.values())

    owns_client = client is None
    client = client if client is not None else httpx.Client(follow_redirects=True)

    def run_one(
        sub_query: QueryProtocol,
    ) -> tuple[SearchOutcome, NoFacadeAnsweredError | None]:
        # A fresh processor (and so, for the saving one, a fresh session and
        # transaction) per sub-query: entered around this sub-query's search and exited
        # after. This is the seam that makes a worker independent -- it owns its session
        # and shares nothing mutable -- so it is safe to call from a thread. `client` is
        # shared: httpx.Client is thread-safe for concurrent requests.
        processor_cm: AbstractContextManager[ResultProcessor | None] = (
            nullcontext(None) if processor_factory is None else processor_factory()
        )
        try:
            with processor_cm as processor:
                outcome = search_single_project(
                    sub_query,
                    selector,
                    stop_at_first_result=stop_at_first_result,
                    limit=limit,
                    max_results=max_results,
                    warn_on_pagination=warn_on_pagination,
                    client=client,
                    api_call_observer=api_call_observer,
                    processor=processor,
                )
        except NoFacadeAnsweredError as exc:
            # This sub-query's endpoints all failed. Keep going: turn it into an empty
            # outcome carrying the failures, so a caller still gets the sub-queries that
            # did answer (and can see this one did not, via `SearchOutcome.answered`).
            # Anything else (a config error, a pagination-cap breach, a bug) is not a
            # "node was down" and is left to propagate and stop the whole run.
            # `exc.failures` came from search_single_project, so it is that variant.
            failures = cast("dict[FacadeKey, CouldNotSearchError]", exc.failures)
            return SearchOutcome({}, {}, failures), exc
        return outcome, None

    try:
        if max_workers is None or max_workers == 1 or len(sub_queries) <= 1:
            # Sequential: results are handled and committed one sub-query at a time.
            results = [run_one(sq) for sq in sub_queries]
        else:
            # Parallel: `map` preserves input order and, if a worker raises something
            # we do not fold in (see run_one), re-raises the first such error only once
            # the executor has joined the rest, so the others' committed results stay.
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                results = list(executor.map(run_one, sub_queries))
    finally:
        if owns_client:
            client.close()

    caught = [exc for _, exc in results if exc is not None]
    if caught and len(caught) == len(results):
        # Every sub-query failed, so there is nothing to return: fail loudly. A lone
        # failure re-raises as-is, so a single-project search fails exactly as it would
        # without the wrapper; several failures are gathered so none is lost.
        if len(caught) == 1:
            raise caught[0]
        raise AllSubQueriesFailedError(tuple(caught))

    return tuple(outcome for outcome, _ in results)
