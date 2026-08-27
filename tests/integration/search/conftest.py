"""
Fixtures for the live search integration tests

The `engine`/`database_path` fixtures come from `tests/integration/conftest.py`
(file-backed SQLite, unmigrated). Here we add the piece the health-tracking
assertions need: a migrated database, an observer that records into it, and a way
to read the recorded rows back.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, select

from esmporium.db import SearchApiCallRecord, record_search_api_calls
from esmporium.db.migrate import upgrade_to_head


@pytest.fixture
def recorded(engine):
    """
    Get an observer that records search-API calls, and a reader for the rows

    Yields a `(observer, read_calls)` pair: pass `observer` to `search` or
    `check_query_values`, then call `read_calls()` to get the recorded
    [SearchApiCallRecord][esmporium.db.schema.SearchApiCallRecord] rows back.
    """
    upgrade_to_head(engine)

    with Session(engine) as session:
        observer = record_search_api_calls(session)

        def read_calls():
            # A fresh session so we read what the observer committed, not a stale
            # identity-map view.
            with Session(engine) as reader:
                return list(
                    reader.exec(
                        select(SearchApiCallRecord).order_by(SearchApiCallRecord.id)
                    )
                )

        yield observer, read_calls
