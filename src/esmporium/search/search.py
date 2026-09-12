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
    UnreadableResponseError,
)
from esmporium.search.health import SearchAPICall, SearchAPICallObserver
from esmporium.search.result_parsing import ParsedDocument, ResultProcessor
from esmporium.search.search_api_facade import (
    DEFAULT_SELECTOR,
    NMatchesReader,
    SearchAPIFacadeSelector,
    SelectorOfferedNoAPIFacadeError,
)

logger = logging.getLogger(__name__)


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


class CouldNotUseSearchResultsError(CouldNotSearchError):
    """
    Raised when an API answers a search with something we cannot read

    A [CouldNotSearchError][(m).],
    because from the caller's point of view the outcome is the same:
    this host has given us no results.
    A distinct kind, because the host did answer:
    what failed was our reading of it, so the cause is an
    [UnreadableResponseError][esmporium.search.apis.UnreadableResponseError]
    saying which key we could not find rather than a transport failure.
    """

    def __init__(
        self,
        host: str,
        cause: Exception,
        url: str | None = None,
    ) -> None:
        """
        Initialise the error

        Parameters
        ----------
        host
            The host whose answer we could not read

        cause
            What we could not read, kept on `cause` for callers to inspect

        url
            The URL we asked, if we know it

            Folded into the message so a report of this carries
            what someone would need to ask the same question again.
        """
        # Deliberately not `super().__init__`: `CouldNotSearchError` says the host
        # did not answer, and this one did.
        RuntimeError.__init__(
            self,
            f"{host} answered our search request with something we could not read "
            f"({cause})"
            f"{f' We asked: {url}.' if url else ''} "
            "So it has given us no results.",
        )
        self.host = host
        self.cause = cause
        self.url = url


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
    What came of a search: the datasets found, how many matched, and who refused

    We deliberately do not carry the raw JSON here. Each host's raw documents are kept
    on the parsed `ParsedDocument.raw_json` (and persisted verbatim by the `db` layer),
    so re-exposing the whole response envelope would be redundant. The one envelope
    value worth keeping, the total number of records that matched, is surfaced
    explicitly as `n_matches`.
    """

    datasets: dict[str, tuple[ParsedDocument, ...]]
    """The parsed documents each endpoint answered with, keyed by host"""

    n_matches: dict[str, int | None]
    """
    How many records each endpoint reported matched the search, keyed by host

    This is the total matched, which can exceed the number of documents returned in one
    page. `None` for an endpoint whose response carried no count we could read.
    """

    refusals: dict[str, CouldNotSearchError]
    """What each endpoint which did not answer said, keyed by host"""


def search(  # noqa: PLR0913 - the keyword-only extras are deliberate injection seams
    query: QueryProtocol,
    selector: SearchAPIFacadeSelector = DEFAULT_SELECTOR,
    *,
    stop_at_first_result: bool = True,
    # Limit handling and pagination will be added in PR2.5
    limit: int = 10_000,
    client: httpx.Client | None = None,
    api_call_observer: SearchAPICallObserver | None = None,
    processor: ResultProcessor | None = None,
) -> SearchOutcome:
    """
    Search the facades the selector yields, and parse their answers into datasets

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

    client
        The HTTP client to search with.
        If `None`, one is built for the call and closed at the end.

    api_call_observer
        Told about each request to each API.

        If `None` (the default), nothing is recorded.
        See [esmporium.search.health][] for how to build one.

    processor
        Called with `(host, parsed_documents)` as soon as each endpoint answers, so its
        results can be acted on (e.g. saved) the moment they arrive rather than at the
        end. If `None` (the default), the parsed documents are still collected into the
        returned outcome, just not handed anywhere. See
        [`esmporium.db.build_result_processor`][] for the database-saving one.

    Returns
    -------
    :
        The datasets each endpoint answered with, how many each reported matched,
        and what each endpoint which did not answer said, all keyed by host

    Raises
    ------
    SelectorOfferedNoAPIFacadeError
        `selector` had no facade to offer for this query at all

    NoAPIWouldAnswerError
        The selector offered at least one endpoint and none of them answered
    """
    canonical = to_canonical(query)

    datasets: dict[str, tuple[ParsedDocument, ...]] = {}
    n_matches: dict[str, int | None] = {}
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
                raw = fire(
                    client,
                    facade.search_api,
                    request,
                    api_call_observer,
                    read_n_matches=facade.get_n_matches,
                )
            except SearchAPIRequestError as exc:
                refusals[host] = CouldNotSearchError(host, cause=exc)
            else:
                # The facade knows this host's format and project, so it turns the raw
                # answer into datasets here, the moment it arrives. Note: if the
                # selector offers the same host twice, the second answer simply replaces
                # the first here. That is wasteful, because we run the query again, but
                # it is not wrong: the answers are for the same query from the same
                # host, so either will do. This way of handling results is only safe
                # because we only handle a single query in this function and our facades
                # only support searching a single project at a time. If either of those
                # assumptions changed, this would break. We will have to be more careful
                # in higher-level functions to do queries over multiple projects (PR3).
                try:
                    parsed = facade.parse_search_results(raw)
                except UnreadableResponseError as exc:
                    refusals[host] = CouldNotUseSearchResultsError(
                        host,
                        cause=exc,
                        url=get_url(facade.search_api, request),
                    )
                else:
                    datasets[host] = parsed
                    n_matches[host] = _result_count_or_none(facade.get_n_matches, raw)
                    if processor is not None:
                        processor(host, parsed)

                    if stop_at_first_result:
                        break

            attempt += 1

    finally:
        if owns_client:
            client.close()

    if not asked_someone:
        raise SelectorOfferedNoAPIFacadeError(canonical, selector)

    if not datasets and refusals:
        raise NoAPIWouldAnswerError(tuple(refusals.values()))

    return SearchOutcome(datasets, n_matches, refusals)
