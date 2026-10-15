"""
Re-useable fixtures etc. for our integration tests

The tests use a file-backed SQLite database rather than an in-memory one,
because an in-memory database only lives as long as the connection that made it,
and migrating is inherently a multi-connection exercise.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlmodel import create_engine

from esmporium.db import METADATA, configure_sqlite_for_concurrency

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from typing import NoReturn


@pytest.fixture
def skip_or_fail() -> Callable[..., NoReturn]:
    """
    Get a function which turns every endpoint failing into a skip or a failure

    Call it with the failures carried by an error saying nobody answered
    (`NoFacadeAnsweredError.failures`),
    the failure type to skip,
    and the reason to skip with.

    For example, an endpoint which did not answer is down or unwell,
    which says nothing about the behaviour under test,
    so if that is every failure, the test skips.
    Any other failure fails the test.

    Failing on anything but certain exceptions
    means a kind of failure we add later fails loudly instead of quietly skipping.

    Note what this is *not* for. An endpoint which answers and returns nothing has not
    failed: these APIs report a mis-spelled facet name or value as a successful empty
    result, so emptiness is a finding and belongs in an assertion or a `pytest.fail`,
    never a skip.
    """

    def check(
        failures: Mapping[object, Exception], *, skippable: type[Exception], reason: str
    ) -> NoReturn:
        other_failures = [
            failure
            for failure in failures.values()
            if not isinstance(failure, skippable)
        ]
        if other_failures:
            pytest.fail(
                "\n".join(str(failure) for failure in other_failures),
                pytrace=False,
            )

        pytest.skip(reason)

    return check


@pytest.fixture
def database_path(tmp_path):
    """Get the path of the database to use in the tests"""
    return tmp_path / "esmporium.db"


@pytest.fixture
def engine(database_path):
    """Get engine for the database to use in the tests"""
    return configure_sqlite_for_concurrency(create_engine(f"sqlite:///{database_path}"))


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
