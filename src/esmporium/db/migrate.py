"""
Migration of a local database to the schema that this version of esmporium expects

The database is on the user's own machine,
so nothing else can ever migrate it for them.
That is why alembic is a required dependency of esmporium,
rather than a development-only one,
and why the migration scripts ship inside the installed package
(see the `migrations` directory next to this module).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

if TYPE_CHECKING:
    from sqlalchemy import Engine

MIGRATIONS_DIR: Path = Path(__file__).parent / "migrations"
"""
Directory that holds our alembic migration environment

This ships with the package,
so this resolves inside the user's installed copy of esmporium.
"""


def get_alembic_config() -> Config:
    """
    Get the alembic configuration that points at our packaged migrations

    We build this in code rather than reading `alembic.ini`
    because `alembic.ini` is not shipped with the package.

    Returns
    -------
    :
        Alembic configuration.

        This has no database URL set.
        Callers are expected to supply a connection instead,
        which is what [`upgrade_to_head`][esmporium.db.migrate.upgrade_to_head] does.
    """
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))

    return config


def get_head_revision() -> str:
    """
    Get the revision that this version of esmporium expects a database to be at

    Returns
    -------
    :
        The identifier of the most recent migration shipped with esmporium.
    """
    head = ScriptDirectory.from_config(get_alembic_config()).get_current_head()
    if head is None:  # pragma: no cover
        # Can only happen if the packaged migrations went missing.
        msg = f"No migrations found in {MIGRATIONS_DIR}"
        raise RuntimeError(msg)

    return head


def get_current_revision(engine: Engine) -> str | None:
    """
    Get the revision that a database is currently at

    Parameters
    ----------
    engine
        Engine connected to the database to inspect

    Returns
    -------
    :
        The database's current revision,
        or `None` if the database has never had a migration applied
        (which includes the case of a database that does not exist yet).
    """
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def upgrade_to_head(engine: Engine) -> None:
    """
    Upgrade a database to the schema that this version of esmporium expects

    This is safe to call on a database that is already up to date,
    in which case it does nothing.
    It is also safe to call on a brand new, empty database,
    in which case it creates the schema from scratch.

    Parameters
    ----------
    engine
        Engine connected to the database to upgrade
    """
    config = get_alembic_config()

    with engine.begin() as connection:
        # Hand our connection to `env.py` rather than letting it create an engine,
        # so the caller keeps control of how the database is opened.
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
