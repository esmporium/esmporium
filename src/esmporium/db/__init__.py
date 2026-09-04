"""
Local database layer

Supports handling of dataset tracking, searches, downloads etc.
This should be the only layer which touches the local databases directly.
"""

from __future__ import annotations

from esmporium.db.dataset_uniqueness import facet_differences
from esmporium.db.results_to_database import (
    UnhandledDatasetClashError,
    ingest_results,
    save_dataset,
)
from esmporium.db.schema import (
    DATASET_FACET_COLUMNS,
    METADATA,
    Dataset,
    DatasetNodeInformation,
    DatasetRawDoc,
    DatasetVersionSpecific,
    RawDocVersionLink,
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
    "DatasetNodeInformation",
    "DatasetRawDoc",
    "DatasetVersionSpecific",
    "HostHealth",
    "HostRanker",
    "RawDocVersionLink",
    "SearchAPICallRecord",
    "UnhandledDatasetClashError",
    "aggregate_host_health",
    "build_health_selector",
    "facet_differences",
    "get_median_response_time_for_ranking",
    "ingest_results",
    "record_search_api_calls",
    "save_dataset",
]
