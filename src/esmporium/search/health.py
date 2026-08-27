"""
Health record information of search APIs for every search request

Here we define the health information to be saved, including:
success, failure, error message, time taken, number of results
(per host, per request).

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

    error: str | None
    """See [SearchAPICallRecord.error][esmporium.db.schema.SearchAPICallRecord.error]."""  # noqa: E501

    num_results: int | None
    """See [SearchAPICallRecord.num_results][esmporium.db.schema.SearchAPICallRecord.num_results]."""  # noqa: E501

    response_time_seconds: float
    """See [SearchAPICallRecord.response_time_seconds][esmporium.db.schema.SearchAPICallRecord.response_time_seconds]."""  # noqa: E501


# TODO Zeb: this makes sense enough but not enough to clarify
# the docstring
SearchAPICallObserver = Callable[[SearchAPICall], None]
"""
Something told about each search API call as it happens

Given the [SearchAPICall][(m).] describing one call, do something with it
(record it, print it, collect it). Called once per call to
[fire][esmporium.search.search.fire] when one is supplied.
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
