"""
Search API implementation

This is low-level, intended to mirror the ESGF search APIs directly.
It is extremely easy to make invalid queries using these pieces.
If you want to make queries, we recommend using the components in
[esmporium.search.search_api_facade][] instead
because of their more robust query creation, result parsing and error handling.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from tenacity import Retrying

from esmporium.query import QueryCanonical
from esmporium.search.esgf_generations import (
    ESGF1Solr,
    ESGF15Bridge,
    ESGFNGStac,
    Request,
    SearchAPIGeneration,
    SolrCMIP5Parameters,
    SolrCMIP6Parameters,
    SolrCMIP7Parameters,
    StacCMIP5Parameters,
    StacCMIP6Parameters,
    StacCMIP7Parameters,
)
from esmporium.search.retry import build_transient_retrying

# TODO:
# - [x] make search/apis/protocol.py etc. so we can split this a bit more clearly
# - [ ] adding all the new search API definitions
# - delete SearchAPIOld
# - move all the pre-built API generation definitions into facade
# - add concrete implementations of different generations into search/apis
# - move pre-built facade instances into search_api_facade
# - move SearchAPISelector and associated errors into search_api_facade and rename
# - add FacetSpecificSearchAPIStore
#   or whatever we are going to call it into search_api_facade
# - review with claude (given changes, what do you think, what tests are missing etc.)


@dataclass(frozen=True)
class SearchAPIOld:
    """
    One search endpoint we can hit
    """

    host: str
    """The host to send requests to, e.g. `esgf.nci.org.au`"""

    generation: SearchAPIGeneration
    """
    The (request/wire) format this host speaks

    This also handles the vocabulary that this API understands
    """

    retrying: Retrying
    """The retry policy to use when hitting this API"""

    timeout: float = 30.0
    """
    How long to wait on a single request to this host, in seconds

    Most hosts reply quickly e.g. 5 seconds.
    The slowest hosts take around 30 seconds.
    In general, you want to make this as short as possible
    because waiting for a reply that will never come
    can kill the speed in your retries.
    """

    scheme: str = "https"
    """
    The URL scheme to reach this host over
    """

    def url(self, request: Request) -> str:
        """
        Build the full URL for a request against this host

        Parameters
        ----------
        request
            The request to make to this host

        Returns
        -------
        :
            The URL to send `request` to
        """
        return f"{self.scheme}://{self.host}{request.path}"


# Pre-built search API generations.
# See note in docstring at the top of the module for why it is like this.
SOLR_CMIP5 = ESGF1Solr(params=SolrCMIP5Parameters)
"""
Search API that hits ESGF1 using SOLR syntax and expects CMIP5 style queries
"""
SOLR_CMIP6 = ESGF1Solr(params=SolrCMIP6Parameters)
"""
Search API that hits ESGF1 using SOLR syntax and expects CMIP6 style queries
"""
SOLR_CMIP7 = ESGF1Solr(params=SolrCMIP7Parameters)
"""
Search API that hits ESGF1 using SOLR syntax and expects CMIP7 style queries
"""

BRIDGE_CMIP5 = ESGF15Bridge(params=SolrCMIP5Parameters)
"""
Search API that hits ESGF1.5 bridge and expects CMIP5 style queries
"""
BRIDGE_CMIP6 = ESGF15Bridge(params=SolrCMIP6Parameters)
"""
Search API that hits ESGF1.5 bridge and expects CMIP5 style queries
"""

STAC_CMIP5 = ESGFNGStac(params=StacCMIP5Parameters)
"""
Search API that hits ESGF-NG STAC API and expects CMIP5 style queries
"""
STAC_CMIP6 = ESGFNGStac(params=StacCMIP6Parameters)
"""
Search API that hits ESGF-NG STAC API and expects CMIP6 style queries
"""
STAC_CMIP7 = ESGFNGStac(params=StacCMIP7Parameters)
"""
Search API that hits ESGF-NG STAC API and expects CMIP7 style queries
"""


SearchAPISelector = Callable[[QueryCanonical, int], SearchAPIOld | None]
"""
Chooses which endpoint to try next

Given the canonical query and a 0-based attempt index, returns the next
[SearchAPI][esmporium.search.search_api.SearchAPI] to try, or `None` to stop.
"""


class SelectorOfferedNoAPIError(ValueError):
    """
    Raised when a selector has no endpoint to offer for a query, from the very start

    Asking for a search is asking for it to happen.
    Handing back an empty answer would read as
    "we asked, and nobody had anything for you",
    when what happened is that nobody was asked at all:
    a selector with an empty list, or one whose rules
    rule out every endpoint for this query, is a bug in the calling code
    and is worth saying out loud rather than quietly returning nothing.

    This is only about having nobody to ask.
    Endpoints which were asked and did not answer are a different thing,
    and are reported as such.
    """

    def __init__(self, canonical: QueryCanonical, selector: SearchAPISelector) -> None:
        """
        Initialise the error

        Parameters
        ----------
        canonical
            The query we were going to ask about

        selector
            The selector which had nothing to offer for it
        """
        self.canonical = canonical
        self.selector = selector
        super().__init__(
            "No API was offered on the very first attempt, "
            f"so there was nobody to ask. The selector was: {selector}. "
            f"The query was: {canonical!r}."
        )


def build_list_selector(apis: Sequence[SearchAPIOld]) -> SearchAPISelector:
    """
    Build a selector that yields the given endpoints in order, then stops

    Every query works through the same list.

    Parameters
    ----------
    apis
        The endpoints to yield, in order

    Returns
    -------
    :
        A selector over `apis`
    """

    def select(canonical: QueryCanonical, attempt: int) -> SearchAPIOld | None:
        return apis[attempt] if attempt < len(apis) else None

    return select


def build_project_list_selector(
    project_lists: Mapping[str, Sequence[SearchAPIOld]],
) -> SearchAPISelector:
    """
    Build a selector that works through a project specific list of endpoints

    Parameters
    ----------
    project_lists
        The endpoints to yield for each project, in order

    Returns
    -------
    :
        A selector which yields APIs in an order specific to the query's project
    """

    def select(canonical: QueryCanonical, attempt: int) -> SearchAPIOld | None:
        """
        Select search API to use

        Parameters
        ----------
        canonical
            Query (in canonical form)

        attempt
            Search attempt

        Returns
        -------
        :
            [SearchAPI][(m).] to use

            If we have run out of APIs to try, we return `None`

        Raises
        ------
        ValueError
            The query specifies a search that is not for exactly one project

        KeyError
            We do not have a list for the input project
        """
        if len(canonical.project) != 1:
            msg = (
                "We can only unambiguously pick the SearchAPI list "
                "if there is exactly one project, "
                f"received: {canonical.project}"
            )
            raise ValueError(msg)

        project = canonical.project[0]

        apis = project_lists[project]

        return apis[attempt] if attempt < len(apis) else None

    return select


CMIP5_APIS: list[SearchAPIOld] = [
    SearchAPIOld("esg-dn1.nsc.liu.se", SOLR_CMIP5, build_transient_retrying(2)),
    SearchAPIOld("esgf.nci.org.au", SOLR_CMIP5, build_transient_retrying(2)),
    SearchAPIOld("esgf-node.ornl.gov", BRIDGE_CMIP5, build_transient_retrying(2)),
    SearchAPIOld("esgf.ceda.ac.uk", SOLR_CMIP5, build_transient_retrying(2)),
    SearchAPIOld("esgf-data.dkrz.de", SOLR_CMIP5, build_transient_retrying(2)),
    SearchAPIOld("search.east.esgf.io", STAC_CMIP5, build_transient_retrying(2)),
    SearchAPIOld("search.west.esgf.io", STAC_CMIP5, build_transient_retrying(2)),
]
"""
Known CMIP5 search APIs

These are sorted in order of greatest to least results,
when we checked.
"""

CMIP6_APIS: list[SearchAPIOld] = [
    SearchAPIOld("esg-dn1.nsc.liu.se", SOLR_CMIP6, build_transient_retrying(2)),
    SearchAPIOld("esgf-node.ornl.gov", BRIDGE_CMIP6, build_transient_retrying(2)),
    SearchAPIOld("esgf.nci.org.au", SOLR_CMIP6, build_transient_retrying(2)),
    SearchAPIOld("esgf.ceda.ac.uk", SOLR_CMIP6, build_transient_retrying(2)),
    SearchAPIOld("esgf-data.dkrz.de", SOLR_CMIP6, build_transient_retrying(2)),
    SearchAPIOld("search.east.esgf.io", STAC_CMIP6, build_transient_retrying(2)),
    SearchAPIOld("search.west.esgf.io", STAC_CMIP6, build_transient_retrying(2)),
]
"""
Known CMIP6 search APIs

These are sorted in order of greatest to least results,
when we checked.
"""

CMIP7_APIS: list[SearchAPIOld] = [
    SearchAPIOld("search.east.esgf.io", STAC_CMIP7, build_transient_retrying(2)),
    SearchAPIOld("search.west.esgf.io", STAC_CMIP7, build_transient_retrying(2)),
    SearchAPIOld("esgf.nci.org.au", SOLR_CMIP7, build_transient_retrying(2)),
    SearchAPIOld("esgf-data.dkrz.de", SOLR_CMIP7, build_transient_retrying(2)),
]
"""
Known CMIP7 search APIs

These are sorted in order of greatest to least results,
when we checked.
"""

DEFAULT_SEARCH_APIS_BY_PROJECT: Mapping[str, Sequence[SearchAPIOld]] = {
    "CMIP5": CMIP5_APIS,
    "CMIP6": CMIP6_APIS,
    "CMIP7": CMIP7_APIS,
}
"""
Default search APIs to use, grouped by project
"""

DEFAULT_SELECTOR = build_project_list_selector(DEFAULT_SEARCH_APIS_BY_PROJECT)
"""The selector used when the caller does not choose one"""
