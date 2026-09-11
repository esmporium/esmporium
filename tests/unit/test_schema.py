"""
Tests of our database schema
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, create_engine, select

from esmporium.db import (
    DATASET_FACET_COLUMNS,
    METADATA,
    DataNode,
    Dataset,
    DatasetRawDoc,
    DatasetVersion,
    DatasetVersionDataNodeLink,
    RawDocVersionLink,
    UnhandledDatasetClashError,
    save_dataset,
)
from esmporium.db.schema import DATASET_IDENTITY_INDEX
from esmporium.search import DatasetFacets

VALID_DATASET_KWARGS = {
    # No `id`: it is a surrogate integer the database assigns. The row's real identity
    # is *every descriptive column* — `id_project_specific` plus all nine facets —
    # enforced by the `uq_dataset_identity` index (see `Dataset.__table_args__`).
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


# A dataset's identity is every column except the surrogate `id`: the ESGF-side
# `id_project_specific` plus all of "our columns" (the nine facets). The three tests
# below cover the three ways two datasets can relate on that identity, splitting our
# columns from the ESGF column.


def test_same_id_project_specific_differ_on_our_column_is_allowed(engine):
    """
    Two datasets sharing `id_project_specific` but differing on one of our columns

    This is allowed: they are genuinely different datasets. Here the differing column
    is `grid_label` (chosen so the case is not just the `variable` one below). A real
    instance is CMIP5, where one `master_id` bundles many variables.
    """
    with Session(engine) as session:
        save_dataset(session, Dataset(**{**VALID_DATASET_KWARGS, "grid_label": "gn"}))
        save_dataset(session, Dataset(**{**VALID_DATASET_KWARGS, "grid_label": "gr"}))
        session.commit()

        assert len(session.exec(select(Dataset)).all()) == 2


def test_same_id_project_specific_different_variable_is_allowed(engine):
    """
    Test that one CMIP5 ESGF dataset's variables can coexist

    A concrete instance of the case in
    `test_same_id_project_specific_differ_on_our_column_is_allowed`, where the differing
    column is `variable`: a CMIP5 `master_id` bundles many variables, so every
    per-variable row we derive from it shares one `id_project_specific`. Those rows
    differ only in `variable`, and the database must accept them all.
    """
    with Session(engine) as session:
        save_dataset(session, Dataset(**VALID_DATASET_KWARGS))
        save_dataset(session, Dataset(**{**VALID_DATASET_KWARGS, "variable": "pr"}))
        session.commit()

        assert len(session.exec(select(Dataset)).all()) == 2


def test_same_our_columns_differ_on_id_project_specific_is_allowed(engine):
    """
    Two datasets identical across all our columns but with different native ids

    This is allowed: the difference lives in a project-specific facet we do not model
    as a column, so it survives only in `id_project_specific` (and the raw JSON). Real
    instances: CMIP5 `product` (`output1` vs `output2`) and CMIP6 `activity_id`
    (`CMIP` vs `CFMIP`), both of which leave every one of our columns identical.
    """
    with Session(engine) as session:
        save_dataset(session, Dataset(**VALID_DATASET_KWARGS))
        same_our_columns_different_native_id = {
            **VALID_DATASET_KWARGS,
            "id_project_specific": "a-different-id-project-specific",
        }
        save_dataset(session, Dataset(**same_our_columns_different_native_id))
        session.commit()

        assert len(session.exec(select(Dataset)).all()) == 2


def test_identical_all_columns_raises_clash(engine):
    """
    Two datasets identical across our columns AND `id_project_specific` clash

    Nothing our model records tells them apart, so this is not a benign duplicate: it
    means the data differs in a facet we do not model, and we refuse it loudly rather
    than silently keep one. This uses the CMIP5 shape (`grid_label=None`) on purpose:
    a plain UNIQUE would let it through because SQLite treats NULLs as distinct, so it
    is exactly the case the `coalesce(grid_label, '')` identity index exists to catch.
    """
    cmip5_shape = {**VALID_DATASET_KWARGS, "grid_label": None}
    with Session(engine) as session:
        save_dataset(session, Dataset(**cmip5_shape))
        session.commit()

        with pytest.raises(UnhandledDatasetClashError):
            save_dataset(session, Dataset(**cmip5_shape))


def test_identity_index_name_matches_constant():
    """The constant naming the identity index must be a real index on the table.

    `save_dataset` recognises a clash by matching this exact name in SQLite's
    `IntegrityError` text, so the name in `__table_args__` and the
    `DATASET_IDENTITY_INDEX` constant it (and `save_dataset`) share must not drift.
    Renaming the index in the model without updating the constant would slip past the
    clash tests only if SQLite happened to still report the old name; this pins the two
    together directly.
    """
    assert DATASET_IDENTITY_INDEX in {ix.name for ix in Dataset.__table__.indexes}


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


def test_dataset_facets_mirror_dataset_columns():
    """`DatasetFacets` declares exactly `Dataset`'s facets plus `id_project_specific`.

    This pins the `search` <-> `db` coupling in one assertion. The facade parses results
    into [`DatasetFacets`][esmporium.search.result_parsing.DatasetFacets], and the `db`
    layer builds a `Dataset` from each one, so if a facet is added to `Dataset` without
    adding it to `DatasetFacets` (or vice versa), parsing and storage silently fall out
    of step. This fails by name the moment they diverge -- a faster, sharper signal than
    an ingest blowing up on a NOT NULL column.
    """
    assert set(DatasetFacets.model_fields) == {
        "id_project_specific",
        *DATASET_FACET_COLUMNS,
    }


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


# --- The version / node / raw-doc tables ---------------------------------------
#
# These pin the UNIQUE constraints the ingestion path relies on: its get-or-create
# helpers stay idempotent (re-ingesting a search reuses rows) only because these pairs
# cannot be duplicated. Foreign keys are NOT exercised here: SQLite does not enforce
# them without `PRAGMA foreign_keys=ON`, which we do not set, so these tests use plain
# integer ids and assert only the uniqueness rules. The end-to-end behaviour over real
# rows is covered in `tests/unit/db/test_results_round_trip.py`.


def _version(dataset_id: int, version: str) -> DatasetVersion:
    """A version row with the required snapshot flags filled in."""
    return DatasetVersion(
        dataset_id=dataset_id,
        version=version,
        is_latest=True,
        retracted=False,
    )


def test_edition_is_unique_per_dataset_and_version(engine):
    """One edition per `(dataset_id, version)`; a second identical pair is refused."""
    with Session(engine) as session:
        dataset = Dataset(**VALID_DATASET_KWARGS)
        session.add(dataset)
        session.commit()

        session.add(_version(dataset.id, "20200101"))
        session.commit()

        session.add(_version(dataset.id, "20200101"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_same_version_string_under_two_datasets_is_allowed(engine):
    """`version` is unique only *within* a dataset — it is decoupled from the bundle.

    This is the point of the decoupling: two per-variable CMIP5 datasets share one
    bundle version (`20200101`), and both must be storable.
    """
    with Session(engine) as session:
        tas = Dataset(**VALID_DATASET_KWARGS)
        pr = Dataset(**{**VALID_DATASET_KWARGS, "variable": "pr"})
        session.add(tas)
        session.add(pr)
        session.commit()

        session.add(_version(tas.id, "20200101"))
        session.add(_version(pr.id, "20200101"))
        session.commit()  # no clash: the pairs differ on dataset_id

        assert len(session.exec(select(DatasetVersion)).all()) == 2


def test_data_node_is_unique(engine):
    """One row per distinct data node (there are only a handful across ESGF)."""
    with Session(engine) as session:
        session.add(DataNode(data_node="esgf.nci.org.au"))
        session.commit()

        session.add(DataNode(data_node="esgf.nci.org.au"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_edition_node_link_pair_is_unique(engine):
    """The same (edition, node) link twice is refused, so recording it again reuses."""
    with Session(engine) as session:
        session.add(DatasetVersionDataNodeLink(dataset_version_id=1, data_node_id=1))
        session.commit()

        session.add(DatasetVersionDataNodeLink(dataset_version_id=1, data_node_id=1))
        with pytest.raises(IntegrityError):
            session.commit()


def test_edition_node_link_is_many_to_many(engine):
    """A node hosts many editions, an edition many nodes: (1,1) (1,2) (2,1) coexist."""
    with Session(engine) as session:
        session.add(DatasetVersionDataNodeLink(dataset_version_id=1, data_node_id=1))
        session.add(DatasetVersionDataNodeLink(dataset_version_id=1, data_node_id=2))
        session.add(DatasetVersionDataNodeLink(dataset_version_id=2, data_node_id=1))
        session.commit()

        assert len(session.exec(select(DatasetVersionDataNodeLink)).all()) == 3


def test_raw_doc_esgf_id_is_unique(engine):
    """The exact JSON is stored once per source document, keyed by `esgf_doc_id`."""
    with Session(engine) as session:
        session.add(
            DatasetRawDoc(
                esgf_doc_id="instance_id|node",
                raw_json="{}",
                raw_docs_format_tag="solr",
            )
        )
        session.commit()

        session.add(
            DatasetRawDoc(
                esgf_doc_id="instance_id|node",
                raw_json="{}",
                raw_docs_format_tag="solr",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_raw_doc_edition_link_pair_is_unique(engine):
    """One (document, edition) pair; linking the same document to it twice is a dupe."""
    with Session(engine) as session:
        session.add(RawDocVersionLink(raw_doc_id=1, dataset_version_id=1))
        session.commit()

        session.add(RawDocVersionLink(raw_doc_id=1, dataset_version_id=1))
        with pytest.raises(IntegrityError):
            session.commit()


def test_one_document_can_describe_many_editions(engine):
    """A CMIP5 document bundles many per-variable editions: one raw_doc, many links."""
    with Session(engine) as session:
        session.add(RawDocVersionLink(raw_doc_id=1, dataset_version_id=1))
        session.add(RawDocVersionLink(raw_doc_id=1, dataset_version_id=2))
        session.commit()

        assert len(session.exec(select(RawDocVersionLink)).all()) == 2
