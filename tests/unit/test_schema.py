"""
Tests of our database schema
"""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, create_engine, select

from esmporium.db import DATASET_FACET_COLUMNS, METADATA, Dataset

VALID_DATASET_KWARGS = {
    # No `id`: it is a surrogate integer the database assigns. The row's real identity
    # is `(id_project_specific, variable)`.
    "id_project_specific": (
        "cmip5.output1.BCC.bcc-csm1-1.rcp45.mon.atmos.Amon.r1i1p1_tas"
    ),
    "project": "CMIP5",
    "model": "bcc-csm1-1",
    "institution": "BCC",
    "experiment": "rcp45",
    "variant_label": "r1i1p1",
    "variable": "tas",
    "reporting_interval": "mon",
    # CMIP5 doesn't have grid labels.
    # All data seems to be reported in native grids,
    # that might be experiment and realm specific,
    # hence the grid label we use below.
    "grid_label": "bcc-csm-1_rcp45_atmos",
    "processing_id": "Amon",
}
"""A dataset with every facet filled in, i.e. one the database should accept"""

FACET_COLUMNS = [
    "project",
    "model",
    "institution",
    "experiment",
    "variant_label",
    "variable",
    "reporting_interval",
    "grid_label",
    "processing_id",
]


@pytest.fixture
def engine():
    """
    Get engine for the database to use in the tests

    The schema is created straight from the models here, not from the migrations.
    Whether the migrations agree with the models is a separate question,
    tested separately.
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
    """
    assert "dataset" in METADATA.tables

    with Session(engine) as session:
        # Empty, ensures that the table exists
        assert session.exec(select(Dataset)).all() == []


def test_round_trip(engine):
    """
    Test that a dataset comes back out of the database as it went in

    This is important for types like datetimes, enums, paths.
    Those are the columns where the value that comes back
    is quietly not the value that went in.
    """
    with Session(engine) as session:
        dataset = Dataset(**VALID_DATASET_KWARGS)
        session.add(dataset)
        session.commit()
        dataset_id = dataset.id

    with Session(engine) as session:
        retrieved = session.get(Dataset, dataset_id)

    assert retrieved is not None
    # The surrogate id is assigned by the database; everything we put in comes back.
    assert isinstance(retrieved.id, int)
    for column, value in VALID_DATASET_KWARGS.items():
        assert getattr(retrieved, column) == value

    for column in VALID_DATASET_KWARGS:
        column_type = Dataset.model_fields[column].annotation
        assert isinstance(getattr(retrieved, column), column_type)


def test_natural_key_is_unique(engine):
    """
    Test that two datasets can't share `(id_project_specific, variable)`

    This is the row's real identity. Same native id AND same variable means the same
    dataset, so storing it twice is the contradiction we want caught. This is also
    what keeps two CMIP5 products (which differ in `id_project_specific`) apart.
    """
    with Session(engine) as session:
        session.add(Dataset(**VALID_DATASET_KWARGS))
        session.commit()

        # Same native id and same variable: the same dataset a second time.
        session.add(Dataset(**VALID_DATASET_KWARGS))

        with pytest.raises(
            IntegrityError,
            match=re.escape(
                "UNIQUE constraint failed: "
                "dataset.id_project_specific, dataset.variable"
            ),
        ):
            session.commit()


def test_same_id_project_specific_different_variable_is_allowed(engine):
    """
    Test that one CMIP5 ESGF dataset's variables can coexist

    A CMIP5 `master_id` bundles many variables, so every per-variable row we derive
    from it shares one `id_project_specific`. Those rows differ only in `variable`,
    and the database must accept them all.
    """
    with Session(engine) as session:
        session.add(Dataset(**VALID_DATASET_KWARGS))
        session.add(Dataset(**{**VALID_DATASET_KWARGS, "variable": "pr"}))
        session.commit()

        assert len(session.exec(select(Dataset)).all()) == 2


def test_facets_are_not_unique(engine):
    """
    Test that two datasets are allowed to describe the same combination of facets

    The same facets under a different native id (a different ESGF dataset) is allowed:
    the natural key is `(id_project_specific, variable)`, not the facets.
    """
    with Session(engine) as session:
        session.add(Dataset(**VALID_DATASET_KWARGS))
        session.commit()

        same_facets_different_native_id = {
            **VALID_DATASET_KWARGS,
            "id_project_specific": "a-different-id-project-specific",
        }
        session.add(Dataset(**same_facets_different_native_id))
        session.commit()

        assert len(session.exec(select(Dataset)).all()) == 2


def test_facet_columns_are_the_declared_facets():
    """
    Test that `DATASET_FACET_COLUMNS` lists every facet of a dataset

    `DATASET_FACET_COLUMNS` is written out by hand
    (see the note on it in `schema.py`),
    so this is what stops a facet being added to the model
    without being added there.

    Every column except the ID is a facet today.
    When that stops being true (e.g. when we record when we last saw a dataset),
    this test has to become a list of the columns that are not facets,
    rather than being deleted.

    This has be updated by hand every time.
    That is deliberate, not an accident.
    Changing our Dataset model is a big deal.
    We want to make sure we really think through changes.
    """
    columns_that_are_not_the_id = [
        column.name
        for column in Dataset.__table__.columns
        if column.name not in ("id", "id_project_specific")
    ]

    # This ensures that we make clear decisions about new columns
    assert sorted(DATASET_FACET_COLUMNS) == sorted(columns_that_are_not_the_id)
    # This makes sure that we test all the declared columns in src
    assert sorted(FACET_COLUMNS) == sorted(DATASET_FACET_COLUMNS), (
        "Update FACET_COLUMNS to match DATASET_FACET_COLUMNS"
    )


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
    This is worth pinning down, because it is a side effect of how SQLModel is written
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

    See the plan recorded underneath `model_config` in `schema.py`
    for closing this properly once we parse ESGF records.
    """
    # No exception, despite every facet being required.
    dataset = Dataset(id_project_specific="only-a-native-id")

    assert dataset.variable is None


# Every facet is required EXCEPT `grid_label`, which is nullable on purpose
# (CMIP5 has no grid label; see `Dataset.grid_label`). Omitting it is allowed, so it
# is not part of this NOT NULL test.
NOT_NULL_FACET_COLUMNS = [column for column in FACET_COLUMNS if column != "grid_label"]


@pytest.mark.parametrize("column", NOT_NULL_FACET_COLUMNS)
def test_facet_columns_are_not_nullable(engine, column):
    """
    Test that a dataset can't be stored with a missing facet
    """
    # Note that only the column under test is missing;
    # every other facet is filled in, so an `IntegrityError` here
    # can only have come from this column.
    #
    # The facet is *omitted* rather than passed as `None` on purpose.
    # Passing `None` explicitly is caught by Pydantic before the database
    # ever sees it, which would make this a test of `validate_assignment`
    # rather than of the constraint we actually care about here.
    kwargs = {k: v for k, v in VALID_DATASET_KWARGS.items() if k != column}

    with Session(engine) as session:
        session.add(Dataset(**kwargs))

        with pytest.raises(IntegrityError, match="NOT NULL constraint failed"):
            session.commit()
