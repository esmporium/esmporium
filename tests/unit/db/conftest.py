"""Fixtures for the database-layer unit tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import create_engine

from esmporium.db import METADATA

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine


@pytest.fixture
def engine() -> Iterator[Engine]:
    """
    Get an in-memory SQLite engine with our schema created

    `StaticPool` keeps every connection pointed at the one in-memory database, so
    the observer's per-call sessions all see the same tables (a plain in-memory
    SQLite gives each connection its own, separate database). The schema is built
    with `create_all` rather than migrations: the migrations are covered by
    `tests/integration/test_migrations.py`, and here we only need the tables.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    METADATA.create_all(engine)
    yield engine
    engine.dispose()
