"""
Tests that our migrations and our models describe the same database

These tests ensure that every time that someone changes a model,
they have to also write the migration.

The tests use a file-backed SQLite database rather than an in-memory one,
because an in-memory database only lives as long as the connection that made it,
and migrating is inherently a multi-connection exercise.
"""

from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlmodel import Session, create_engine, text

from esmporium.db import METADATA, Dataset, migrate


@pytest.fixture
def engine(tmp_path):
    """Get engine for the database to use in the tests"""
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


def get_schema(engine):
    """
    Get everything SQLite knows about the shape of a database

    Returns
    -------
    :
        The type, name and defining SQL of every table, index and constraint,
        in a stable order.
    """
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT type, name, sql FROM sqlite_master ORDER BY type, name")
        ).all()


def test_migrations_leave_database_matching_models(engine):
    """
    Test that applying every migration gives the schema our models describe

    If this fails, the models were changed without a migration being written.
    """
    migrate.upgrade_to_head(engine)

    assert get_pending_changes(engine) == [], (
        'Start with `make migration MESSAGE="..." to fix this'
    )


def test_upgrade_from_nothing_records_head_revision(engine):
    """
    Test that we can tell an up-to-date database from one that needs migrating

    The migrating itself is alembic's, but the two answers being compared here
    are ours: `get_current_revision` and `get_head_revision`
    are what esmporium will use to decide whether a user's database needs
    upgrading before it is opened, and both go through the alembic configuration
    we build in code (see `get_alembic_config`) rather than through `alembic.ini`,
    which isn't shipped with the package.
    Point that configuration at the wrong place, or ship without the migrations,
    and this is the test that says so.

    It also pins the contract that a database which doesn't exist yet
    reports `None` rather than raising,
    because "no database" and "database that has never been migrated"
    have to be handled the same way.
    """
    assert migrate.get_current_revision(engine) is None

    migrate.upgrade_to_head(engine)

    assert migrate.get_current_revision(engine) == migrate.get_head_revision()


def test_upgrade_is_idempotent(engine, get_dataset_kwargs):
    """
    Test that migrating an already-migrated database is a no-op

    Callers shouldn't have to check whether a migration is needed before asking,
    so upgrading twice has to be safe.

    "No-op" is checked in the two ways that can actually bite:
    the schema has to come out identical statement for statement
    (not merely have the same tables in it),
    and the rows have to survive.
    Both matter because our migrations run in batch mode,
    where changing a table means copying it into a new one and dropping the old,
    so a migration that ran a second time by mistake
    would take the data with it.
    """
    migrate.upgrade_to_head(engine)

    # Add an entry to the database
    with Session(engine) as session:
        session.add(Dataset(**get_dataset_kwargs("id-one")))
        session.commit()

    schema_before = get_schema(engine)
    revision_before = migrate.get_current_revision(engine)

    migrate.upgrade_to_head(engine)

    assert get_schema(engine) == schema_before
    assert migrate.get_current_revision(engine) == revision_before
    assert get_pending_changes(engine) == []

    # Make sure that migrating an already-migrated database
    # is actually a no-op by checking that the entry we added above
    # wasn't wiped by the upgrade call,
    # which it would be if the migrations were actually run.
    with Session(engine) as session:
        assert session.get(Dataset, "id-one") is not None
