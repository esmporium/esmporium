"""
Observing the health of the search APIs

Every live request to a search API goes through
[fire][esmporium.search.search.fire], whatever it was driven by.
That is the one place where the request, the response code, any error, the number
of results and the time taken all exist together, so it is where we observe a call.

Observing is opt-in and deliberately use case agnostic.
`fire` builds a plain [SearchApiCall][(m).] describing what happened
and hands it to a [SearchApiCallObserver][(m).] if one was given.
What to *do* with that record is the observer's choice:
the database layer provides an observer which records it
(see [record_search_api_calls][esmporium.db.health.record_search_api_calls]),
but an observer is just a function, so a caller can print it, collect it,
or send it somewhere else instead.

Note that we define a `request` as the search we send, and a `call`
as the request sent to a single host. A single request can result in several
calls to a host (retrying policy).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class SearchAPICall:
    """
    Health information saved per search API per call

    """

    host: str
    """See [SearchAPICallRecord.host][esmporium.db.schema.SearchAPICallRecord.host]."""

    http_method: str
    """See [SearchAPICallRecord.http_method][esmporium.db.schema.SearchAPICallRecord.http_method]."""  # noqa: E501

    url: str
    """See [SearchAPICallRecord.url][esmporium.db.schema.SearchAPICallRecord.url]."""

    request_body: str | None
    """See [SearchAPICallRecord.request_body][esmporium.db.schema.SearchAPICallRecord.request_body]."""  # noqa: E501

    response_code: int | None
    """See [SearchAPICallRecord.response_code][esmporium.db.schema.SearchAPICallRecord.response_code]."""  # noqa: E501

    success: bool
    """See [SearchAPICallRecord.success][esmporium.db.schema.SearchAPICallRecord.success]."""  # noqa: E501

    error: Exception | None
    """
    The exception that made the attempt fail, or `None` on success

    Kept as the live exception so an observer can
    inspect its type or cause. The database stores a string instead; that
    translation happens in
    [SearchAPICallRecord.from_call][esmporium.db.schema.SearchAPICallRecord.from_call].
    """

    num_results: int | None
    """See [SearchAPICallRecord.num_results][esmporium.db.schema.SearchAPICallRecord.num_results]."""  # noqa: E501

    response_time_seconds: float
    """See [SearchAPICallRecord.response_time_seconds][esmporium.db.schema.SearchAPICallRecord.response_time_seconds]."""  # noqa: E501

    attempt_number: int
    """See [SearchAPICallRecord.attempt_number][esmporium.db.schema.SearchAPICallRecord.attempt_number]."""  # noqa: E501


SearchAPICallObserver = Callable[[SearchAPICall], None]
"""
Something told about each search API call as it happens
"""


def fan_out(*observers: SearchAPICallObserver) -> SearchAPICallObserver:
    """
    Turn several observers into one that calls each in turn

    This is how more than one observer is supported:
    [fire][esmporium.search.search.fire] still calls a single observer,
    and `fan_out` composes many into that one
    (e.g. record to the database *and* print to the console).

    Parameters
    ----------
    observers
        The observers to call, in order

    Returns
    -------
    :
        A single observer which calls each of `observers`

        With no arguments this is a valid no-op observer.
    """

    def observer(call: SearchAPICall) -> None:
        for obs in observers:
            obs(call)

    return observer
