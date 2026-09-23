"""
High-level search functionality
"""

from __future__ import annotations

import json
import logging
import math
import shlex
import time
import warnings
from collections.abc import Callable, Collection, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, TypeAlias, cast

import httpx

from esmporium.formatting import readable_list
from esmporium.query import (
    QueryProtocol,
    as_query_iterable,
    facet_spec,
    to_canonical,
    translate_to_projects,
)
from esmporium.search.apis import (
    NoSearchResultNumberOfMatchesReturnedError,
    Request,
    SearchAPI,
    UnreadableResponseError,
)
from esmporium.search.health import SearchAPICall, SearchAPICallObserver
from esmporium.search.result_parsing import ParsedDocument, ResultProcessor
from esmporium.search.search_api_facade import (
    DEFAULT_SELECTOR,
    ClashingFacetsError,
    NMatchesReader,
    SearchAPIFacade,
    SearchAPIFacadeSelector,
    SelectorOfferedNoAPIFacadeError,
)

if TYPE_CHECKING:
    from esmporium.search.check_query_values import CouldNotGetAllowedValuesError

logger = logging.getLogger(__name__)

FacadeKey: TypeAlias = tuple[str, str, str]
"""
A key which identifies a given facade

The first element is the host.
The second is the type of search API this facade uses/assumes.
The third is the query style used by this facade.
"""


def get_facade_key(facade: SearchAPIFacade) -> FacadeKey:
    """
    Get the key which identifies a facade

    Each element can be served by more than one facade,
    which is why we group them like this.

    Parameters
    ----------
    facade
        The facade to identify

    Returns
    -------
    :
        The facade's key
    """
    return (
        facade.search_api.host,
        type(facade.search_api).__name__,
        facade.parameters.base_query_style.__name__,
    )


def get_url(api: SearchAPI, request: Request) -> str:
    """
    Build the full URL for a request to a given API

    Parameters
    ----------
    api
        The API we want to ask

    request
        The request to make

    Returns
    -------
    :
        The URL to send `request` to
    """
    return f"{api.scheme}://{api.host}{request.path}"


def curl_equivalent(request: httpx.Request) -> str:
    """
    Render an httpx request as a `curl` command which reproduces it

    Every piece is shell-quoted, so the result is safe to paste into a terminal.

    Parameters
    ----------
    request
        The request to render

    Returns
    -------
    :
        A `curl` command equivalent to `request`
    """
    parts = ["curl", "-X", request.method]
    for name, value in request.headers.items():
        parts += ["-H", shlex.quote(f"{name}: {value}")]

    body = request.content
    if body:
        parts += ["--data", shlex.quote(body.decode("utf-8", errors="replace"))]

    parts.append(shlex.quote(str(request.url)))

    return " ".join(parts)


def log_request_as_url_and_curl(
    api: SearchAPI, request: httpx.Request, log_level: int = logging.DEBUG
) -> None:
    """
    Log a request we are about to send, at `DEBUG`

    Parameters
    ----------
    api
        The API the request is going to

    request
        The request

    log_level
        Level at which to log
    """
    if not logger.isEnabledFor(log_level):
        # Skip rendering if the logger is not enabled for the given level
        return

    url = str(request.url)
    curl = curl_equivalent(request)
    logger.log(
        log_level,
        "search request to %s\n%s %s\n%s",
        api.host,
        request.method,
        url,
        curl,
        extra={
            "search_api_host": api.host,
            "http_method": request.method,
            "http_url": url,
            "http_curl": curl,
        },
    )


class SearchAPIRequestError(RuntimeError):
    """
    Raised when one request to one search API does not come back with a usable answer

    This is the low-level failure that [fire][(m).fire] raises,
    whether the host never answered (a transport error or timeout)
    or answered with something we could not use (a bad status, unreadable JSON).

    Higher-level callers are expected to translate this into their own terms.
    """

    def __init__(self, host: str) -> None:
        """
        Initialise the error

        Parameters
        ----------
        host
            The host the request went to
        """
        self.host = host
        super().__init__(
            f"{host} did not come back with a usable answer to our request."
        )


def _result_count_or_none(
    read_n_matches: NMatchesReader | None, raw: dict[str, Any]
) -> int | None:
    """
    Read how many records a response reported, or `None` if it reported none

    A search response reports its total; the STAC facet-values response
    (a collection document) does not, hence the tolerance.

    Parameters
    ----------
    read_n_matches
        Reads the count out of `raw`, or `None` if the caller cannot say how.

    raw
        The response to read

    Returns
    -------
    :
        The number of records reported, or `None` if the response carries no count
        (or if nothing that could read one was passed)
    """
    if read_n_matches is None:
        return None

    try:
        return read_n_matches(raw)
    except NoSearchResultNumberOfMatchesReturnedError:
        return None


def fire(
    client: httpx.Client,
    api: SearchAPI,
    request: Request,
    api_call_observer: SearchAPICallObserver | None = None,
    read_n_matches: NMatchesReader | None = None,
) -> dict[str, Any]:
    """
    Send one request to one API, using that API's retry policy and timeout

    Parameters
    ----------
    client
        The HTTP client to send with

    api
        The API to send to (this also carries the retry policy and timeout)

    request
        The request to send

    api_call_observer
        Told about this call once it is done, on both the success and failure path.
        If `None` (the default), nothing is recorded.
        See [esmporium.search.health][] for how to build one.

    read_n_matches
        Reads how many records the answer says matched, for the observer to record.
        If `None`, no count is recorded.

    Returns
    -------
    :
        The raw JSON the endpoint answered with

    Raises
    ------
    SearchAPIRequestError
        The API never answered, or answered with something we cannot use.
        The underlying cause is carried as the error's `__cause__`.
    """
    built = client.build_request(
        request.method,
        get_url(api, request),
        params=request.params,
        json=request.json_body,
        timeout=api.timeout,
    )
    log_request_as_url_and_curl(api, built)

    request_body = json.dumps(request.json_body) if request.json_body else None

    def _record(  # noqa: PLR0913 - one keyword-only argument per recorded field
        *,
        attempt_number: int,
        success: bool,
        response_code: int | None,
        error: Exception | None,
        num_results: int | None,
        seconds: float,
    ) -> None:
        """Tell the observer, if any, how one attempt went."""
        if api_call_observer is None:
            return
        api_call_observer(
            SearchAPICall(
                host=api.host,
                http_method=request.method,
                url=str(built.url),
                request_body=request_body,
                response_code=response_code,
                success=success,
                error=error,
                num_results=num_results,
                response_time_seconds=seconds,
                attempt_number=attempt_number,
            )
        )

    # Counts the attempts as the retry policy works through them, so each
    # recorded row can say which attempt it was. It has to persist across the
    # calls the retry policy makes to `_attempt`, hence the `nonlocal`.
    attempt = 0

    def _attempt() -> dict[str, Any]:
        # One HTTP attempt: sent, checked and parsed here, so it records itself
        # (with everything in scope) before returning or raising. The retry
        # policy calls this once per attempt, so recording here records them all.
        nonlocal attempt
        attempt += 1
        started = time.monotonic()
        response: httpx.Response | None = None
        try:
            response = client.send(built)
            response.raise_for_status()
            raw: dict[str, Any] = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Record failures we expect:
            # HTTP errors and issues converting the JSON.
            # Everything else just raises straight away
            # (because it's not something we expect or want to record).
            _record(
                attempt_number=attempt,
                success=False,
                response_code=response.status_code if response is not None else None,
                error=exc,
                num_results=None,
                seconds=time.monotonic() - started,
            )
            raise
        _record(
            attempt_number=attempt,
            success=True,
            response_code=response.status_code,
            error=None,
            num_results=_result_count_or_none(read_n_matches, raw),
            seconds=time.monotonic() - started,
        )
        return raw

    try:
        return api.retrying(_attempt)
    except (httpx.HTTPError, ValueError) as exc:
        raise SearchAPIRequestError(api.host) from exc


class CouldNotSearchError(RuntimeError):
    """
    Raised when one API gives us no results for a search

    This is deliberately vague.
    Generally, a subclass of this error provides more specific detail e.g.
    [CouldNotGetSearchResponseError][(m).] means the API did not answer
    and [CouldNotUseSearchResultsError][(m).] means it answered
    with something we could not read.
    """

    def __init__(
        self, message: str, facade_key: FacadeKey, cause: Exception | None
    ) -> None:
        """
        Initialise the error

        Parameters
        ----------
        message
            The message which says why we have no results

        facade_key
            The facade which gave us no results

        cause
            What went wrong, if we know it, kept on `cause` for callers to inspect
        """
        super().__init__(message)
        self.facade_key = facade_key
        self.cause = cause


class CouldNotGetSearchResponseError(CouldNotSearchError):
    """
    Raised when an API does not answer a search
    """

    def __init__(self, facade_key: FacadeKey, cause: Exception | None = None) -> None:
        """
        Initialise the error

        Parameters
        ----------
        facade_key
            The facade which did not answer

        cause
            What went wrong, if we know it

            Folded into the message so that the failure can say *why*,
            and kept on `cause` for callers to inspect.
        """
        why = "" if cause is None else f" ({cause})"
        super().__init__(
            f"{facade_key[0]} did not answer our search request{why}, "
            "so it has given us no results.",
            facade_key=facade_key,
            cause=cause,
        )


class CouldNotUseSearchResultsError(CouldNotSearchError):
    """
    Raised when an API answers a search with something we cannot read

    Unlike [CouldNotGetSearchResponseError][(m).], the host did answer:
    what failed was our reading of it, so the cause is an
    [UnreadableResponseError][esmporium.search.apis.UnreadableResponseError]
    saying which key we could not find rather than a transport failure.
    """

    def __init__(
        self,
        facade_key: FacadeKey,
        cause: Exception,
        url: str | None = None,
    ) -> None:
        """
        Initialise the error

        Parameters
        ----------
        facade_key
            The facade whose answer we could not read

        cause
            What we could not read, kept on `cause` for callers to inspect

        url
            The URL we asked, if we know it

            Folded into the message so a report of this carries
            what someone would need to ask the same question again.
        """
        super().__init__(
            f"{facade_key} answered our search request "
            "with something we could not read "
            f"({cause}), so it has given us no results."
            f"{f' We asked: {url}.' if url else ''}",
            facade_key=facade_key,
            cause=cause,
        )
        self.url = url


class NoFacadeAnsweredError(RuntimeError):
    """
    Raised when every facade we asked failed to give us anything we could use

    "Failed" covers a facade not being able to handle the query,
    an API not answering and an API answering with something we could not read:
    `failures` says which it was for each API.

    Should carry all of the failures rather than only the last,
    because which APIs failed, and how, is the interesting part:
    one node being down says nothing, all of them being down says a lot,
    and only the whole list tells you which it was.
    """

    def __init__(
        self,
        failures: dict[FacadeKey, CouldNotSearchError]
        | dict[FacadeKey, CouldNotGetAllowedValuesError],
    ) -> None:
        """
        Initialise the error

        Parameters
        ----------
        failures
            Why each facade gave us nothing we could use, keyed by the facade key
        """
        self.failures = failures
        asked = "\n".join(
            f"  - {facade_key}: {failure}" for facade_key, failure in failures.items()
        )
        noun = "facade" if len(failures) == 1 else "facades"
        super().__init__(
            f"Asked {len(failures)} {noun} and none of them gave us anything "
            f"we could use:\n{asked}"
        )


class AllSubQueriesFailedError(RuntimeError):
    """
    Raised when every sub-query of a multi-query search or value check failed
    """

    def __init__(self, failures: tuple[NoFacadeAnsweredError, ...]) -> None:
        """
        Initialise the error

        Parameters
        ----------
        failures
            Each failed sub-query's error, in the order the sub-queries ran
        """
        self.failures = failures
        joined = "\n".join(f"  - {failure}" for failure in failures)
        super().__init__(
            f"All {len(failures)} sub-queries failed to return anything we could use:\n"
            f"{joined}"
        )


class ClashingFacetsForFacadeError(CouldNotSearchError):
    """
    Raised when `other_terms` sets values for facets that a query already sets

    Unlike [ClashingFacetsError][(m).], this also gives context about the original query
    and facade being used
    (because the facade decides
    how the original query's facet names are translated to API names).

    other_terms is the escape hatch for facets we do not model,
    so a name in other_terms which lands on a facet the query already sets is ambiguous:
    there is no way to tell which value should win, so we refuse to guess.

    This sub-classes [CouldNotSearchError][(m).]
    because clashing facets prevent us from constructing a request
    and therefore from searching,
    but a query which clashes for one facade can be perfectly askable for another
    so we want this to fall under the same 'banner'.
    """

    def __init__(
        self,
        query: QueryProtocol,
        facade: SearchAPIFacade,
        clashing: Collection[str],
    ) -> None:
        """
        Initialise the error

        Parameters
        ----------
        query
            The original query

        facade
            The facade being used

        clashing
            The facet names that clash,
            once `query` has been translated into the API names required by `facade`.
        """
        self.query = query
        self.facade = facade
        self.clashing = tuple(sorted(clashing))

        clashing_api_names = readable_list(self.clashing)
        noun = "facet" if len(self.clashing) == 1 else "facets"
        conjugation = "clashes" if len(self.clashing) == 1 else "clash"

        # Figure out the canonical name the clashes map to, if any
        spec_facade_parameters = facet_spec(facade.parameters.base_query_style)
        api_name_to_canonical_map = {
            v: k
            for k, v in facade.parameters.get_mapping_to_api_facet_names(
                {
                    *spec_facade_parameters.facet_names,
                    *spec_facade_parameters.canonical_to_native,
                }
            ).items()
        }

        clash_to_canonical_map = {
            clash: api_name_to_canonical_map[clash]
            for clash in self.clashing
            if clash in api_name_to_canonical_map
        }

        # Figure out the query name each clash maps to
        spec_query = facet_spec(type(query))
        clash_to_query_map_canonical = {
            clash: spec_query.canonical_to_native[maybe_canonical]
            for clash, maybe_canonical in clash_to_canonical_map.items()
            if maybe_canonical not in spec_query.query_specific_facets
        }
        clash_to_query_map_query_specific = {
            clash: maybe_query_specific
            for clash, maybe_query_specific in clash_to_canonical_map.items()
            if maybe_query_specific in spec_query.query_specific_facets
            # Don't need to check spec_facade_parameters,
            # the facet would have caused an error already there
            # if it wasn't supported by spec_facade_parameters.
        }

        # The two are keyed by different kinds of facet, so neither can shadow
        # the other. Anything the query does not set (e.g. a facet the facade
        # sets itself) is already gone: it never made it into the maps.
        clash_to_query_map = {
            **clash_to_query_map_canonical,
            **clash_to_query_map_query_specific,
        }

        # A clash we could not map back is a facet the facade puts in the request
        # itself (STAC's `collection`, say, which it works out from the project)
        # rather than one the query names, so there is no query name to offer
        # as the alternative to setting it in `other_terms`.
        mapped = tuple(clash for clash in self.clashing if clash in clash_to_query_map)
        unmapped = tuple(
            clash for clash in self.clashing if clash not in clash_to_query_map
        )

        # Create our relevant map from query terms to API terms,
        # in the order the clashes are reported in
        query_to_clash_map_relevant = {
            clash_to_query_map[clash]: clash for clash in mapped
        }
        clashing_query_names = readable_list(tuple(query_to_clash_map_relevant))

        # Store key results
        self.query_to_api_map_relevant = query_to_clash_map_relevant

        advice = ""
        if mapped:
            advice += (
                f"The relevant mapping from query names to API names is: "
                f"{query_to_clash_map_relevant}. "
                f"Either set {clashing_query_names} via the query "
                f"or set {readable_list(mapped)} via `other_terms`, don't do both. "
            )

        if unmapped:
            verb = "is" if len(unmapped) == 1 else "are"
            pronoun = "it" if len(unmapped) == 1 else "they"
            advice += (
                f"{readable_list(unmapped)} {verb} set by the facade itself "
                "rather than by a facet of the query, "
                f"so {pronoun} cannot be set via `other_terms`. "
            )

        msg = (
            f"`other_terms` {noun} {clashing_api_names} {conjugation} "
            "with the query's facet names, "
            "once the query's facet names are translated to the API's names. "
            f"{advice}"
            f"{query=}"
        )
        super().__init__(msg, facade_key=get_facade_key(facade), cause=None)


class PaginationLimitError(RuntimeError):
    """
    Raised when paging through a search hits a safety limit before it is exhausted

    This is a guardrail, not a normal outcome. It fires either because a search
    collected more records than the caller allowed (`max_results`), or because an
    endpoint asked us to re-request a page we had already requested, which would
    otherwise loop forever.
    """

    def __init__(self, facade_key: FacadeKey, message: str) -> None:
        """
        Initialise the error

        Parameters
        ----------
        facade_key
            The facade we were paging through

        message
            Why we stopped, phrased to follow on from "while paging results from
            <facade>: "
        """
        self.facade_key = facade_key
        super().__init__(f"While paging results from {facade_key}: {message}")


# Note for developers: A [UserWarning][] so it shows by default,
# and its own category so it is easy to silence on its own: filter it with
# `warnings.simplefilter("ignore", PaginationWarning)`,
# or escalate it to an error in tests, without touching any other warning.
class PaginationWarning(UserWarning):
    """
    Warns that a search is large enough that it will page through several requests

    Emitted (unless `warn_on_pagination` is turned off) when an endpoint reports more
    matches than fit in one page, so the caller knows the search will make several
    requests and may take a while before it is done.
    """


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


@dataclass(frozen=True)
class FacadePages:
    """The outcome of paging through one facade's answer to a search"""

    collected: tuple[ParsedDocument, ...]
    """Every parsed document we fetched, across all pages"""

    n_matches: int | None
    """The total the endpoint reported matched, read from the first page"""

    completed: bool
    """Whether we fetched every page (`False` if a page failed part way)"""

    failure: CouldNotSearchError | None
    """The failure that stopped us, if paging did not complete"""


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
        will take, for the [PaginationWarning][(m).].

    max_results
        The most records to collect before stopping with a
        [PaginationLimitError][(m).]. `None` disables the cap.

    warn_on_pagination
        Whether to emit a [PaginationWarning][(m).] when the endpoint reports more
        matches than fit in one page, so the caller knows paging is happening.

    api_call_observer
        Told about each further request, as in [fire][(m).]

    processor
        Handed each page as it is parsed, as in [search][(m).]

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
    api = facade.search_api
    facade_key = get_facade_key(facade)
    collected: list[ParsedDocument] = []
    # Stays None if the first fetch fails before we can read a count.
    n_matches: int | None = None
    seen: set[str] = set()
    try:
        fire_here = partial(
            fire,
            client=client,
            api=api,
            api_call_observer=api_call_observer,
            read_n_matches=facade.get_n_matches,
        )
        raw = fire_here(request=request)
        # Read the count directly: a search response
        # that omits its total is one we cannot use, so let the raise propagate to the
        # UnreadableResponseError handler below rather than swallowing it to None.
        n_matches = facade.get_n_matches(raw)
        if warn_on_pagination:
            _warn_if_paginating(facade_key[0], n_matches=n_matches, limit=limit)
        seen = {_request_fingerprint(request)}

        while True:
            parsed = facade.parse_search_results(raw)
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
        return FacadePages(
            tuple(collected),
            n_matches,
            completed=False,
            failure=CouldNotGetSearchResponseError(facade_key, cause=exc),
        )
    except UnreadableResponseError as exc:
        return FacadePages(
            tuple(collected),
            n_matches,
            completed=False,
            failure=CouldNotUseSearchResultsError(
                facade_key, cause=exc, url=get_url(api, request)
            ),
        )

    # Paged to the end. If the endpoint reported a total, note when what we collected
    # falls short of it (most likely the index shifted mid-scan): the pages we did get
    # are still all we could follow, so this is a heads-up, not a failure.
    if n_matches is not None and len(collected) != n_matches:
        logger.debug(
            "%s paged to the end but collected %d of the %d records it reported "
            "matched (the index may have shifted mid-scan)",
            facade_key[0],
            len(collected),
            n_matches,
        )

    return FacadePages(tuple(collected), n_matches, completed=True, failure=None)


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
        [PaginationLimitError][(m).].

        This is an optional safety guardrail against a runaway search over a huge
        result set: set it to a number to cap how much one endpoint may return.
        It defaults to `None` (no cap), so a search fetches every matching record
        unless you ask it not to.

        Turning this off does not remove the loop protection: an endpoint that asks
        us to re-request a page we already fetched still stops with a
        [PaginationLimitError][(m).], because that would otherwise page forever.

    warn_on_pagination
        Whether to warn (with a [PaginationWarning][(m).]) when a search matches more
        records than fit in one page, so you know it will make several requests and
        may take a while.

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
