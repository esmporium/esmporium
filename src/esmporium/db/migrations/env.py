"""
Alembic migration environment for esmporium's local database

This file is loaded and executed by alembic,
it is not part of esmporium's public API.
Note that this directory deliberately has no `__init__.py`:
it is data that ships with the package, not an importable sub-package
(if it were importable, our docs generation would import this module
and run it as a side effect).

To run migrations from within esmporium, see [`esmporium.db.migrate`][].
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, engine_from_config, pool

# Note that this is esmporium's own metadata, not SQLModel's global one,
# so autogenerate only ever sees tables we defined.
from esmporium.db.schema import METADATA

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = METADATA


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode

    This emits SQL to the script output stream
    rather than running it against a database,
    which is handy for reviewing what a migration will actually do.
    """
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # See the note in `do_run_migrations`.
        render_as_batch=True,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """
    Run the migrations against an open connection

    Parameters
    ----------
    connection
        Connection against which to run the migrations
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # SQLite can't `ALTER TABLE` in most of the ways
        # that a migration typically wants to.
        # Batch mode makes alembic emit
        # a create-new-table/copy/drop/rename dance instead,
        # which is the only way most schema changes can work on SQLite.
        render_as_batch=True,
        # Without these, alembic's autogenerate silently ignores
        # changes to a column's type or default,
        # which is exactly the kind of change that is easy to forget.
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode

    If a caller has already put a connection on `config.attributes`
    (which is what [`esmporium.db.migrate`][] does),
    we use it rather than creating our own engine.
    That is what lets the same migration scripts be driven
    both by the `alembic` command line during development
    and by esmporium itself at runtime.
    """
    connection = config.attributes.get("connection", None)

    if connection is not None:
        do_run_migrations(connection)
        return

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as conn:
        do_run_migrations(conn)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
