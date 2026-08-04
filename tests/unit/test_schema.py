"""
Tests of our database schema

There is no behaviour to test yet, only declarations.
What these tests pin down is therefore deliberately narrow:
that the declarations are ones SQLAlchemy can actually build a table from,
that a row survives a round-trip through the database unchanged,
and that the constraints we think we have are constraints we actually have.

The point of the last of those is that a constraint you believe in
but haven't asserted is just a comment.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, create_engine, select

from esmporium.db import METADATA, Dataset

VALID_DATASET_KWARGS = {
    "id": "CMIP6.CMIP.CSIRO.ACCESS-ESM1-5.historical.r1i1p1f1.Amon.tas.gn",
    "project": "CMIP6",
    "model": "ACCESS-ESM1-5",
    "institution": "CSIRO",
    "experiment": "historical",
    "variant_label": "r1i1p1f1",
    "variable": "tas",
    "reporting_interval": "mon",
    "grid_label": "gn",
    "processing_id": "Amon",
}
"""A dataset with every facet filled in, i.e. one the database should accept"""

FACET_COLUMNS = [k for k in VALID_DATASET_KWARGS if k != "id"]


@pytest.fixture
def engine():
    """
    Get an engine for a fresh, empty, in-memory database

    The schema is created straight from the models here, not from the migrations.
    Whether the migrations agree with the models is a separate question,
    tested in `tests/integration/test_migrations.py`.
    """
    engine = create_engine("sqlite://")
    METADATA.create_all(engine)

    return engine


def test_metadata_can_be_realised_as_tables(engine):
    """
    Test that our declarations can actually be turned into tables

    This is a smoke test, but not a trivial one.
    SQLModel resolves the type annotations on our fields at class creation time,
    and an annotation it can't map to a column type
    is an error you only see when something tries to build the table.
    Adding `from __future__ import annotations` to the schema module,
    for example, breaks exactly this and nothing else,
    which is why the schema module carries a comment warning against it.
    """
    assert "dataset" in METADATA.tables

    with Session(engine) as session:
        # Empty, but only answerable if the table exists.
        assert session.exec(select(Dataset)).all() == []


def test_round_trip(engine):
    """
    Test that a dataset comes back out of the database as it went in

    Every column here is a string, so this looks like it can't fail.
    It is worth having anyway, because it stops being free
    the moment a column is anything else: a datetime, an enum, a path.
    Those are the columns where the value that comes back
    is quietly not the value that went in.
    """
    with Session(engine) as session:
        session.add(Dataset(**VALID_DATASET_KWARGS))
        session.commit()

    with Session(engine) as session:
        retrieved = session.get(Dataset, VALID_DATASET_KWARGS["id"])

    assert retrieved is not None
    assert retrieved.model_dump() == VALID_DATASET_KWARGS


def test_id_is_unique(engine):
    """
    Test that two datasets can't share an ID

    The ID is derived from the facets of the dataset,
    so two rows sharing one means we've either double-inserted
    or contradicted ourselves about what that dataset is.
    """
    with Session(engine) as session:
        session.add(Dataset(**VALID_DATASET_KWARGS))
        session.commit()

        # Same ID, but a different dataset: exactly the contradiction we want caught.
        session.add(Dataset(**{**VALID_DATASET_KWARGS, "variable": "tos"}))

        with pytest.raises(IntegrityError):
            session.commit()


def test_facets_are_unique(engine):
    """
    Test that two datasets can't describe the same combination of facets

    This is a cross-check rather than a duplicate of the primary key.
    The ID comes from ESGF's `master_id`, which we don't control,
    so the same dataset arriving under two spellings of its ID
    would slip past the primary key.
    The facets are what we parsed ourselves, so checking them catches that.
    """
    with Session(engine) as session:
        session.add(Dataset(**VALID_DATASET_KWARGS))
        session.commit()

        same_facets_different_id = {**VALID_DATASET_KWARGS, "id": "a-different-id"}
        session.add(Dataset(**same_facets_different_id))

        with pytest.raises(IntegrityError):
            session.commit()


def test_assignment_is_validated():
    """
    Test that changing a field on a dataset is checked
    """
    dataset = Dataset(**VALID_DATASET_KWARGS)

    with pytest.raises(ValidationError):
        dataset.variable = None


def test_bad_values_are_validated_on_construction():
    """
    Test that passing a bad value is caught when the dataset is built

    SQLModel doesn't validate table models on construction,
    but its `__init__` assigns fields with `setattr`,
    so `validate_assignment` catches this anyway.
    Worth pinning down, because it is a side effect of how SQLModel is written
    rather than something it promises, so it could change under us.
    """
    with pytest.raises(ValidationError):
        Dataset(**{**VALID_DATASET_KWARGS, "variable": None})


def test_omitted_fields_are_not_validated_on_construction():
    """
    Test the hole that the two tests above do not cover

    An omitted field is never assigned, so it is never checked,
    and no amount of Pydantic configuration changes that for a table model.
    This is why the facets are NOT NULL in the database:
    catching the omission is the database's job, and only the database's.

    See the plan recorded against `model_config` in `schema.py`
    for closing this properly once we parse ESGF records.
    """
    # No exception, despite every facet being required.
    dataset = Dataset(id="only-an-id")

    assert dataset.variable is None


@pytest.mark.parametrize("column", FACET_COLUMNS)
def test_facet_columns_are_not_nullable(engine, column):
    """
    Test that a dataset can't be stored with a missing facet

    A dataset with no variable, or no experiment, isn't a partial record,
    it's a record we can't identify. The database should refuse it
    rather than leave us to discover it later.

    This also underpins `uq_dataset_facets`, which can only catch duplicates
    while every facet is NOT NULL, because SQL treats two NULLs as different
    values. Each case here is therefore load-bearing for
    [`test_facets_are_unique`][], not just a tidiness check.

    Note that only the column under test is missing;
    every other facet is filled in, so an `IntegrityError` here
    can only have come from this column.

    The facet is *omitted* rather than passed as `None` on purpose.
    Passing `None` explicitly is caught by Pydantic before the database
    ever sees it, which would make this a test of `validate_assignment`
    rather than of the constraint we actually care about here.
    """
    kwargs = {k: v for k, v in VALID_DATASET_KWARGS.items() if k != column}

    with Session(engine) as session:
        session.add(Dataset(**kwargs))

        with pytest.raises(IntegrityError):
            session.commit()
