"""
Header-read records and branch times against a migrated database

`create_all` builds the schema from the models, so it can only ever agree with them.
These tests build it from the migrations instead, which is what a user's database
really goes through.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, select

from esmporium.datasets import (
    DATASET_FACET_COLUMNS,
    HeaderReadOutcome,
)
from esmporium.db.migrate import upgrade_to_head
from esmporium.db.schema import (
    DataNode,
    Dataset,
    DatasetVersion,
    DatasetVersionParent,
    File,
    HeaderReadCallRecord,
    ParentInfoSource,
    ParentResolution,
)


@pytest.fixture
def migrated(engine):
    """Get an engine whose schema was built by the migrations, not by `create_all`"""
    upgrade_to_head(engine)

    return engine


def _a_file(session: Session) -> tuple[int, int, int]:
    """Store a dataset, a version, a data node and one file; give back their ids."""
    facets = dict.fromkeys(DATASET_FACET_COLUMNS, "placeholder")
    facets.update(project="CMIP5", variable="tas", model="bcc-csm1-1")

    dataset = Dataset(id_project_specific="a-dataset", **facets)
    session.add(dataset)
    session.flush()

    version = DatasetVersion(
        dataset_id=dataset.id, version="20120101", is_latest=True, retracted=False
    )
    session.add(version)

    node = DataNode(data_node="esgf.example.org")
    session.add(node)
    session.flush()

    each = File(dataset_version_id=version.id, filename="tas.nc", size=368702896)
    session.add(each)
    session.flush()

    return each.id, node.id, version.id


def test_a_header_read_call_row_round_trips(migrated):
    """Every column the model declares survives the migrated schema"""
    with Session(migrated) as session:
        file_id, node_id, _ = _a_file(session)

        session.add(
            HeaderReadCallRecord(
                data_node_id=node_id,
                file_id=file_id,
                url="https://dap.esgf.example.org/thredds/fileServer/tas.nc",
                http_method="GET",
                response_code=206,
                outcome=HeaderReadOutcome.SUCCESS,
                error=None,
                bytes_requested=65536,
                bytes_transferred=6364,
                seconds=0.42,
                attempt_number=1,
                range_honoured=True,
            )
        )
        session.commit()

        row = session.exec(select(HeaderReadCallRecord)).one()

    assert row.outcome == HeaderReadOutcome.SUCCESS
    assert row.bytes_requested == 65536
    assert row.bytes_transferred == 6364
    assert row.range_honoured
    assert row.created_at is not None


def test_a_node_that_ignored_the_range_is_recorded_as_such(migrated):
    """
    The `200`-with-the-whole-file case survives a round trip

    Worth a test of its own because it is the one piece of information this table
    holds that the download one does not, and a column that silently defaulted to
    `True` would hide exactly the nodes it exists to find.
    """
    with Session(migrated) as session:
        file_id, node_id, _ = _a_file(session)

        session.add(
            HeaderReadCallRecord(
                data_node_id=node_id,
                file_id=file_id,
                url="https://dap.esgf.example.org/thredds/fileServer/tas.nc",
                http_method="GET",
                response_code=200,
                outcome=HeaderReadOutcome.SUCCESS,
                bytes_requested=65536,
                bytes_transferred=65536,
                seconds=1.5,
                attempt_number=1,
                range_honoured=False,
            )
        )
        session.commit()

        row = session.exec(select(HeaderReadCallRecord)).one()

    assert not row.range_honoured
    assert row.response_code == 200


def test_a_failed_read_records_no_response_code(migrated):
    """
    `None` is how "the node never spoke" is told apart from "the node said no"
    """
    with Session(migrated) as session:
        file_id, node_id, _ = _a_file(session)

        session.add(
            HeaderReadCallRecord(
                data_node_id=node_id,
                file_id=file_id,
                url="https://dap.esgf.example.org/thredds/fileServer/tas.nc",
                http_method="GET",
                response_code=None,
                outcome=HeaderReadOutcome.HOST_FAULT,
                error="HeaderNodeHostFaultError: could not reach the data node",
                bytes_requested=65536,
                bytes_transferred=0,
                seconds=30.0,
                attempt_number=1,
            )
        )
        session.commit()

        row = session.exec(select(HeaderReadCallRecord)).one()

    assert row.response_code is None
    assert row.outcome == HeaderReadOutcome.HOST_FAULT
    assert not HeaderReadOutcome(row.outcome).is_worth_retrying


def test_branch_times_round_trip_on_a_parent_assertion(migrated):
    """
    The two columns a file header fills in and no search document does
    """
    with Session(migrated) as session:
        _, _, version_id = _a_file(session)

        session.add(
            DatasetVersionParent(
                dataset_version_id=version_id,
                source=ParentInfoSource.FILE_HEADER,
                resolution=ParentResolution.UNRESOLVED,
                parent_project="CMIP6",
                parent_model="TaiESM1",
                parent_experiment="piControl",
                parent_variant_label="r1i1p1f1",
                parent_time_units="days since 0701-01-01",
                branch_time_in_parent=438000.0,
                branch_time_in_child=0.0,
            )
        )
        session.commit()

        row = session.exec(select(DatasetVersionParent)).one()

    assert row.branch_time_in_parent == 438000.0
    assert row.branch_time_in_child == 0.0


def test_a_branch_time_of_zero_is_not_the_same_as_no_branch_time(migrated):
    """
    `0.0` is a value a header really publishes, so it must not read back as `None`

    A child usually starts at its own time zero, so `branch_time_in_child` is `0.0`
    far more often than it is anything else -- and a CMIP5 `branch_time` of `0.0` is
    one of the five encodings that attribute carries. A column that could not tell
    those from "the source said nothing" would lose the distinction every time it
    mattered.
    """
    with Session(migrated) as session:
        _, _, version_id = _a_file(session)

        session.add(
            DatasetVersionParent(
                dataset_version_id=version_id,
                source=ParentInfoSource.FILE_HEADER,
                resolution=ParentResolution.UNRESOLVED,
                parent_experiment="piControl",
                branch_time_in_parent=0.0,
                branch_time_in_child=None,
            )
        )
        session.commit()

        row = session.exec(select(DatasetVersionParent)).one()

    assert row.branch_time_in_parent == 0.0
    assert row.branch_time_in_parent is not None
    assert row.branch_time_in_child is None


def test_the_migrated_schema_matches_the_models(migrated, get_pending_changes):
    """
    Nothing is left for alembic to do, so the migration and the models agree
    """
    assert get_pending_changes(migrated) == []


def test_an_outcome_read_back_is_a_plain_string(migrated):
    """
    The gotcha the column's docstring warns about, pinned

    `outcome` is stored as the enum's *value*, so a row read out of the database
    carries a `str`. It compares equal to the enum member -- which is what makes `==`
    safe -- but it is not that member, so `is` fails and so does reaching for a
    property. Anything wanting the behaviour has to rebuild the member.
    """
    with Session(migrated) as session:
        file_id, node_id, _ = _a_file(session)

        session.add(
            HeaderReadCallRecord(
                data_node_id=node_id,
                file_id=file_id,
                url="https://dap.esgf.example.org/thredds/fileServer/tas.nc",
                http_method="GET",
                outcome=HeaderReadOutcome.TIMEOUT,
                bytes_requested=65536,
                seconds=30.0,
                attempt_number=1,
            )
        )
        session.commit()

        row = session.exec(select(HeaderReadCallRecord)).one()

    assert row.outcome == HeaderReadOutcome.TIMEOUT
    assert row.outcome is not HeaderReadOutcome.TIMEOUT
    assert not hasattr(row.outcome, "is_worth_retrying")
    # Which is why a reader that wants the behaviour rebuilds the member.
    assert HeaderReadOutcome(row.outcome).stops_this_data_node
