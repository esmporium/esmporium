"""
Downloads and data node health against a migrated database
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from esmporium.datasets import (
    DATASET_FACET_COLUMNS,
    AccessService,
    ContainerFormat,
    DownloadOutcome,
)
from esmporium.db import aggregate_data_node_health, record_data_node_calls
from esmporium.db.migrate import upgrade_to_head
from esmporium.db.schema import (
    DataAccess,
    DataNode,
    DataNodeCallRecord,
    Dataset,
    DatasetVersion,
    Download,
    DownloadStatus,
    File,
)
from esmporium.downloads import DataNodeCall

# Over 2^31, so a column that was a plain 32-bit integer would fail here. CMIP daily
# files routinely pass 2 GB, which is why `bytes_transferred` and `size_bytes` are
# BigInteger -- and SQLite would not have noticed, hence checking it against the
# migrated schema.
BIG = 3_000_000_000


def _a_file(session: Session) -> tuple[int, int, int]:
    """Store a dataset, a version, a data node and one file, and give back their ids."""
    facets = dict.fromkeys(DATASET_FACET_COLUMNS, "placeholder")
    facets.update(project="CMIP6", variable="tas", grid_label="gn")

    dataset = Dataset(id_project_specific="a-dataset", **facets)
    session.add(dataset)
    session.flush()

    version = DatasetVersion(
        dataset_id=dataset.id, version="20200101", is_latest=True, retracted=False
    )
    session.add(version)

    node = DataNode(data_node="esgf.example.org")
    session.add(node)
    session.flush()

    each = File(dataset_version_id=version.id, filename="tas.nc", size=BIG)
    session.add(each)
    session.flush()

    return each.id, node.id, version.id


@pytest.fixture
def migrated(engine):
    """Get an engine whose schema was built by the migrations, not by `create_all`"""
    upgrade_to_head(engine)

    return engine


def test_a_download_row_round_trips(migrated):
    """Every column the model declares survives the migrated schema"""
    with Session(migrated) as session:
        file_id, _, _ = _a_file(session)

        session.add(
            Download(
                file_id=file_id,
                status=DownloadStatus.DONE,
                path="/data/tas.nc",
                size_bytes=BIG,
                checksum_verified=True,
                checksum_type_used="SHA256",
                attempts=3,
                last_error=None,
                finished_at=datetime.datetime.now(datetime.timezone.utc),
            )
        )
        session.commit()

        row = session.exec(select(Download)).one()

    assert row.status == DownloadStatus.DONE
    # The 32-bit overflow case.
    assert row.size_bytes == BIG
    assert row.checksum_verified


def test_one_download_row_per_file_is_enforced_by_the_database(migrated):
    """
    The unique index really exists after migrating

    Identity here is the file, and a second row for the same file would mean two
    answers to "do we have it".
    """
    with Session(migrated) as session:
        file_id, _, _ = _a_file(session)
        session.add(Download(file_id=file_id, status=DownloadStatus.QUEUED))
        session.commit()

        session.add(Download(file_id=file_id, status=DownloadStatus.FAILED))
        with pytest.raises(IntegrityError):
            session.commit()


def test_a_data_node_call_record_round_trips(migrated):
    """Including a route reference and a byte count past the 32-bit limit"""
    with Session(migrated) as session:
        file_id, node_id, version_id = _a_file(session)

        access = DataAccess(
            dataset_version_id=version_id,
            file_id=file_id,
            data_node_id=node_id,
            service=AccessService.HTTPS,
            container_format=ContainerFormat.NETCDF,
            url="https://esgf.example.org/tas.nc",
            raw_json="{}",
        )
        session.add(access)
        session.flush()

        session.add(
            DataNodeCallRecord(
                data_node_id=node_id,
                file_id=file_id,
                data_access_id=access.id,
                url=access.url,
                http_method="GET",
                response_code=200,
                outcome=DownloadOutcome.SUCCESS,
                bytes_transferred=BIG,
                seconds=120.5,
                attempt_number=1,
                resumed=True,
            )
        )
        session.commit()

        row = session.exec(select(DataNodeCallRecord)).one()

    assert row.outcome == DownloadOutcome.SUCCESS
    assert row.bytes_transferred == BIG
    assert row.resumed


def test_the_observer_and_aggregation_work_against_migrations(migrated):
    """
    End to end through the real schema: observe, then read health back
    """
    observer = record_data_node_calls(migrated)

    observer(
        DataNodeCall(
            data_node="esgf.example.org",
            url="https://esgf.example.org/tas.nc",
            http_method="GET",
            response_code=200,
            outcome=DownloadOutcome.SUCCESS,
            error=None,
            bytes_transferred=BIG,
            seconds=100.0,
            attempt_number=1,
        )
    )

    health = aggregate_data_node_health(migrated, since=None)

    assert health["esgf.example.org"].n_success == 1
    assert health["esgf.example.org"].total_bytes == BIG
    # 3e9 bytes in 100s is 30 MB/s.
    assert health["esgf.example.org"].median_throughput_mb_per_s == pytest.approx(30.0)
