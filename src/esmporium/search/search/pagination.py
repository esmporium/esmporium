"""
Paging through one facade's whole answer to one request
"""

from __future__ import annotations

import json
import logging
import math
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any, Generic, TypeVar

import httpx

from esmporium.search.apis import Request, UnreadableResponseError
from esmporium.search.health import SearchAPICallObserver
from esmporium.search.result_parsing import (
    FileResultProcessor,
    ParsedDocument,
    ParsedFileDocument,
    ResultProcessor,
)
from esmporium.search.search.errors import (
    CouldNotGetSearchResponseError,
    CouldNotSearchError,
    CouldNotUseSearchResultsError,
    PaginationLimitError,
    PaginationWarning,
    SearchAPIRequestError,
)
from esmporium.search.search.firing import fire, get_url
from esmporium.search.search.keys import FacadeKey, get_facade_key
from esmporium.search.search_api_facade import SearchAPIFacade

logger = logging.getLogger(__name__)

Parsed = TypeVar("Parsed")
"""What one page of a response parses into, e.g. a [ParsedDocument][esmporium.search.ParsedDocument]"""  # noqa: E501


@dataclass(frozen=True)
class Pages(Generic[Parsed]):
    """The outcome of paging through one facade's answer to one request"""

    collected: tuple[Parsed, ...]
    """Every parsed document we fetched, across all pages"""

    n_matches: int | None
    """The total the endpoint reported matched, read from the first page"""

    completed: bool
    """Whether we fetched every page (`False` if a page failed part way)"""

    failure: CouldNotSearchError | None
    """The failure that stopped us, if paging did not complete"""


FacadePages = Pages[ParsedDocument]
"""The outcome of paging through one facade's answer to a *dataset* search"""

FilePages = Pages[ParsedFileDocument]
"""
The outcome of paging through one facade's answer to a `type=File` request

The same four things as [FacadePages][(m).FacadePages], because the hazards of paging
do not care what is being paged: see
[collect_all_file_pages][(m).collect_all_file_pages].
"""


# TODO: In a future PR add this to SearchAPIHealth ?
def _request_fingerprint(request: Request) -> str:
    """
    Render a request as a stable string, so repeats can be spotted

    Used to detect an endpoint that asks us to re-request a page we have already
    requested, which would otherwise page forever.

    Parameters
    ----------
    request
        The request to render

    Returns
    -------
    :
        A string that is equal for two requests exactly when they would be sent the
        same way
    """
    return json.dumps(
        {
            "method": request.method,
            "path": request.path,
            "params": request.params,
            "json_body": request.json_body,
        },
        sort_keys=True,
        default=str,
    )


def _warn_if_paginating(host: str, *, n_matches: int, limit: int) -> None:
    """
    Warn that a search will page through several requests, if it will

    A search pages whenever more records match than fit in one page (`limit`), and
    the number of requests that takes is `ceil(n_matches / limit)`: a big result set
    with a small page size is many small requests, and a huge result set is many
    requests even at the largest page size. Either can take a while, so we say so.

    Parameters
    ----------
    host
        The host being searched, named in the warning

    n_matches
        How many records the endpoint reported matched

    limit
        The page size that was asked for, i.e. the most records in one response

    Warns
    -----
    PaginationWarning
        `n_matches` exceeds `limit`, so the search will page
    """
    if n_matches <= limit:
        return

    pages = math.ceil(n_matches / limit)
    warnings.warn(
        f"This search of {host} matched {n_matches:,} records but fetches "
        f"{limit:,} per page, so it will page through about {pages:,} requests "
        "and may take a while. Raise `limit` to fetch more records per request, "
        "narrow your query, or pass warn_on_pagination=False to silence this.",
        PaginationWarning,
        stacklevel=2,
    )


def collect_all_pages(  # noqa: PLR0913 - the keyword-only extras are injection seams
    client: httpx.Client,
    facade: SearchAPIFacade,
    request: Request,
    *,
    limit: int,
    max_results: int | None,
    warn_on_pagination: bool,
    api_call_observer: SearchAPICallObserver | None,
    processor: ResultProcessor | None,
) -> FacadePages:
    """
    Fetch and page through one facade's whole answer, parsing each page as it arrives

    This sends `request`, then follows the endpoint's own pagination page by page (see
    [esmporium.search.apis.SearchAPI.next_page_request][]) until there are no more,
    fetching every page itself.

    Each page is handed to `processor` the moment it is parsed, so a failure part way
    still leaves the earlier pages saved. A failure -- fetching the first page or a
    later one, or reading any of them -- is returned on the result rather than raised,
    so the caller can keep the pages we did get and record the failure against the
    facade.

    Parameters
    ----------
    client
        The HTTP client to fetch the pages with

    facade
        The facade whose answer we are paging through

    request
        The search request to send for the first page

    limit
        The page size that was asked for, i.e. the most records in one response

        Used only to work out (against the total matched) how many requests paging
        will take, for the
        [PaginationWarning][esmporium.search.search.PaginationWarning].

    max_results
        The most records to collect before stopping with a
        [PaginationLimitError][esmporium.search.search.PaginationLimitError].
        `None` disables the cap.

    warn_on_pagination
        Whether to emit a
        [PaginationWarning][esmporium.search.search.PaginationWarning] when the
        endpoint reports more matches than fit in one page, so the caller knows paging
        is happening.

    api_call_observer
        Told about each further request, as in [fire][esmporium.search.search.fire]

    processor
        Handed each page as it is parsed, as in [search][esmporium.search.search.search]

    Returns
    -------
    :
        What we collected, the total matched, and whether we got every page

    Raises
    ------
    PaginationLimitError
        We collected more than `max_results`, or the endpoint asked us to
        re-request a page we had already requested
    """
    return _collect_all_pages(
        client,
        facade,
        request,
        limit=limit,
        max_results=max_results,
        warn_on_pagination=warn_on_pagination,
        api_call_observer=api_call_observer,
        parse_page=facade.parse_search_results,
        processor=processor,
    )


def collect_all_file_pages(  # noqa: PLR0913 - the keyword-only extras are injection seams
    client: httpx.Client,
    facade: SearchAPIFacade,
    request: Request,
    *,
    limit: int,
    max_results: int | None,
    warn_on_pagination: bool,
    api_call_observer: SearchAPICallObserver | None,
    processor: FileResultProcessor | None,
) -> FilePages:
    """
    Fetch and page through one facade's whole answer to a `type=File` request

    The file-level counterpart of [collect_all_pages][(m).collect_all_pages], and the
    same function underneath: the only difference is that each page is read with
    [parse_file_search_results][esmporium.search.SearchAPIFacade.parse_file_search_results]
    rather than as dataset documents.

    They share an engine on purpose. Everything that can go wrong while paging --
    an endpoint which asks us to re-request a page we have already had (which would
    page forever), a result set larger than we meant to pull, a page that fails part
    way through -- goes wrong the same way whatever the pages contain, and files are
    where it bites hardest: a `type=File` request against a whole batch of datasets
    is the largest answer we ever ask for.

    Parameters
    ----------
    client
        The HTTP client to fetch the pages with

    facade
        The facade whose answer we are paging through

        Must be a Solr-shaped one; ESGF-NG carries its files in the search response
        itself.

    request
        The file search request to send for the first page

        Usually built with
        [build_file_search_request][esmporium.search.SearchAPIFacade.build_file_search_request].

    limit
        The page size that was asked for, i.e. the most files in one response

    max_results
        The most file documents to collect before stopping with a
        [PaginationLimitError][esmporium.search.search.PaginationLimitError].
        `None` disables the cap.

    warn_on_pagination
        Whether to emit a
        [PaginationWarning][esmporium.search.search.PaginationWarning] when the
        endpoint reports more matches than fit in one page

    api_call_observer
        Told about each request, as in [fire][esmporium.search.search.fire]

    processor
        Handed each page as it is parsed, so a failure part way still leaves the
        earlier pages saved

        The database layer supplies one; see
        [esmporium.db.build_file_result_processor][].

    Returns
    -------
    :
        What we collected, the total matched, and whether we got every page

    Raises
    ------
    NotASolrFacadeError
        `facade` has no `type=File` request to make, so it cannot answer one either

    PaginationLimitError
        We collected more than `max_results`, or the endpoint asked us to
        re-request a page we had already requested
    """
    # Checked before the first fetch rather than when the first page is parsed, so a
    # facade which could never answer this is refused without a request going out.
    facade.file_search_api()

    return _collect_all_pages(
        client,
        facade,
        request,
        limit=limit,
        max_results=max_results,
        warn_on_pagination=warn_on_pagination,
        api_call_observer=api_call_observer,
        parse_page=facade.parse_file_search_results,
        processor=processor,
    )


def _collect_all_pages(  # noqa: PLR0913 - the keyword-only extras are injection seams
    client: httpx.Client,
    facade: SearchAPIFacade,
    request: Request,
    *,
    limit: int,
    max_results: int | None,
    warn_on_pagination: bool,
    api_call_observer: SearchAPICallObserver | None,
    parse_page: Callable[[dict[str, Any]], tuple[Parsed, ...]],
    processor: Callable[[SearchAPIFacade, tuple[Parsed, ...]], None] | None,
) -> Pages[Parsed]:
    """
    Page through one facade's whole answer, reading each page with `parse_page`

    The engine behind [collect_all_pages][(m).collect_all_pages] and
    [collect_all_file_pages][(m).collect_all_file_pages]; see either for what the
    shared parameters mean.

    Parameters
    ----------
    parse_page
        Reads one raw page into the records it carries

    processor
        Handed each page as it is parsed

    Returns
    -------
    :
        What we collected, the total matched, and whether we got every page
    """
    api = facade.search_api
    facade_key: FacadeKey = get_facade_key(facade)
    collected: list[Parsed] = []
    # Stays None if the first fetch fails before we can read a count.
    n_matches: int | None = None
    seen: set[str] = set()
    try:
        fire_here = partial(
            fire,
            client=client,
            api=api,
            api_call_observer=api_call_observer,
        )
        raw = fire_here(request=request)
        # Read the count directly: a search response
        # that omits its total is one we cannot use, so let the raise propagate to the
        # UnreadableResponseError handler below rather than swallowing it to None.
        n_matches = api.get_n_matches(raw)
        if warn_on_pagination:
            _warn_if_paginating(facade_key[0], n_matches=n_matches, limit=limit)
        seen = {_request_fingerprint(request)}

        while True:
            parsed = parse_page(raw)
            collected.extend(parsed)
            if processor is not None:
                processor(facade, parsed)

            if max_results is not None and len(collected) > max_results:
                raise PaginationLimitError(
                    facade_key,
                    f"collected more than the max_results safety cap of "
                    f"{max_results}. Raise max_results (or set it to None) to fetch "
                    "more.",
                )

            nxt = api.next_page_request(request, raw)
            if nxt is None:
                break

            fingerprint = _request_fingerprint(nxt)
            if fingerprint in seen:
                raise PaginationLimitError(
                    facade_key,
                    "the endpoint asked us to re-request a page we had already "
                    "requested, which would page forever.",
                )
            seen.add(fingerprint)

            request = nxt
            raw = fire_here(request=nxt)
    except SearchAPIRequestError as exc:
        return Pages(
            tuple(collected),
            n_matches,
            completed=False,
            failure=CouldNotGetSearchResponseError(facade_key, cause=exc),
        )
    except UnreadableResponseError as exc:
        return Pages(
            tuple(collected),
            n_matches,
            completed=False,
            failure=CouldNotUseSearchResultsError(
                facade_key, cause=exc, url=get_url(api, request)
            ),
        )

    # Paged to the end. If the endpoint reported a total, note when what we collected
    # differs from it. The total is read from the first page while the pages after it
    # are fetched one by one, so an index being published to (or retracted from) while
    # we scan it gives us more records than the total promised as readily as fewer.
    # Either way the pages we got are all the endpoint offered us, so this is a
    # heads-up, not a failure.
    if n_matches is not None and len(collected) != n_matches:
        logger.debug(
            "%s paged to the end but collected %d records against the %d it reported "
            "matched (the index may have shifted mid-scan)",
            facade_key[0],
            len(collected),
            n_matches,
        )

    return Pages(tuple(collected), n_matches, completed=True, failure=None)
