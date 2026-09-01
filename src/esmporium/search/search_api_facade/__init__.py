"""
Search API facade

This contains our facades to search APIs.
These facades are introduced to add more robust
query creation, result parsing and error handling.
Complete documentation of this will be added in future.

A facade pairs a *parameter definition*
(the vocabulary a project is written in for a family of APIs,
e.g. [ESGF1_CMIP6_FACADE_PARAMETERS][(m).ESGF1_CMIP6_FACADE_PARAMETERS])
with a *search API*
(the format spoken by a family of endpoints,
e.g. [SearchAPIESGF1Solr][esmporium.search.apis.SearchAPIESGF1Solr]).
The parameter definition is the facade's concern:
it is the facade which turns a canonical query into the names
and shapes a search API speaks,
and which turns the answer back into the canonical vocabulary.
The search API (not the facade layer) knows nothing about canonical queries;
it only knows how to encode a request and decode a response for its own format.
Keeping the two layers visibly distinct is deliberate.
for example, every facade user reaches through to `facade.search_api.host` explicitly,
rather than the facade re-exposing it,
so it is always clear where things are coming from.
"""
# TODO: devs - add more complete docs in a follow up PR

from __future__ import annotations

from esmporium.search.search_api_facade.core import (
    SearchAPIFacade,
    UnaskableFacetError,
    check_facets_askable,
    check_facets_expressible,
    get_unexpressible_facets,
)
from esmporium.search.search_api_facade.parameters import (
    ESGF1_CMIP5_FACADE_PARAMETERS,
    ESGF1_CMIP6_FACADE_PARAMETERS,
    ESGF1_CMIP7_FACADE_PARAMETERS,
    ESGFNG_CMIP5_FACADE_PARAMETERS,
    ESGFNG_CMIP6_FACADE_PARAMETERS,
    ESGFNG_CMIP7_FACADE_PARAMETERS,
    ESGF1CMIP5ParametersQueryStyle,
    ESGF1CMIP6ParametersQueryStyle,
    ESGF1CMIP7ParametersQueryStyle,
    ESGFNGCMIP5ParametersQueryStyle,
    ESGFNGCMIP6ParametersQueryStyle,
    ESGFNGCMIP7ParametersQueryStyle,
    FacadeParametersProtocol,
    OneProjectRequiredError,
    ProjectPrefixMismatchError,
    get_mapping_to_query_style_facet_names,
)
from esmporium.search.search_api_facade.selectors import (
    DEFAULT_SEARCH_API_FACADES_BY_PROJECT,
    DEFAULT_SELECTOR,
    SearchAPIFacadeSelector,
    SelectorOfferedNoAPIFacadeError,
    build_list_selector,
    build_project_list_selector,
)
from esmporium.search.search_api_facade.store import (
    INBUILT_SEARCH_API_FACADE_STORE,
    SearchAPIFacadeClassification,
    SearchAPIFacadeStore,
)

__all__ = [
    "DEFAULT_SEARCH_API_FACADES_BY_PROJECT",
    "DEFAULT_SELECTOR",
    "ESGF1_CMIP5_FACADE_PARAMETERS",
    "ESGF1_CMIP6_FACADE_PARAMETERS",
    "ESGF1_CMIP7_FACADE_PARAMETERS",
    "ESGFNG_CMIP5_FACADE_PARAMETERS",
    "ESGFNG_CMIP6_FACADE_PARAMETERS",
    "ESGFNG_CMIP7_FACADE_PARAMETERS",
    "INBUILT_SEARCH_API_FACADE_STORE",
    "ESGF1CMIP5ParametersQueryStyle",
    "ESGF1CMIP6ParametersQueryStyle",
    "ESGF1CMIP7ParametersQueryStyle",
    "ESGFNGCMIP5ParametersQueryStyle",
    "ESGFNGCMIP6ParametersQueryStyle",
    "ESGFNGCMIP7ParametersQueryStyle",
    "FacadeParametersProtocol",
    "OneProjectRequiredError",
    "ProjectPrefixMismatchError",
    "SearchAPIFacade",
    "SearchAPIFacadeClassification",
    "SearchAPIFacadeSelector",
    "SearchAPIFacadeStore",
    "SelectorOfferedNoAPIFacadeError",
    "SelectorOfferedNoAPIFacadeError",
    "UnaskableFacetError",
    "build_list_selector",
    "build_project_list_selector",
    "check_facets_askable",
    "check_facets_expressible",
    "get_mapping_to_query_style_facet_names",
    "get_unexpressible_facets",
]
