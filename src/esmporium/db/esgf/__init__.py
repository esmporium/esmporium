"""
ESGF facet query and translation.

A user writes a query in whichever MIP dialect they prefer (one of the
`ESGFQuery*` skins) and translates it to one or more MIP eras with `translate`.
Every dialect lowers to a single canonical intermediate representation, and every
era renders that back out to its native facet names, so `N` input dialects and
`M` eras cover `N x M` journeys with `N + M` pieces of code.

This layer is facet-only: it builds native param dicts, it does not search.
"""

from esmporium.db.esgf.canonical import CANONICAL_FACETS, CanonicalQuery
from esmporium.db.esgf.mip_translation import (
    CMIP5_PROFILE,
    CMIP6_PROFILE,
    CMIP7_PROFILE,
    EraProfile,
    FacetNotRepresentableError,
    UnknownEraError,
    get_profile,
)
from esmporium.db.esgf.query_models import (
    ESGFQuery,
    ESGFQueryCMIP5,
    ESGFQueryCMIP6,
    ESGFQueryCMIP7,
)
from esmporium.db.esgf.translate import NoTargetErasError, translate

__all__ = [
    "CANONICAL_FACETS",
    "CMIP5_PROFILE",
    "CMIP6_PROFILE",
    "CMIP7_PROFILE",
    "CanonicalQuery",
    "ESGFQuery",
    "ESGFQueryCMIP5",
    "ESGFQueryCMIP6",
    "ESGFQueryCMIP7",
    "EraProfile",
    "FacetNotRepresentableError",
    "NoTargetErasError",
    "UnknownEraError",
    "get_profile",
    "translate",
]
