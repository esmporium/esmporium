"""
Retry policy for talking to the live ESGF search APIs

A 5xx from a node means a load-balanced backend is flapping,
so it (and transport errors) are transient and worth retrying;
a 4xx is a real "no" and is not retried.
"""

from __future__ import annotations

import httpx
from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

_TRANSIENT_STATUS_FLOOR = 500
"""At and above this status code the failure is the node's, so we retry it"""


def _is_transient(exc: BaseException) -> bool:
    """
    Decide whether a failure is worth retrying

    Parameters
    ----------
    exc
        The exception raised while sending a request

    Returns
    -------
    :
        Whether `exc` is a transient failure we should retry
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= _TRANSIENT_STATUS_FLOOR
    return isinstance(exc, httpx.TransportError)


def transient_retry(attempts: int) -> Retrying:
    """
    Build a tenacity policy that retries transient failures with backoff

    Parameters
    ----------
    attempts
        The most times to try in total,
        i.e. one initial try plus that many minus one retries

    Returns
    -------
    :
        The retry policy
    """
    return Retrying(
        stop=stop_after_attempt(attempts),
        wait=wait_exponential(multiplier=0.5, max=8),
        retry=retry_if_exception(_is_transient),
        reraise=True,
    )
