"""
High-level search functionality
"""

from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any

import httpx

from esmporium.query import QueryProtocol, to_canonical
from esmporium.search.esgf_generations import DEFAULT_LIMIT, Request
from esmporium.search.search_api import (
    DEFAULT_SELECTOR,
    SearchAPI,
    SearchAPISelector,
    SelectorOfferedNoAPIError,
)

logger = logging.getLogger(__name__)


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


# Rename to _log_request_as_url_and_curl
def _log_request(
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


def fire(
    client: httpx.Client,
    api: SearchAPI,
    request: Request,
) -> dict[str, Any] | None:
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

    Returns
    -------
    :
        The raw JSON the endpoint answered with,
        or `None` if it never answered
        or answered with something we cannot use
    """
    built = client.build_request(
        request.method,
        api.url(request),
        params=request.params,
        json=request.json_body,
        timeout=api.timeout,
    )
    _log_request(api, built)

    def _once() -> dict[str, Any]:
        response = client.send(built)
        response.raise_for_status()
        res: dict[str, Any] = response.json()
        return res

    try:
        return api.retrying(_once)
    except (httpx.HTTPError, ValueError):
        # TODO: raise an error here rather than signalling failure with `None`.
        # `None` cannot say why the API did not answer,
        # and the caller has to remember to check for it,
        # which is exactly the kind of silence we are trying to avoid
        # (see `NoAPIWouldAnswerError`, which can only name the hosts
        # that refused, never what they said).
        # It is also what we would want for logging search API health,
        # because that is the key use case for
        # which we would want this extra information.
        # We will make this change in the PR
        # where we start parsing search results into `Dataset`s,
        # because that is where the parsing failures
        # will want the same treatment.
        return None


class CouldNotSearchError(RuntimeError):
    """
    Raised when one API will not answer a search
    """

    def __init__(self, host: str) -> None:
        """
        Initialise the error

        Parameters
        ----------
        host
            The host which did not answer
        """
        # TODO: carry the underlying cause once `fire` raises
        # rather than returning `None` (see the TODO in `fire`).
        # Until then, this can only say that the host did not answer,
        # never why.
        self.host = host
        super().__init__(
            f"{host} did not answer our search request, so it has given us no results."
        )


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


def search(
    query: QueryProtocol,
    selector: SearchAPISelector = DEFAULT_SELECTOR,
    *,
    stop_at_first_result: bool = True,
    limit: int = DEFAULT_LIMIT,
    client: httpx.Client | None = None,
) -> SearchOutcome:
    """
    Search the endpoints the selector yields, and collect their raw JSON

    Parameters
    ----------
    query
        The query to use for the search

    selector
        Chooses which endpoint to try at each attempt, and when to stop.

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

    Returns
    -------
    :
        What each endpoint answered with,
        and what each endpoint which did not answer said,
        both keyed by host

    Raises
    ------
    SelectorOfferedNoAPIError
        `selector` had no endpoint to offer for this query,
        so there was nobody to search

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
        while (api := selector(canonical, attempt)) is not None:
            asked_someone = True
            request = api.generation.build_search_request(canonical, limit)
            raw = fire(client, api, request)
            if raw is None:
                refusals[api.host] = CouldNotSearchError(api.host)
            else:
                # Note: if the selector offers the same host twice,
                # the second answer simply replaces the first here.
                # That is wasteful, because we run the query again,
                # but it is not wrong: the answers are for the same query
                # from the same host, so either will do.
                results[api.host] = raw
                if stop_at_first_result:
                    break

            attempt += 1

    finally:
        if owns_client:
            client.close()

    if not asked_someone:
        raise SelectorOfferedNoAPIError(canonical, selector)

    if not results and refusals:
        raise NoAPIWouldAnswerError(tuple(refusals.values()))

    return SearchOutcome(results, refusals)
