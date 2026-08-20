"""
The search step: fire a query at the ranked endpoints and collect raw JSON

This is deliberately thin.
It ties together the pieces that live elsewhere in this package:
a [selector][esmporium.search.search_api.SearchAPISelector] decides which
[SearchAPI][esmporium.search.search_api.SearchAPI] to try (and in what order),
each API's [generation][esmporium.search.esgf_generations.SearchAPIGeneration]
turns the query into a [Request][esmporium.search.esgf_generations.Request],
and each API's retry policy decides how hard to try before giving up.

Searching is sequential here: one endpoint at a time.
Fanning the requests out in parallel is a later step.

When logging is turned up to `DEBUG`, every request is logged before it is sent,
as both its URL-equivalent and a `curl` command that reproduces it.
The log record also carries the process and thread it was made from
(as the standard library records those on every log record),
so a later, parallel search can be read back per worker.
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

REQUEST_TIMEOUT: float = 30.0
"""
How long to wait on a single request, in seconds

A node which is going to answer answers quickly;
one which does not is better retried or skipped than waited on.
"""


def _curl_equivalent(request: httpx.Request) -> str:
    """
    Render an httpx request as a `curl` command which reproduces it

    The command is built from the request httpx is actually going to send,
    headers, body and all, so it is a faithful reproduction rather than a guess.
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


def _log_request(api: SearchAPI, request: httpx.Request) -> None:
    """
    Log a request we are about to send, at `DEBUG`

    The URL-equivalent and the `curl`-equivalent go into both the (plain text)
    message and the structured fields, so a plain-text log and a structured one
    both carry them. The process and thread are on the record already;
    the standard library puts them there.

    Rendering is skipped entirely unless `DEBUG` is on,
    so the string-building is not paid for when it would only be thrown away.

    Parameters
    ----------
    api
        The endpoint the request is going to

    request
        The request httpx is going to send
    """
    if not logger.isEnabledFor(logging.DEBUG):
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
    client: httpx.Client, api: SearchAPI, request: Request
) -> dict[str, Any] | None:
    """
    Send one request to one endpoint, under that endpoint's retry policy

    A transient failure (a 5xx or a transport error) is retried as far as the
    endpoint's policy allows; a non-transient "no" (a 4xx, or a body we cannot
    read as JSON) is not, because retrying it would only ask the same question
    and get the same answer.

    Parameters
    ----------
    client
        The HTTP client to send with

    api
        The endpoint to send to, carrying the retry policy to send under

    request
        The request to send, as built by the endpoint's generation

    Returns
    -------
    :
        The raw JSON the endpoint answered with,
        or `None` if it never answered (the policy gave up)
        or answered with something we cannot use (a 4xx, or an unreadable body)
    """
    built = client.build_request(
        request.method,
        api.url(request),
        params=request.params,
        json=request.json_body,
        timeout=REQUEST_TIMEOUT,
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
        The facet query, in whichever vocabulary it was written in;
        it is translated to canonical form once, here, and searched with that.

    selector
        Chooses which endpoint to try at each attempt, and when to stop.
        The default ranks endpoints by the query's project.

    stop_at_first_result
        If `True` (the default), return as soon as one endpoint answers.
        The index nodes largely mirror one another, so one good answer is usually
        enough.

        If `False`, work through every endpoint the selector yields and keep each
        one's answer. The nodes do not hold exactly the same data, so the union
        across them is more complete than any single node
        (merging and de-duplicating that union is a later step).

    limit
        The page size to ask each endpoint for,
        i.e. the most records in one response, not the total matched.
        The total comes back in the response itself.

    client
        The HTTP client to search with.
        If none is given, one is built for the call and closed at the end.
        Passing one in is mostly for tests, which drive a client backed by a
        mock transport so they can search without a network.

    Returns
    -------
    :
        The raw JSON each endpoint answered with, keyed by host.
        An endpoint which never answered is left out;
        an endpoint which answered with an empty-but-valid response is kept,
        because "nothing matched" is an answer.

    Raises
    ------
    ValueError
        The selector was asked to rank endpoints for a query
        which does not name exactly one project

    KeyError
        The selector has no list of endpoints for the query's project
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
