"""
Really downloading files from the live ESGF data nodes

Opt-in, because it needs somebody else's server to be up and really writes bytes:
run it with `--run-downloads-data`, or `make test-downloads`.

The target is CMIP7 `areacella` -- a time-invariant cell-area field, so one small 2D
array and the cheapest real CMIP7 file there is (52,593 bytes, measured 2026-11-02).
A download test that pulls gigabytes is one nobody runs.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, select

from esmporium.datasets import DownloadOutcome
from esmporium.db import (
    DataNodeCallRecord,
    Download,
    DownloadStatus,
    aggregate_data_node_health,
    build_result_processor,
    download,
    download_summary,
    plan_downloads,
    record_data_node_calls,
)
from esmporium.db.migrate import upgrade_to_head
from esmporium.query import QueryCMIP7
from esmporium.search import search_single_project
from esmporium.search.search.errors import NoFacadeAnsweredError

pytestmark = pytest.mark.downloads_data

# One dataset, one small file. `esm-scen7-vl` rather than the more obvious
# `piControl`, because `piControl r1i1p2f1`'s published size and checksum do not match
# the file its data node serves -- see
# `DownloadOutcome.SIZE_MISMATCH` for the measurements. Using it here would make this
# test fail for a reason that has nothing to do with our code.
SMALL_CMIP7 = QueryCMIP7(
    variable_id="areacella",
    source_id="CanESM5-1",
    experiment_id="esm-scen7-vl",
    variant_label="r1i1p2f1",
)


@pytest.fixture
def searched(engine):
    """
    Get a migrated database with one small CMIP7 dataset's files and routes in it

    Goes through the real search-and-ingest path rather than inserting rows.
    """
    upgrade_to_head(engine)

    with Session(engine) as session:
        outcome = search_single_project_or_skip(session)
        session.commit()

    if not outcome:
        pytest.skip("the CMIP7 search returned nothing to download")

    return engine


def search_single_project_or_skip(session) -> bool:
    """
    Search for the demo dataset, skipping if no endpoint answered

    Parameters
    ----------
    session
        The session to save results into

    Returns
    -------
    :
        Whether anything was found
    """
    try:
        outcome = search_single_project(
            SMALL_CMIP7, processor=build_result_processor(session)
        )
    except NoFacadeAnsweredError:
        # Every endpoint failing is somebody else's server being down, not a change
        # in our code, so this is a skip rather than a failure -- the same judgement
        # `tests/integration/conftest.py`'s `skip_or_fail` makes.
        return False

    return bool(outcome.parsed_docs)


def test_a_small_cmip7_file_really_downloads(searched, tmp_path):
    """
    The whole path, for real: plan, fetch, verify, record
    """
    root = tmp_path / "downloads"

    with Session(searched) as session:
        plan = plan_downloads(session, SMALL_CMIP7, root)

    assert plan, "the search found files but the plan is empty"

    results = download(
        searched,
        SMALL_CMIP7,
        root,
        observer=record_data_node_calls(searched),
    )

    assert results, "nothing was attempted"
    for result in results:
        assert result.succeeded, (
            f"{result.request.label} did not download: {result.last_error}"
        )
        assert result.fetch is not None

        # The bytes are where we said they would be, and are the right length.
        assert result.fetch.path.exists()
        assert result.fetch.path.stat().st_size == result.fetch.size_on_disk
        assert result.fetch.size_on_disk == result.request.expected_size

        # ESGF publishes a checksum for these, so it was really checked.
        assert result.fetch.checksum_verified
        assert result.fetch.checksum_type_used

        # Nothing left behind.
        assert not result.fetch.path.with_suffix(
            result.fetch.path.suffix + ".part"
        ).exists()

    # The derived path really is a DRS-shaped tree under the root.
    on_disk = [path for path in root.rglob("*") if path.is_file()]
    assert len(on_disk) == len(results)
    assert all("CMIP7" in path.parts for path in on_disk)
    assert all(any(part.startswith("v") for part in path.parts) for path in on_disk)


def test_the_database_records_what_was_downloaded(searched, tmp_path):
    """Rows for the files, rows for the attempts, and health that adds up"""
    root = tmp_path / "downloads"

    download(searched, SMALL_CMIP7, root, observer=record_data_node_calls(searched))

    with Session(searched) as session:
        rows = session.exec(select(Download)).all()
        assert rows
        for row in rows:
            assert row.status == DownloadStatus.DONE
            assert row.path is not None
            assert row.checksum_verified
            assert row.served_by_data_access_id is not None
            assert row.finished_at is not None

        summary = download_summary(session)
        assert summary.n_by_status["done"] == len(rows)
        assert summary.n_outstanding == 0

        attempts = session.exec(select(DataNodeCallRecord)).all()
        assert attempts
        assert all(a.outcome == DownloadOutcome.SUCCESS for a in attempts)
        assert all(a.response_code == 200 for a in attempts)
        assert sum(a.bytes_transferred for a in attempts) == sum(
            row.size_bytes or 0 for row in rows
        )

    health = aggregate_data_node_health(searched, since=None)
    assert health
    for each in health.values():
        assert each.failure_rate == 0.0
        # Real bytes in real time, so there is a real measurement.
        assert each.median_throughput_mb_per_s is not None
        assert each.median_throughput_mb_per_s > 0.0


def test_a_second_run_skips_what_is_already_here(searched, tmp_path):
    """
    The durable queue, end to end: downloading twice does not download twice
    """
    root = tmp_path / "downloads"

    first = download(searched, SMALL_CMIP7, root)
    assert first

    second = download(searched, SMALL_CMIP7, root)

    # Nothing was attempted the second time, because the plan was empty.
    assert second == ()

    with Session(searched) as session:
        assert download_summary(session).n_outstanding == 0


def test_a_parallel_run_gets_everything_a_serial_one_does(searched, tmp_path):
    """
    The same files, fetched several at a time, against the real data nodes

    The serial test above is kept alongside this one on purpose: if only one of the two
    paths breaks, which one it was should be obvious from which test failed.
    """
    root = tmp_path / "downloads"

    results = download(
        searched,
        SMALL_CMIP7,
        root,
        observer=record_data_node_calls(searched),
        max_workers=4,
    )

    assert results, "nothing was attempted"
    for result in results:
        assert result.succeeded, (
            f"{result.request.label} did not download: {result.last_error}"
        )
        assert result.fetch is not None
        assert result.fetch.path.exists()
        assert result.fetch.checksum_verified
        # Nothing half-written left behind by a worker.
        assert not result.fetch.path.with_suffix(
            result.fetch.path.suffix + ".part"
        ).exists()

    with Session(searched) as session:
        rows = session.exec(select(Download)).all()
        assert rows
        assert all(row.status == DownloadStatus.DONE for row in rows)
        # The `on_start` seam ran inside the workers.
        assert all(row.started_at is not None for row in rows)
        assert download_summary(session).n_outstanding == 0

        # Every attempt succeeded, so no node refused us under the cap.
        attempts = session.exec(select(DataNodeCallRecord)).all()
        assert attempts
        assert all(each.outcome == DownloadOutcome.SUCCESS for each in attempts)
