"""
Specification of queries

These are intended to be used both when searching ESGF
[TODO add cross-ref once the ESGF search module is added]
and when searching our database ([esmporium.db][esmporium.db]).
"""

from esmporium.query.canonical_query import (
    CANONICAL_FACETS,
    FacetValues,
    FacetValuesByName,
    NotACanonicalFacetError,
    NotFacetValuesError,
    QueryCanonical,
    QueryFacet,
)
from esmporium.query.known_queries import (
    NON_FACET_FIELDS,
    DuplicateCanonicalFacetError,
    FacetSpec,
    MultipleFacetAnnotationsError,
    NoFacetsDeclaredError,
    Query,
    QueryCMIP5,
    QueryCMIP6,
    QueryCMIP7,
    UnannotatedFacetError,
    UnknownProjectError,
    facet_spec,
    facet_values_from_attributes,
)
from esmporium.query.protocol import QueryProtocol, SourceQuery
from esmporium.query.translate import (
    PROJECT_QUERY_MAP_DEFAULT,
    FacetNotExpressibleError,
    NoTargetProjectError,
    from_canonical,
    to_canonical,
    translate_to_projects,
    translate_to_type,
)

__all__ = [
    "CANONICAL_FACETS",
    "NON_FACET_FIELDS",
    "PROJECT_QUERY_MAP_DEFAULT",
    "DuplicateCanonicalFacetError",
    "FacetNotExpressibleError",
    "FacetSpec",
    "FacetValues",
    "FacetValuesByName",
    "MultipleFacetAnnotationsError",
    "NoFacetsDeclaredError",
    "NoTargetProjectError",
    "NotACanonicalFacetError",
    "NotFacetValuesError",
    "Query",
    "QueryCMIP5",
    "QueryCMIP6",
    "QueryCMIP7",
    "QueryCanonical",
    "QueryFacet",
    "QueryProtocol",
    "SourceQuery",
    "UnannotatedFacetError",
    "UnknownProjectError",
    "facet_spec",
    "facet_values_from_attributes",
    "from_canonical",
    "to_canonical",
    "translate_to_projects",
    "translate_to_type",
]
