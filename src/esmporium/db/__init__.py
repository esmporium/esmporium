"""
Local database layer

Supports handling of dataset tracking, searches, downloads etc.
This should be the only layer which touches the local databases directly.
"""

from __future__ import annotations

from esmporium.db.schema import METADATA, Dataset

__all__ = ["METADATA", "Dataset"]
