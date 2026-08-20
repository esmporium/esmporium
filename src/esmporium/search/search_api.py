"""
The search APIs we can talk to, and how we pick between them

A [SearchAPI][esmporium.search.search_api.SearchAPI] is one endpoint we can hit:
a host, the [generation][esmporium.search.esgf_generations.SearchAPIGeneration]
(wire format) it speaks, and its own retry policy.
A host speaks exactly one wire format, so these travel together.

A [selector][esmporium.search.search_api.SearchAPISelector] turns a query into an
ordered choice of endpoints to try.
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
from esmporium.search.retry import transient_retry


@dataclass(frozen=True)
class SearchAPI:
    """
    One search endpoint we can hit
    """

    host: str
    """The host to send requests to, e.g. `esgf.nci.org.au`"""

    generation: SearchAPIGeneration
    """The wire format this host speaks, handed the vocabulary to speak it in"""

    retrying: Retrying
    """The retry policy to send under"""

    scheme: str = "https"
    """
    The URL scheme to reach this host over

    Almost every node speaks `https`; this exists so that a host which only
    offers `http` can be reached without hard-coding the scheme into `url`.
    """

    def url(self, request: Request) -> str:
        """
        Build the full URL for a request against this host

        Parameters
        ----------
        request
            The request whose path to put on this host

        Returns
        -------
        :
            The URL to send `request` to
        """
        return f"{self.scheme}://{self.host}{request.path}"


# One generation per (wire-format, project). Each is handed its params class; for
# STAC the cmipN: prefix rides on the params (StacCMIP*.prefix), so a params class
# can never be paired with the wrong prefix.
SOLR_CMIP5 = ESGF1Solr(params=SolrCMIP5Parameters)
STAC_CMIP5 = ESGFNGStac(params=StacCMIP5Parameters)
SOLR_CMIP6 = ESGF1Solr(params=SolrCMIP6Parameters)
STAC_CMIP6 = ESGFNGStac(params=StacCMIP6Parameters)
SOLR_CMIP7 = ESGF1Solr(params=SolrCMIP7Parameters)
STAC_CMIP7 = ESGFNGStac(params=StacCMIP7Parameters)

# ORNL's ESGF-1.5 bridge reuses the SAME Solr param name tables (names match);
# only the request encoding differs, which the generation handles.
BRIDGE_CMIP5 = ESGF15Bridge(params=SolrCMIP5Parameters)
BRIDGE_CMIP6 = ESGF15Bridge(params=SolrCMIP6Parameters)

# Per-project rankings, ORDERED BY MEASURED UNIQUE-DATASET COVERAGE (a live
# historical/tas probe: unique master_id, latest & not-retracted). By default
# search() stops at the first node that answers, so this order decides who that
# is; it is also the fallback chain when the top node is down. ORNL's live
# "1.5-bridge" (ESGF15Bridge -- Solr-shaped replies, comma-joined request
# dialect) is included at its measured rank (CMIP6 2nd, CMIP5 3rd).
# TODOZeb: remove a lot of these comments above? The ordering was based on
# live searching but that ordering could change in the future?
# Also should these defaults live here or in __init__.py?
CMIP5_APIS: list[SearchAPI] = [  # LIU > NCI > ORNL > CEDA > DKRZ; NG has no CMIP5
    SearchAPI("esg-dn1.nsc.liu.se", SOLR_CMIP5, transient_retry(2)),
    SearchAPI("esgf.nci.org.au", SOLR_CMIP5, transient_retry(2)),
    SearchAPI("esgf-node.ornl.gov", BRIDGE_CMIP5, transient_retry(2)),
    SearchAPI("esgf.ceda.ac.uk", SOLR_CMIP5, transient_retry(2)),
    SearchAPI("esgf-data.dkrz.de", SOLR_CMIP5, transient_retry(2)),
    SearchAPI("search.east.esgf.io", STAC_CMIP5, transient_retry(2)),
    SearchAPI("search.west.esgf.io", STAC_CMIP5, transient_retry(2)),
]
CMIP6_APIS: list[SearchAPI] = [  # LIU > ORNL > NCI > CEDA > DKRZ, then NG/STAC
    SearchAPI("esg-dn1.nsc.liu.se", SOLR_CMIP6, transient_retry(2)),
    SearchAPI("esgf-node.ornl.gov", BRIDGE_CMIP6, transient_retry(2)),
    SearchAPI("esgf.nci.org.au", SOLR_CMIP6, transient_retry(2)),
    SearchAPI("esgf.ceda.ac.uk", SOLR_CMIP6, transient_retry(2)),
    SearchAPI("esgf-data.dkrz.de", SOLR_CMIP6, transient_retry(2)),
    SearchAPI("search.east.esgf.io", STAC_CMIP6, transient_retry(2)),
    SearchAPI("search.west.esgf.io", STAC_CMIP6, transient_retry(2)),
]
CMIP7_APIS: list[SearchAPI] = [  # NG first (CMIP7 lives there); ESGF1 fallback
    SearchAPI("search.east.esgf.io", STAC_CMIP7, transient_retry(2)),
    SearchAPI("search.west.esgf.io", STAC_CMIP7, transient_retry(2)),
    SearchAPI("esgf.nci.org.au", SOLR_CMIP7, transient_retry(2)),
    SearchAPI("esgf-data.dkrz.de", SOLR_CMIP7, transient_retry(2)),
]

PROJECT_PLANS: Mapping[str, Sequence[SearchAPI]] = {
    "CMIP5": CMIP5_APIS,
    "CMIP6": CMIP6_APIS,
    "CMIP7": CMIP7_APIS,
}
"""The default per-project ranking of endpoints to try"""


SearchAPISelector = Callable[[QueryCanonical, int], SearchAPI | None]
"""
Chooses which endpoint to try next

Given the canonical query and a 0-based attempt index, returns the next
[SearchAPI][esmporium.search.search_api.SearchAPI] to try, or None to stop.
Injectable, so the choice and order of endpoints can vary
without touching the search loop.
Our default ranks endpoints by the query's project
"""


def list_selector(apis: Sequence[SearchAPI]) -> SearchAPISelector:
    """
    Build a selector that yields the given endpoints in order, then stops

    The project is ignored: every query gets the same list.

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


def project_ranked_selector(
    plans: Mapping[str, Sequence[SearchAPI]],
) -> SearchAPISelector:
    """
    Build a selector that yields a per-project ranking of endpoints

    Parameters
    ----------
    plans
        The endpoints to yield for each project, in order

    Returns
    -------
    :
        A selector which yields `plans` for the query's project,
        or nothing for a project it has no plan for
    """

    def select(canonical: QueryCanonical, attempt: int) -> SearchAPI | None:
        apis = plans.get(canonical.project[0])
        if apis is None:
            return None  # a project we have no plan for -> nothing to try
        return apis[attempt] if attempt < len(apis) else None

    return select


DEFAULT_SELECTOR = project_ranked_selector(PROJECT_PLANS)
"""The selector used when the caller does not choose one"""
