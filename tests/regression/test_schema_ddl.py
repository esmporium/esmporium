"""
Regression test on the SQL our schema compiles to

The value here is not that the DDL is right, it's that it is *visible*.
A one-word change to a field annotation can silently change a column's type
or drop an index, and reviewing a diff of the models tells you very little
about what actually happens to a database.
Reviewing a diff of the SQL `CREATE TABLE` statement tells you exactly.
"""

from __future__ import annotations

from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import CreateIndex, CreateTable

from esmporium.db import METADATA


def get_ddl():
    """
    Get the DDL that our schema compiles to, as a single deterministic string

    Returns
    -------
    :
        The `CREATE TABLE` and `CREATE INDEX` statements for every table,
        in a stable order.
    """
    dialect = sqlite.dialect()

    statements = []
    for table in METADATA.sorted_tables:
        statements.append(str(CreateTable(table).compile(dialect=dialect)).strip())
        for index in sorted(table.indexes, key=lambda idx: idx.name or ""):
            statements.append(str(CreateIndex(index).compile(dialect=dialect)).strip())

    ddl = "\n\n".join(f"{statement};" for statement in statements)

    # Be careful to strip trailing whitespace
    # so we don't end up fighting our own pre-comit.
    # Nothing is lost by dropping the trailing whitespace.
    return "\n".join(line.rstrip() for line in ddl.splitlines()) + "\n"


def test_schema_ddl(file_regression):
    """
    Test that the SQL our schema compiles to hasn't changed unnoticed

    When this test fails, that is usually correct and expected.
    Regenerate the file by using the `--force-regen` flag (from pytest-regressions),
    then read the resulting diff
    (`tests/integration/test_migrations.py` ensures that a migration exists,
    but double checking manually won't hurt).
    """
    file_regression.check(get_ddl(), extension=".sql")
