"""
Observing the health of the search APIs

Every live request to a search API goes through
[fire][esmporium.search.search.fire], whether it was driven by
[search][esmporium.search.search.search] or by
[check_query_values][esmporium.search.check_query_values.check_query_values].
That is the one place where the request, the response code, any error, the number
of results and the time taken all exist together, so it is where we observe a call.

Observing is opt-in and deliberately DB-agnostic.
`fire` builds a plain [SearchApiCall][(m).] describing what happened
and hands it to a [SearchApiCallObserver][(m).] if one was given.
What to *do* with that record is the observer's choice:
the database layer provides an observer which records it
(see [record_search_api_calls][esmporium.db.health.record_search_api_calls]),
but an observer is just a function, so a caller can print it, collect it,
or send it somewhere else instead. Nothing here imports the database:
the search layer emits a fact, and the database layer alone turns it into a row.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class SearchApiCall:
    """
    What happened on one request to one search API

    One of these is built per call to [fire][esmporium.search.search.fire],
    i.e. per logical request to one host (after that host's retries),
    on both the success and the failure path.
    """

    host: str
    """The host the request went to, e.g. `esgf.nci.org.au`"""

    http_method: str
    """The HTTP method used, e.g. `GET` or `POST`"""

    url: str
    """
    The full URL the request went to, including any query string

    For the Solr-shaped APIs the facets ride in the query string,
    so this captures them; for the STAC APIs the query is in the body instead
    (see [request_body][(c).request_body]).
    """

    request_body: str | None
    """
    The request body that was sent, if any

    The STAC APIs carry the query as a JSON body (a `POST`);
    the Solr-shaped APIs put everything in the URL and send no body,
    in which case this is `None`.
    """

    response_code: int | None
    """
    The HTTP status code the host answered with

    `None` when nothing answered at all,
    e.g. a transport error or a timeout,
    which is how "the host said no" is told apart from "the host never spoke".
    """

    success: bool
    """Whether we got a usable answer back"""

    error: str | None
    """
    The failure's message, or `None` on success

    This is whatever the underlying exception said,
    so a later reader can see *why* a call failed without re-running it.
    """

    num_results: int | None
    """
    The number of records the host reported matched, if it reported one

    `None` when the response carries no count we can read
    (a search response reports its total; the STAC facet-values response,
    which is a collection document, does not).
    """

    response_time_seconds: float
    """How long the call took, in wall-clock seconds"""


SearchApiCallObserver = Callable[[SearchApiCall], None]
"""
Something told about each search API call as it happens

Given the [SearchApiCall][(m).] describing one call, do something with it
(record it, print it, collect it). Called once per call to
[fire][esmporium.search.search.fire] when one is supplied.
"""


def fan_out(*observers: SearchApiCallObserver) -> SearchApiCallObserver:
    """
    Turn several observers into one that calls each in turn

    This is how more than one observer is supported:
    [fire][esmporium.search.search.fire] still calls a single observer,
    and `fan_out` composes many into that one
    (e.g. record to the database *and* print to the console).

    The observers are called in the order given,
    and an exception from one is allowed to propagate:
    a failure to record a call is not something to swallow.

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

    def observer(call: SearchApiCall) -> None:
        for obs in observers:
            obs(call)

    return observer
