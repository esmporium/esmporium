"""
High-level search functionality
"""

from __future__ import annotations

import logging
import shlex
from typing import Any

import httpx

from esmporium.query import QueryProtocol, to_canonical
from esmporium.search.esgf_generations import DEFAULT_LIMIT, Request
from esmporium.search.search_api import DEFAULT_SELECTOR, SearchAPI, SearchAPISelector

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
        # TODO: is it worth returning more information than just `None`
        # in the case of failure?
        # We can also leave this until we start logging search API health
        # because that is the key use case for
        # which we would want this extra information
        # (so maybe is the right point at which to make a change).
        return None


def search(
    query: QueryProtocol,
    selector: SearchAPISelector = DEFAULT_SELECTOR,
    *,
    stop_at_first_result: bool = True,
    limit: int = DEFAULT_LIMIT,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
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
        The raw JSON each endpoint answered with, keyed by host.
        An endpoint which never answered is left out.
    """
    canonical = to_canonical(query)
    results: dict[str, Any] = {}

    owns_client = client is None
    client = client if client is not None else httpx.Client(follow_redirects=True)

    try:
        attempt = 0
        while (api := selector(canonical, attempt)) is not None:
            request = api.generation.build_search_request(canonical, limit)
            raw = fire(client, api, request)
            if raw is not None:
                results[api.host] = raw
                if stop_at_first_result:
                    break

            attempt += 1

    finally:
        if owns_client:
            client.close()

    return results
