"""
Sending one request to one search API
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from esmporium.http import log_request_as_url_and_curl
from esmporium.search.apis import (
    NoSearchResultNumberOfMatchesReturnedError,
    Request,
    SearchAPI,
)
from esmporium.search.health import SearchAPICall, SearchAPICallObserver
from esmporium.search.search.errors import SearchAPIRequestError

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


def _result_count_or_none(api: SearchAPI, raw: dict[str, Any]) -> int | None:
    """
    Read how many records a response reported, or `None` if it reported none

    A search response reports its total; the STAC facet-values response
    (a collection document) does not, hence the tolerance.

    Parameters
    ----------
    api
        The API which answered, which knows where its format writes the count

    raw
        The response to read

    Returns
    -------
    :
        The number of records reported, or `None` if the response carries no count
    """
    try:
        return api.get_n_matches(raw)
    except NoSearchResultNumberOfMatchesReturnedError:
        return None


def fire(
    client: httpx.Client,
    api: SearchAPI,
    request: Request,
    api_call_observer: SearchAPICallObserver | None = None,
) -> dict[str, Any]:
    """
    Send one request to one API, using that API's retry policy and timeout

    Parameters
    ----------
    client
        The HTTP client to send with

    api
        The API to send to

        This also carries the retry policy and timeout,
        and reads how many records the answer says matched,
        for the observer to record.

    request
        The request to send

    api_call_observer
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
    log_request_as_url_and_curl(
        logger, api.host, built, what="search", host_field="search_api_host"
    )

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
            num_results=_result_count_or_none(api, raw),
            seconds=time.monotonic() - started,
        )
        return raw

    try:
        return api.retrying(_attempt)
    except (httpx.HTTPError, ValueError) as exc:
        raise SearchAPIRequestError(api.host) from exc
