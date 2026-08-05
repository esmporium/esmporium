"""
Re-useable fixtures etc. for our integration tests

The tests use a file-backed SQLite database rather than an in-memory one,
because an in-memory database only lives as long as the connection that made it,
and migrating is inherently a multi-connection exercise.
"""

from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlmodel import create_engine

from esmporium.db import METADATA


@pytest.fixture
def database_path(tmp_path):
    """Get the path of the database to use in the tests"""
    return tmp_path / "esmporium.db"


@pytest.fixture
def engine(database_path):
    """Get engine for the database to use in the tests"""
    return create_engine(f"sqlite:///{database_path}")


@pytest.fixture
def get_pending_changes():
    """
    Get a helper that reports the changes alembic would still need to make

    The changes are those needed for the database to match our models,
    so an empty result means the database and the models agree.
    """

    def get(engine):
        with engine.connect() as connection:
            migration_context = MigrationContext.configure(
                connection,
                opts={"compare_type": True, "compare_server_default": True},
            )

            return compare_metadata(migration_context, METADATA)

    return get
