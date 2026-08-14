"""
Sending a single search request to a node and timing it.

[`search_once`][esmporium.esgf.search.client.search_once] takes a node, a built
[`SearchRequest`][esmporium.esgf.search.request.SearchRequest], and that node's
config, sends the request with httpx, and returns a
[`CallResult`][esmporium.esgf.search.client.CallResult] — never raising for a node
failure, so one dead node cannot sink a whole fan-out.

Retries here are *same-endpoint* retries on a transient failure (a network error or
a ``5xx``): a ``4xx`` is returned as-is (retrying will not help), and a ``2xx`` is
parsed and returned. Advancing to a *different* endpoint on a no-result is the
orchestrator's job, not this function's.
"""

from time import perf_counter
from typing import Any

import httpx
from pydantic import BaseModel

from esmporium.esgf.search.generation import GenerationConfig
from esmporium.esgf.search.hosts import IndexNode
from esmporium.esgf.search.request import SearchRequest

# HTTP statuses at or above this are treated as transient and retried.
_SERVER_ERROR = 500
_OK = 200


class CallResult(BaseModel):
    """
    The outcome of one search-API call: how it went, and what came back.

    Holds the raw response payload (``data``) untouched — parsing it into datasets
    is a later step — plus enough to record the call's performance.
    """

    model_config = {"frozen": True}

    ok: bool
    """Whether we got a usable 2xx response."""
    elapsed_seconds: float
    """How long the (final) attempt took."""
    status_code: int | None = None
    """The HTTP status of the final attempt, or ``None`` on a network error."""
    num_matched: int | None = None
    """How many datasets the node reported matched, if the payload said."""
    data: dict[str, Any] | None = None
    """The raw JSON response payload, as returned; ``None`` if the call failed."""
    error: str | None = None
    """A short description of the failure, if any."""

    @property
    def has_results(self) -> bool:
        """Whether the call succeeded and matched at least one dataset."""
        return self.ok and (self.num_matched or 0) > 0


def _extract_num_matched(payload: Any) -> int | None:
    """Read the matched-dataset count across Solr and both STAC spellings."""
    if not isinstance(payload, dict):
        return None
    for key in ("numberMatched", "numMatched"):
        if key in payload:
            return int(payload[key])
    context = payload.get("context")
    if isinstance(context, dict) and "matched" in context:
        return int(context["matched"])
    response = payload.get("response")
    if isinstance(response, dict) and "numFound" in response:
        return int(response["numFound"])
    return None


def search_once(
    client: httpx.Client,
    node: IndexNode,
    request: SearchRequest,
    config: GenerationConfig,
    *,
    retries: int = 0,
) -> CallResult:
    """
    Send one search request to a node, retrying the same node on transient failure.

    Parameters
    ----------
    client
        The httpx client to send with (its timeout governs the call).

    node
        The node to search; its host builds the URL via ``config``.

    request
        The request to send (its ``method`` selects GET params vs POST json).

    config
        The node's generation config, used to build the search URL.

    retries
        How many extra attempts to make against this same node on a transient
        failure (network error or ``5xx``). ``0`` means a single attempt.

    Returns
    -------
    :
        A [`CallResult`][esmporium.esgf.search.client.CallResult]; failures are
        reported in it, never raised.
    """
    url = config.search_url(node.host)
    error = "no attempt made"
    status: int | None = None
    elapsed = 0.0
    for _ in range(retries + 1):
        start = perf_counter()
        try:
            if request.method == "POST":
                response = client.post(url, json=request.json_body)
            else:
                response = client.get(url, params=request.params)
        except httpx.RequestError as exc:
            elapsed, status, error = (
                perf_counter() - start,
                None,
                f"request error: {exc}",
            )
            continue
        elapsed, status = perf_counter() - start, response.status_code
        if response.status_code >= _SERVER_ERROR:
            error = f"server error {response.status_code}"
            continue
        if response.status_code != _OK:
            return CallResult(
                ok=False,
                elapsed_seconds=elapsed,
                status_code=response.status_code,
                error=f"HTTP {response.status_code}: {response.text[:200]}",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            return CallResult(
                ok=False,
                elapsed_seconds=elapsed,
                status_code=response.status_code,
                error=f"invalid JSON: {exc}",
            )
        return CallResult(
            ok=True,
            elapsed_seconds=elapsed,
            status_code=response.status_code,
            num_matched=_extract_num_matched(payload),
            data=payload if isinstance(payload, dict) else None,
        )
    return CallResult(
        ok=False, elapsed_seconds=elapsed, status_code=status, error=error
    )
