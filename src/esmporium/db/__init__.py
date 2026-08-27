"""
Local database layer

Supports handling of dataset tracking, searches, downloads etc.
This should be the only layer which touches the local databases directly.
"""

from __future__ import annotations

from esmporium.db.health import record_search_api_calls
from esmporium.db.schema import (
    DATASET_FACET_COLUMNS,
    METADATA,
    Dataset,
    SearchAPICallRecord,
)

__all__ = [
    "DATASET_FACET_COLUMNS",
    "METADATA",
    "Dataset",
    "SearchAPICallRecord",
    "record_search_api_calls",
]
