"""
High-level search functionality
"""

from __future__ import annotations

import json
import logging
import shlex
import time
from dataclasses import dataclass
from typing import Any

import httpx

from esmporium.query import QueryProtocol, to_canonical
from esmporium.search.apis import (
    NoSearchResultNumberOfMatchesReturnedError,
    Request,
    SearchAPI,
)
from esmporium.search.health import SearchAPICall, SearchAPICallObserver
from esmporium.search.search_api_facade import (
    DEFAULT_SELECTOR,
    SearchAPIFacadeSelector,
    SelectorOfferedNoAPIFacadeError,
)

logger = logging.getLogger(__name__)

DEFAULT_LIMIT: int = 10_000
"""
The page size we ask each endpoint for if the caller does not choose one

This is the maximum number of records in one response, not the total number of
matches (which comes back in the response itself). Each search API enforces its
own accepted range and will refuse a page size outside it.
"""


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


# TODO: make these public
def _curl_equivalent(request: httpx.Request) -> str:
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


# TODO: make these public
def _log_request_as_url_and_curl(
    api: SearchAPI,
    request: httpx.Request,
    # Make the level to log at a parameter, rather than being hard-coded
) -> None:
    """
    Log a request we are about to send, at `DEBUG`

    Parameters
    ----------
    api
        The endpoint the request is going to

    request
        The request
    """
    if not logger.isEnabledFor(logging.DEBUG):
        # Skip rendering if the logger is not enabled for the given level
        return

    url = str(request.url)
    curl = _curl_equivalent(request)
    logger.debug(
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


def _result_count_or_none(api: SearchAPI, raw: dict[str, Any]) -> int | None:
    """
    Read how many records a response reported, or `None` if it reported none

    A search response reports its total; the STAC facet-values response
    (a collection document) does not, hence the tolerance.

    Parameters
    ----------
    api
        The API the response came from (its generation knows how to read the count)

    raw
        The response to read

    Returns
    -------
    :
        The number of records reported, or `None` if the response carries no count
    """
    try:
        return api.get_search_result_n_matches(raw)
    except NoSearchResultNumberOfMatchesReturnedError:
        return None


def fire(
    client: httpx.Client,
    api: SearchAPI,
    request: Request,
    observer: SearchAPICallObserver | None = None,
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

    observer
        Told about this call once it is done, on both the success and failure path.
        If `None` (the default), nothing is recorded.
        See [esmporium.search.health][] for how to build one.

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
    _log_request_as_url_and_curl(api, built)

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
        if observer is None:
            return
        observer(
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
            num_results=_result_count_or_none(api, raw),
            seconds=time.monotonic() - started,
        )
        return raw

    try:
        return api.retrying(_attempt)
    except (httpx.HTTPError, ValueError) as exc:
        raise SearchAPIRequestError(api.host) from exc


class CouldNotSearchError(RuntimeError):
    """
    Raised when one API will not answer a search
    """

    def __init__(self, host: str, cause: Exception | None = None) -> None:
        """
        Initialise the error

        Parameters
        ----------
        host
            The host which did not answer

        cause
            What went wrong, if we know it. Folded into the message so that a
            refusal can say *why*, and kept on `cause` for callers to inspect
        """
        self.host = host
        self.cause = cause
        if cause is None:
            message = (
                f"{host} did not answer our search request, "
                "so it has given us no results."
            )
        else:
            message = (
                f"{host} did not answer our search request ({cause}), "
                "so it has given us no results."
            )
        super().__init__(message)


class NoAPIWouldAnswerError(RuntimeError):
    """
    Raised when every API we searched refused to answer
    """

    def __init__(self, refusals: tuple[CouldNotSearchError, ...]) -> None:
        """
        Initialise the error

        Parameters
        ----------
        refusals
            What each API said, in the order they were asked
        """
        self.refusals = refusals
        self.hosts = tuple(refusal.host for refusal in refusals)
        asked = "\n".join(f"  - {refusal}" for refusal in refusals)
        super().__init__(
            f"Searched {len(refusals)} API(s) and none of them answered, "
            f"so we have no results to give you:\n{asked}"
        )


@dataclass(frozen=True)
class SearchOutcome:
    """
    What came of a search: the endpoints which answered, and those which did not
    """

    results: dict[str, Any]
    """The raw JSON each endpoint answered with, keyed by host"""

    refusals: dict[str, CouldNotSearchError]
    """What each endpoint which did not answer said, keyed by host"""


def search(  # noqa: PLR0913 - the keyword-only extras are deliberate injection seams
    query: QueryProtocol,
    selector: SearchAPIFacadeSelector = DEFAULT_SELECTOR,
    *,
    stop_at_first_result: bool = True,
    limit: int = DEFAULT_LIMIT,
    client: httpx.Client | None = None,
    observer: SearchAPICallObserver | None = None,
) -> SearchOutcome:
    """
    Search the facades the selector yields, and collect their raw JSON

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
        i.e. the most records in one response, not the total matched.
        The total comes back in the response itself.

    client
        The HTTP client to search with.
        If `None`, one is built for the call and closed at the end.

    observer
        Told about each request to each API.

        If `None` (the default), nothing is recorded.
        See [esmporium.search.health][] for how to build one.

    Returns
    -------
    :
        What each endpoint answered with,
        and what each endpoint which did not answer said,
        both keyed by host

    Raises
    ------
    SelectorOfferedNoAPIFacadeError
        `selector` had no facade to offer for this query at all

    NoAPIWouldAnswerError
        The selector offered at least one endpoint and none of them answered
    """
    canonical = to_canonical(query)

    results: dict[str, Any] = {}
    refusals: dict[str, CouldNotSearchError] = {}

    owns_client = client is None
    client = client if client is not None else httpx.Client(follow_redirects=True)

    asked_someone = False

    try:
        attempt = 0
        while (facade := selector(canonical, attempt)) is not None:
            asked_someone = True
            request = facade.build_search_request(canonical, limit)
            host = facade.search_api.host
            try:
                raw = fire(client, facade.search_api, request, observer)
            except SearchAPIRequestError as exc:
                refusals[host] = CouldNotSearchError(host, cause=exc)
            else:
                # Note: if the selector offers the same host twice,
                # the second answer simply replaces the first here.
                # That is wasteful, because we run the query again,
                # but it is not wrong: the answers are for the same query
                # from the same host, so either will do.
                results[host] = raw
                if stop_at_first_result:
                    break

            attempt += 1

    finally:
        if owns_client:
            client.close()

    if not asked_someone:
        raise SelectorOfferedNoAPIFacadeError(canonical, selector)

    if not results and refusals:
        raise NoAPIWouldAnswerError(tuple(refusals.values()))

    return SearchOutcome(results, refusals)
