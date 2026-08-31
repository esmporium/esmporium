"""
Local database layer

Supports handling of dataset tracking, searches, downloads etc.
This should be the only layer which touches the local databases directly.
"""

from __future__ import annotations

from esmporium.db.schema import (
    DATASET_FACET_COLUMNS,
    METADATA,
    Dataset,
    SearchAPICallRecord,
)
from esmporium.db.search_health import (
    HostHealth,
    HostRanker,
    aggregate_host_health,
    build_health_selector,
    get_median_response_time_for_ranking,
    record_search_api_calls,
)

__all__ = [
    "DATASET_FACET_COLUMNS",
    "METADATA",
    "Dataset",
    "HostHealth",
    "HostRanker",
    "SearchAPICallRecord",
    "aggregate_host_health",
    "build_health_selector",
    "get_median_response_time_for_ranking",
    "record_search_api_calls",
]
