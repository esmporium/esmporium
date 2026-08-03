"""
Tests that our migrations and our models describe the same database

These are the tests that make migrations trustworthy.
Alembic's autogenerate is a drafting aid, not a guarantee:
nothing stops someone changing a model and never writing the migration,
and the result of that is a user whose database silently doesn't match
the code that reads it.

The tests use a file-backed SQLite database rather than an in-memory one,
because an in-memory database only lives as long as the connection that made it,
and migrating is inherently a multi-connection exercise.
"""

from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect
from sqlmodel import create_engine

from esmporium.db import METADATA, migrate


@pytest.fixture
def engine(tmp_path):
    """Get an engine for an empty, file-backed database that does not exist yet"""
    return create_engine(f"sqlite:///{tmp_path / 'esmporium.db'}")


def get_pending_changes(engine):
    """
    Get the changes alembic would need to make for the database to match the models

    Returns
    -------
    :
        The differences alembic detected. Empty if the two agree.
    """
    with engine.connect() as connection:
        migration_context = MigrationContext.configure(
            connection,
            opts={"compare_type": True, "compare_server_default": True},
        )

        return compare_metadata(migration_context, METADATA)


def test_migrations_leave_database_matching_models(engine):
    """
    Test that applying every migration gives the schema our models describe

    If this fails, the models were changed without a migration being written.
    The fix is `make migration MESSAGE="..."`, then reading what it generated.
    """
    migrate.upgrade_to_head(engine)

    assert get_pending_changes(engine) == []


def test_upgrade_from_nothing_records_head_revision(engine):
    """
    Test that we can tell an up-to-date database from one that needs migrating
    """
    assert migrate.get_current_revision(engine) is None

    migrate.upgrade_to_head(engine)

    assert migrate.get_current_revision(engine) == migrate.get_head_revision()


def test_upgrade_is_idempotent(engine):
    """
    Test that migrating an already-migrated database is a no-op

    Callers shouldn't have to check whether a migration is needed before asking,
    so upgrading twice has to be safe.
    """
    migrate.upgrade_to_head(engine)
    before = inspect(engine).get_table_names()

    migrate.upgrade_to_head(engine)

    assert inspect(engine).get_table_names() == before
    assert get_pending_changes(engine) == []
