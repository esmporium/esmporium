"""
Searching ESGF

Queries are written in whichever vocabulary suits the user
(see [esmporium.query][]),
then translated into the vocabulary of the search API being spoken to.
Those vocabularies live in [esmporium.search.esgf_generations][].
"""

from esmporium.search.esgf_generations import (
    SolrCMIP5Parameters,
    SolrCMIP6Parameters,
    SolrCMIP7Parameters,
    StacCMIP5Parameters,
    StacCMIP6Parameters,
    StacCMIP7Parameters,
    StacParams,
)

__all__ = [
    "SolrCMIP5Parameters",
    "SolrCMIP6Parameters",
    "SolrCMIP7Parameters",
    "StacCMIP5Parameters",
    "StacCMIP6Parameters",
    "StacCMIP7Parameters",
    "StacParams",
]
