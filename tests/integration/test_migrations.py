"""
Tests that our migrations and our models describe the same database

These tests ensure that every time that someone changes a model,
they have to also write the migration.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, select, text

from esmporium.db import Dataset, UnhandledDatasetClashError, migrate, save_dataset


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


def test_migrations_leave_database_matching_models(engine, get_pending_changes):
    """
    Test that applying every migration gives the schema our models describe

    If this fails, the models were changed without a migration being written.
    """
    migrate.upgrade_to_head(engine)

    assert get_pending_changes(engine) == [], (
        'Start with `make migration MESSAGE="..." to fix this'
    )


def test_clash_detection_works_on_a_migrated_database(engine, get_dataset_kwargs):
    """A clash still raises `UnhandledDatasetClashError` on a *migrated* database.

    `save_dataset` recognises an identity clash by matching `DATASET_IDENTITY_INDEX` in
    SQLite's `IntegrityError` text. That name has to agree in three places: the model's
    `__table_args__`, the constant `save_dataset` reads, and the migration that actually
    builds the index. The unit clash tests create the schema with `METADATA.create_all`,
    so they only ever exercise the first two -- the index they hit is the model's.

    This test migrates a real database instead, so the index is the *migration's*, and
    checks a clash still surfaces as `UnhandledDatasetClashError`. If the migration's
    index name ever drifts from the constant (a rename applied to the model but not the
    migration), `save_dataset` stops matching, a raw `IntegrityError` escapes, and this
    fails. It is the one guard `test_migrations_leave_database_matching_models` cannot
    give: SQLite cannot reflect expression-based indexes, so alembic silently skips
    comparing this one (see its `SAWarning`).

    `grid_label=None` uses the CMIP5 shape on purpose, so the migrated index's
    `coalesce(grid_label, '')` expression is exercised too.
    """
    migrate.upgrade_to_head(engine)

    clash = get_dataset_kwargs("clash", grid_label=None)
    with Session(engine) as session:
        save_dataset(session, Dataset(**clash))
        session.commit()

        with pytest.raises(UnhandledDatasetClashError):
            save_dataset(session, Dataset(**clash))


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


def test_upgrade_is_idempotent(engine, get_dataset_kwargs, get_pending_changes):
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
        surviving = session.exec(
            select(Dataset).where(Dataset.id_project_specific == "id-one_ps")
        ).all()
        assert len(surviving) == 1
