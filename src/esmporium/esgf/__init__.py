"""
ESGF facet query and translation.

A user writes a query in whichever project dialect they prefer (one of the
`ESGFQuery*` skins) and translates it to one or more projects with `translate`.
Every dialect lowers to a single canonical intermediate representation, and every
project renders that back out to its native facet names, so `N` input dialects and
`M` projects cover `N x M` journeys with `N + M` pieces of code.
"""

from esmporium.esgf.canonical import CANONICAL_FACETS, CanonicalQuery
from esmporium.esgf.project_translation_maps import (
    CMIP5_PROFILE,
    CMIP6_PROFILE,
    CMIP7_PROFILE,
    FacetNotRepresentableError,
    ProjectProfile,
    UnknownProjectError,
    get_profile,
)
from esmporium.esgf.query_models import (
    ESGFQuery,
    ESGFQueryCMIP5,
    ESGFQueryCMIP6,
    ESGFQueryCMIP7,
)
from esmporium.esgf.translate import NoTargetProjectError, translate

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
    "FacetNotRepresentableError",
    "NoTargetProjectError",
    "ProjectProfile",
    "UnknownProjectError",
    "get_profile",
    "translate",
]
