"""
Retry policies for fetching bytes from a data node
"""

from __future__ import annotations

import random

from tenacity import RetryCallState, Retrying, retry_if_exception, stop_after_attempt

from esmporium.downloads.errors import DataNodeBlockedError, DataNodeFetchError

DEFAULT_ATTEMPTS = 3
"""
How many times to try one route before moving to the next mirror

One try plus two retries.
"""

DEFAULT_BACKOFF_BASE_SECONDS = 1.0
"""The first wait, doubled on each subsequent attempt"""

DEFAULT_BACKOFF_CAP_SECONDS = 60.0
"""
The longest we will wait between attempts
"""

DEFAULT_JITTER = 0.25
"""
How much to randomise each wait, as a fraction of it

Without this, a run which started several downloads at once retries them all at the
same instant, which is how a node that asked us to slow down gets hit by a
synchronised burst.
"""


def is_transient(exc: BaseException) -> bool:
    """
    Decide whether a failed fetch is worth trying again on the same route

    Parameters
    ----------
    exc
        The exception raised while fetching

    Returns
    -------
    :
        Whether `exc` is a failure we should retry against the same data node

    Examples
    --------
    >>> from esmporium.downloads.errors import (
    ...     DataNodeTimeoutError,
    ...     RouteMissingError,
    ... )
    >>> is_transient(DataNodeTimeoutError("https://x/a.nc", "stalled"))
    True
    >>> is_transient(RouteMissingError("https://x/a.nc", "not here"))
    False

    Anything we never classified is not a flaky data node:

    >>> is_transient(ValueError("not a fetch failure at all"))
    False
    """
    if not isinstance(exc, DataNodeFetchError):
        # Anything we did not classify is a bug or an unrelated failure, not a flaky
        # data node. Retrying it would just take three times as long to surface.
        return False

    return exc.outcome.is_worth_retrying


def _wait(state: RetryCallState) -> float:
    """
    Work out how long to wait before the next attempt

    Honours a `Retry-After` from the data node when there is one, and otherwise backs
    off exponentially with jitter.

    Parameters
    ----------
    state
        Tenacity's record of what has happened so far

    Returns
    -------
    :
        How long to wait, in seconds
    """
    exc = state.outcome.exception() if state.outcome is not None else None

    asked_for = getattr(exc, "retry_after_seconds", None)
    if isinstance(exc, DataNodeBlockedError) and asked_for is not None:
        # The node told us how long. Believe it, but do not let it park us
        # indefinitely: a nonsensical `Retry-After: 86400` should not hang a run.
        return min(float(asked_for), DEFAULT_BACKOFF_CAP_SECONDS)

    # `attempt_number` is 1-based, so the first wait is the base.
    delay = min(
        DEFAULT_BACKOFF_CAP_SECONDS,
        DEFAULT_BACKOFF_BASE_SECONDS * (2 ** (state.attempt_number - 1)),
    )

    jitter = random.uniform(0.0, DEFAULT_JITTER)  # noqa: S311 - jitter, not crypto

    return float(delay + jitter * delay)


def build_download_retrying(attempts: int = DEFAULT_ATTEMPTS) -> Retrying:
    """
    Build a tenacity retrying policy for fetching from one data node

    Parameters
    ----------
    attempts
        The most times to try in total, against this one route

    Returns
    -------
    :
        The retry policy

        `reraise=True`, so the original
        [DataNodeFetchError][esmporium.downloads.DataNodeFetchError] escapes rather
        than tenacity's own `RetryError`. That is what lets the caller read the
        outcome off the exception and decide whether to try another mirror, and it
        matches what
        [build_transient_retrying][esmporium.search.build_transient_retrying] does.
    """
    return Retrying(
        stop=stop_after_attempt(attempts),
        wait=_wait,
        retry=retry_if_exception(is_transient),
        reraise=True,
    )
