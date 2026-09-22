"""
High-level search functionality
"""

# TODO: is this "high-level", or "low level"?
from __future__ import annotations

from esmporium.search.search.errors import (
    AllSubQueriesFailedError,
    ClashingFacetsForFacadeError,
    CouldNotGetSearchResponseError,
    CouldNotSearchError,
    CouldNotUseSearchResultsError,
    NoFacadeAnsweredError,
    PaginationLimitError,
    PaginationWarning,
    SearchAPIRequestError,
)
from esmporium.search.search.firing import (
    curl_equivalent,
    fire,
    get_url,
    log_request_as_url_and_curl,
)
from esmporium.search.search.keys import FacadeKey, get_facade_key
from esmporium.search.search.pagination import FacadePages, collect_all_pages
from esmporium.search.search.run import (
    ProcessorFactory,
    SearchOutcome,
    search,
    search_single_project,
)

__all__ = [
    "AllSubQueriesFailedError",
    "ClashingFacetsForFacadeError",
    "CouldNotGetSearchResponseError",
    "CouldNotSearchError",
    "CouldNotUseSearchResultsError",
    "FacadeKey",
    "FacadePages",
    "NoFacadeAnsweredError",
    "PaginationLimitError",
    "PaginationWarning",
    "ProcessorFactory",
    "SearchAPIRequestError",
    "SearchOutcome",
    "collect_all_pages",
    "curl_equivalent",
    "fire",
    "get_facade_key",
    "get_url",
    "log_request_as_url_and_curl",
    "search",
    "search_single_project",
]
