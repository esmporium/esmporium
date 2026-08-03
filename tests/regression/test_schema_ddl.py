"""
Regression test on the SQL our schema compiles to

The value here is not that the DDL is right, it's that it is *visible*.
A one-word change to a field annotation can silently change a column's type
or drop an index, and reviewing a diff of the models tells you very little
about what actually happens to a database.
Reviewing a diff of the `CREATE TABLE` statement tells you exactly.

So when this test fails, that is usually correct and expected.
Regenerate the file with:

    ESMPORIUM_UPDATE_REGRESSION_DATA=1 pytest tests/regression

then read the resulting diff, and check that a migration exists for it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import CreateIndex, CreateTable

from esmporium.db import METADATA

EXPECTED_DDL_FILE = Path(__file__).parents[1] / "test-data" / "schema-ddl.sql"


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

    return "\n\n".join(f"{statement};" for statement in statements) + "\n"


def test_schema_ddl():
    """
    Test that the SQL our schema compiles to hasn't changed unnoticed
    """
    ddl = get_ddl()

    if os.environ.get("ESMPORIUM_UPDATE_REGRESSION_DATA"):
        EXPECTED_DDL_FILE.parent.mkdir(parents=True, exist_ok=True)
        EXPECTED_DDL_FILE.write_text(ddl)
        pytest.skip(f"Regenerated {EXPECTED_DDL_FILE}")

    assert ddl == EXPECTED_DDL_FILE.read_text()
