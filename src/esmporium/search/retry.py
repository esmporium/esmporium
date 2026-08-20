"""
Retry policies for search requests
"""

from __future__ import annotations

import httpx
from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)


def _is_transient(exc: BaseException, transient_status_floor: int = 500) -> bool:
    """
    Decide whether a failure is worth retrying because it is transient

    Parameters
    ----------
    exc
        The exception raised while sending a request

    transient_status_floor
        Floor (i.e. minimum) value for status codes which are transient

    Returns
    -------
    :
        Whether `exc` is a transient failure we should retry
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= transient_status_floor

    return isinstance(exc, httpx.TransportError)


def build_transient_retrying(attempts: int) -> Retrying:
    """
    Build a tenacity retrying policy that retries transient failures with backoff

    Parameters
    ----------
    attempts
        The most times to try in total

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
