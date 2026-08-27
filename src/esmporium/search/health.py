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
    """

    num_results: int | None
    """
    The number of records the host reported matched

    `None` when the response carries no count we can read
    (a search response reports its total; the STAC facet-values response,
    which is a collection document, does not).
    """

    response_time_seconds: float
    """How long the request took to return"""


# TODO: this makes sense enough but not enough to clarify
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
