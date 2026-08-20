"""
Search API implementation

This contains our search API interface,
the pre-built search APIs we know how to talk to
and pre-built options for how to pick between them.

Note that, in our implementaiton, generation objects are tightly coupled to projects
which is why there is e.g. [SOLR_CMIP5][(m).] and [SOLR_CMIP6][(m).],
rather than just a single SOLR generation instance.
This choice is made so that error handling and reporting is much simpler,
but costs extra requests if we want to search more than one project.
This is a tradeoff we are ok making.
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


@dataclass(frozen=True)
class SearchAPI:
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
SOLR_CMIP6 = ESGF1Solr(params=SolrCMIP6Parameters)
SOLR_CMIP7 = ESGF1Solr(params=SolrCMIP7Parameters)

BRIDGE_CMIP5 = ESGF15Bridge(params=SolrCMIP5Parameters)
BRIDGE_CMIP6 = ESGF15Bridge(params=SolrCMIP6Parameters)

STAC_CMIP5 = ESGFNGStac(params=StacCMIP5Parameters)
STAC_CMIP6 = ESGFNGStac(params=StacCMIP6Parameters)
STAC_CMIP7 = ESGFNGStac(params=StacCMIP7Parameters)


SearchAPISelector = Callable[[QueryCanonical, int], SearchAPI | None]
"""
Chooses which endpoint to try next

Given the canonical query and a 0-based attempt index, returns the next
[SearchAPI][esmporium.search.search_api.SearchAPI] to try, or `None` to stop.
"""


def build_list_selector(apis: Sequence[SearchAPI]) -> SearchAPISelector:
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

    def select(canonical: QueryCanonical, attempt: int) -> SearchAPI | None:
        return apis[attempt] if attempt < len(apis) else None

    return select


def build_project_list_selector(
    project_lists: Mapping[str, Sequence[SearchAPI]],
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

    def select(canonical: QueryCanonical, attempt: int) -> SearchAPI | None:
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


CMIP5_APIS: list[SearchAPI] = [
    SearchAPI("esg-dn1.nsc.liu.se", SOLR_CMIP5, build_transient_retrying(2)),
    SearchAPI("esgf.nci.org.au", SOLR_CMIP5, build_transient_retrying(2)),
    SearchAPI("esgf-node.ornl.gov", BRIDGE_CMIP5, build_transient_retrying(2)),
    SearchAPI("esgf.ceda.ac.uk", SOLR_CMIP5, build_transient_retrying(2)),
    SearchAPI("esgf-data.dkrz.de", SOLR_CMIP5, build_transient_retrying(2)),
    SearchAPI("search.east.esgf.io", STAC_CMIP5, build_transient_retrying(2)),
    SearchAPI("search.west.esgf.io", STAC_CMIP5, build_transient_retrying(2)),
]
"""
Known CMIP5 search APIs

These are sorted in order of greatest to least results,
when we checked.
"""

CMIP6_APIS: list[SearchAPI] = [
    SearchAPI("esg-dn1.nsc.liu.se", SOLR_CMIP6, build_transient_retrying(2)),
    SearchAPI("esgf-node.ornl.gov", BRIDGE_CMIP6, build_transient_retrying(2)),
    SearchAPI("esgf.nci.org.au", SOLR_CMIP6, build_transient_retrying(2)),
    SearchAPI("esgf.ceda.ac.uk", SOLR_CMIP6, build_transient_retrying(2)),
    SearchAPI("esgf-data.dkrz.de", SOLR_CMIP6, build_transient_retrying(2)),
    SearchAPI("search.east.esgf.io", STAC_CMIP6, build_transient_retrying(2)),
    SearchAPI("search.west.esgf.io", STAC_CMIP6, build_transient_retrying(2)),
]
"""
Known CMIP6 search APIs

These are sorted in order of greatest to least results,
when we checked.
"""
CMIP7_APIS: list[SearchAPI] = [
    SearchAPI("search.east.esgf.io", STAC_CMIP7, build_transient_retrying(2)),
    SearchAPI("search.west.esgf.io", STAC_CMIP7, build_transient_retrying(2)),
    SearchAPI("esgf.nci.org.au", SOLR_CMIP7, build_transient_retrying(2)),
    SearchAPI("esgf-data.dkrz.de", SOLR_CMIP7, build_transient_retrying(2)),
]
"""
Known CMIP7 search APIs

These are sorted in order of greatest to least results,
when we checked.
"""

DEFAULT_SELECTOR = build_project_list_selector(
    {
        "CMIP5": CMIP5_APIS,
        "CMIP6": CMIP6_APIS,
        "CMIP7": CMIP7_APIS,
    }
)
"""The selector used when the caller does not choose one"""
