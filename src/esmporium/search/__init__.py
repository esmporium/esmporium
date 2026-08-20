"""
Searching ESGF

Queries are written in whichever vocabulary suits the user
(see [esmporium.query][]),
then translated into the vocabulary of the search API being spoken to
and rendered into that API's wire format.
Both of those live in [esmporium.search.esgf_generations][].
"""

from esmporium.search.esgf_generations import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MIN_LIMIT,
    ESGF1Solr,
    ESGF15Bridge,
    ESGFNGStac,
    FacetListingNotSupported,
    LimitOutOfRangeError,
    OneProjectRequiredError,
    ProjectPrefixMismatchError,
    Request,
    SearchAPIGeneration,
    SolrCMIP5Parameters,
    SolrCMIP6Parameters,
    SolrCMIP7Parameters,
    StacCMIP5Parameters,
    StacCMIP6Parameters,
    StacCMIP7Parameters,
    StacParams,
    UnaskableFacetError,
    check_facets_askable,
    check_facets_expressible,
    check_limit,
    solr_facet_values,
    solr_num_found,
    stac_collection,
    stac_summary_values,
    unexpressible_facets,
)

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "MIN_LIMIT",
    "ESGF1Solr",
    "ESGF15Bridge",
    "ESGFNGStac",
    "FacetListingNotSupported",
    "LimitOutOfRangeError",
    "OneProjectRequiredError",
    "ProjectPrefixMismatchError",
    "Request",
    "SearchAPIGeneration",
    "SolrCMIP5Parameters",
    "SolrCMIP6Parameters",
    "SolrCMIP7Parameters",
    "StacCMIP5Parameters",
    "StacCMIP6Parameters",
    "StacCMIP7Parameters",
    "StacParams",
    "UnaskableFacetError",
    "check_facets_askable",
    "check_facets_expressible",
    "check_limit",
    "solr_facet_values",
    "solr_num_found",
    "stac_collection",
    "stac_summary_values",
    "unexpressible_facets",
]
