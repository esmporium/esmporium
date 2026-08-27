"""
Fixtures for the live search integration tests

The `engine`/`database_path` fixtures come from `tests/integration/conftest.py`
(file-backed SQLite, unmigrated). Here we add the piece the health-tracking
assertions need: a migrated database, an observer that records into it, and a way
to read the recorded rows back.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlmodel import Session, col, select

from esmporium.db import SearchAPICallRecord, record_search_api_calls
from esmporium.db.migrate import upgrade_to_head

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from sqlalchemy import Engine

    from esmporium.search.health import SearchAPICallObserver

# The observer to record with, plus a function that reads back the rows recorded.
Recorded = tuple["SearchAPICallObserver", "Callable[[], list[SearchAPICallRecord]]"]


@pytest.fixture
def recorded(engine: Engine) -> Iterator[Recorded]:
    """
    Get an observer that records search-API calls, and a reader for the rows

    Yields a `(observer, read_calls)` pair: pass `observer` to `search` or
    `check_query_values`, then call `read_calls()` to get the recorded
    [SearchAPICallRecord][esmporium.db.schema.SearchAPICallRecord] rows back.
    """
    upgrade_to_head(engine)

    observer = record_search_api_calls(engine)

    def read_calls() -> list[SearchAPICallRecord]:
        # A fresh session so we read what the observer committed, not a stale
        # identity-map view.
        with Session(engine) as reader:
            return list(
                reader.exec(
                    select(SearchAPICallRecord).order_by(col(SearchAPICallRecord.id))
                )
            )

    yield observer, read_calls
