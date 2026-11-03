"""
Tests of the download queue and its state transitions
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest
from sqlmodel import Session, select

from esmporium.datasets import AccessService, ContainerFormat, DownloadOutcome
from esmporium.db import (
    DataAccess,
    DataNode,
    Dataset,
    DatasetVersion,
    Download,
    DownloadPlanItem,
    DownloadStatus,
    File,
    download,
    download_summary,
    plan_downloads,
    queue_downloads,
    record_data_node_calls,
    record_result,
    routes_for_file,
)
from esmporium.downloads import (
    DownloadRequest,
    DownloadResult,
    FetchOutcome,
    Route,
    RouteFailure,
)
from esmporium.downloads.errors import DataNodeTimeoutError, RouteMissingError
from esmporium.query import QueryCMIP6

ID_PS = "CMIP6.CMIP.MOHC.UKESM1-0-LL.historical.r1i1p1f2.Amon.tas.gn"


@pytest.fixture
def stored(engine, get_dataset_kwargs):
    """
    Put one dataset, one version, two files and their routes in the database
    """
    with Session(engine) as session:
        node = DataNode(data_node="esgf.example.org")
        session.add(node)
        session.flush()

        dataset = Dataset(
            **{
                **get_dataset_kwargs("tas"),
                "id_project_specific": ID_PS,
                "project": "CMIP6",
                "model": "UKESM1-0-LL",
                "experiment": "historical",
                "variant_label": "r1i1p1f2",
                "variable": "tas",
                "reporting_interval": "mon",
                "processing_id": "Amon",
                "institution": "MOHC",
                "grid_label": "gn",
            }
        )
        session.add(dataset)
        session.flush()

        version = DatasetVersion(
            dataset_id=dataset.id,
            version="20200623",
            is_latest=True,
            retracted=False,
        )
        session.add(version)
        session.flush()

        files = []
        for name, size in (("tas_a.nc", 100), ("tas_b.nc", 200)):
            each = File(
                dataset_version_id=version.id,
                filename=name,
                size=size,
                checksum="ab" * 32,
                checksum_type="SHA256",
            )
            session.add(each)
            session.flush()
            files.append(each)

            session.add(
                DataAccess(
                    dataset_version_id=version.id,
                    file_id=each.id,
                    data_node_id=node.id,
                    service=AccessService.HTTPS,
                    container_format=ContainerFormat.NETCDF,
                    url=f"https://esgf.example.org/{name}",
                    raw_json="{}",
                )
            )
            # An OPeNDAP route too, which is published and is not downloadable.
            session.add(
                DataAccess(
                    dataset_version_id=version.id,
                    file_id=each.id,
                    data_node_id=node.id,
                    service=AccessService.OPENDAP,
                    container_format=ContainerFormat.HTML,
                    url=f"https://esgf.example.org/{name}.html",
                    raw_json="{}",
                )
            )
        session.commit()

        return {
            "version_id": version.id,
            "file_ids": [each.id for each in files],
        }


QUERY = QueryCMIP6(experiment_id="historical", variable_id="tas")


def test_routes_for_a_file_keep_the_unfetchable_ones(engine, stored):
    """
    What the APIs published is a fact; filtering is a separate decision

    Keeping them is what lets a caller see that a file was published only over
    OPeNDAP, instead of it looking like the file has no routes at all.
    """
    with Session(engine) as session:
        routes = routes_for_file(session, stored["file_ids"][0])

    assert len(routes) == 2
    assert {route.service for route in routes} == {
        AccessService.HTTPS,
        AccessService.OPENDAP,
    }


def test_a_plan_finds_the_files_a_query_asks_for(engine, stored, tmp_path):
    """Query to datasets to versions to files to routes"""
    with Session(engine) as session:
        plan = plan_downloads(session, QUERY, tmp_path)

    assert [item.request.dest.name for item in plan] == ["tas_a.nc", "tas_b.nc"]
    # The derived path, not anything stored: `id_project_specific` dotted into
    # directories, with the version below it.
    assert plan[0].request.dest.parent.name == "v20200623"
    assert "UKESM1-0-LL" in plan[0].request.dest.parts
    # The facts needed to verify came along.
    assert plan[0].request.expected_size == 100
    assert plan[0].request.checksum_type == "SHA256"


def test_a_plan_for_a_query_matching_nothing_is_empty(engine, stored, tmp_path):
    """No datasets, no files, no error"""
    with Session(engine) as session:
        plan = plan_downloads(
            session, QueryCMIP6(experiment_id="nope", variable_id="nope"), tmp_path
        )

    assert plan == ()


def test_queueing_is_idempotent(engine, stored, tmp_path):
    """
    Queueing the same plan twice leaves one row per file

    Identity here is the file: we either have it or we do not.
    """
    with Session(engine) as session:
        plan = plan_downloads(session, QUERY, tmp_path)
        queue_downloads(session, plan)
        queue_downloads(session, plan)
        session.commit()

    with Session(engine) as session:
        rows = session.exec(select(Download)).all()

    assert len(rows) == 2
    assert all(row.status == DownloadStatus.QUEUED for row in rows)


def test_a_failed_file_is_re_queued_but_a_done_one_is_not(engine, stored, tmp_path):
    """
    Re-running picks up where it left off without undoing finished work
    """
    first, second = stored["file_ids"]
    with Session(engine) as session:
        session.add(Download(file_id=first, status=DownloadStatus.DONE, path="/x"))
        session.add(Download(file_id=second, status=DownloadStatus.FAILED))
        session.commit()

    with Session(engine) as session:
        plan = [
            DownloadPlanItem(
                request=DownloadRequest(routes=(), dest=Path("x"), file_id=file_id),
                file_id=file_id,
                dataset_version_id=stored["version_id"],
            )
            for file_id in (first, second)
        ]
        queue_downloads(session, plan)
        session.commit()

    with Session(engine) as session:
        rows = {row.file_id: row.status for row in session.exec(select(Download)).all()}

    assert rows[first] == DownloadStatus.DONE
    assert rows[second] == DownloadStatus.QUEUED


def test_a_successful_result_is_recorded(engine, stored, tmp_path):
    """Path, size, what was verified, and which mirror served it"""
    file_id = stored["file_ids"][0]
    dest = tmp_path / "tas_a.nc"
    route = Route(
        url="https://esgf.example.org/tas_a.nc",
        data_node="esgf.example.org",
        service=AccessService.HTTPS,
        container_format=ContainerFormat.NETCDF,
        data_access_id=99,
    )
    result = DownloadResult(
        request=DownloadRequest(routes=(route,), dest=dest, file_id=file_id),
        fetch=FetchOutcome(
            path=dest,
            bytes_written=100,
            size_on_disk=100,
            checksum_verified=True,
            checksum_type_used="SHA256",
            resumed=False,
            route=route,
        ),
    )

    with Session(engine) as session:
        row = record_result(session, result)
        session.commit()
        assert row.status == DownloadStatus.DONE
        assert row.path == str(dest)
        assert row.size_bytes == 100
        assert row.checksum_verified
        assert row.checksum_type_used == "SHA256"
        assert row.served_by_data_access_id == 99
        assert row.finished_at is not None


def test_a_retryable_failure_is_recorded_as_failed(engine, stored, tmp_path):
    """A timeout is worth another go, so the status says so"""
    file_id = stored["file_ids"][0]
    route = Route(
        url="https://esgf.example.org/tas_a.nc",
        data_node="esgf.example.org",
        service=AccessService.HTTPS,
        container_format=ContainerFormat.NETCDF,
    )
    result = DownloadResult(
        request=DownloadRequest(
            routes=(route,), dest=tmp_path / "tas_a.nc", file_id=file_id
        ),
        failures=(
            RouteFailure(
                route=route,
                outcome=DownloadOutcome.TIMEOUT,
                error=DataNodeTimeoutError("https://x/a.nc", "stalled"),
            ),
        ),
    )

    with Session(engine) as session:
        row = record_result(session, result)
        session.commit()
        # Read before the session closes: a committed row is expired, so touching
        # its attributes afterwards would need a session to refresh from.
        assert row.status == DownloadStatus.FAILED
        assert "DataNodeTimeoutError" in row.last_error


def test_a_permanent_failure_is_recorded_as_given_up(engine, stored, tmp_path):
    """
    Every mirror gone: a later run must leave this alone

    Which is only safe because the reason is recorded.
    """
    file_id = stored["file_ids"][0]
    route = Route(
        url="https://esgf.example.org/tas_a.nc",
        data_node="esgf.example.org",
        service=AccessService.HTTPS,
        container_format=ContainerFormat.NETCDF,
    )
    result = DownloadResult(
        request=DownloadRequest(
            routes=(route,), dest=tmp_path / "tas_a.nc", file_id=file_id
        ),
        failures=(
            RouteFailure(
                route=route,
                outcome=DownloadOutcome.MISSING,
                error=RouteMissingError("https://x/a.nc", "gone"),
            ),
        ),
    )

    with Session(engine) as session:
        row = record_result(session, result)
        session.commit()
        assert row.status == DownloadStatus.GIVEN_UP


def test_attempts_accumulate_across_runs(engine, stored, tmp_path):
    """
    Not overwritten by the current run's count
    """
    file_id = stored["file_ids"][0]
    route = Route(
        url="https://esgf.example.org/tas_a.nc",
        data_node="esgf.example.org",
        service=AccessService.HTTPS,
        container_format=ContainerFormat.NETCDF,
    )
    failure = RouteFailure(
        route=route,
        outcome=DownloadOutcome.TIMEOUT,
        error=DataNodeTimeoutError("https://x/a.nc", "stalled"),
    )
    result = DownloadResult(
        request=DownloadRequest(
            routes=(route,), dest=tmp_path / "tas_a.nc", file_id=file_id
        ),
        failures=(failure, failure),
    )

    with Session(engine) as session:
        record_result(session, result)
        session.commit()
    with Session(engine) as session:
        row = record_result(session, result)
        session.commit()
        assert row.attempts == 4


def test_recording_a_result_with_no_file_id_is_refused(tmp_path, engine):
    """
    Downloading without a database is fine; recording one that way is not

    The split is deliberate, so the error says which half the caller is in.
    """
    result = DownloadResult(
        request=DownloadRequest(routes=(), dest=tmp_path / "a.nc", file_id=None)
    )

    with Session(engine) as session, pytest.raises(ValueError, match="no file_id"):
        record_result(session, result)


def test_a_done_file_is_skipped_by_the_next_plan(engine, stored, tmp_path):
    """What `skip_done` is for: a second run does not re-download the archive"""
    with Session(engine) as session:
        plan = plan_downloads(session, QUERY, tmp_path)
        assert len(plan) == 2

        # Pretend the first one landed, and really put it on disk at the right size.
        first = plan[0]
        first.request.dest.parent.mkdir(parents=True, exist_ok=True)
        first.request.dest.write_bytes(b"x" * 100)
        session.add(
            Download(
                file_id=first.file_id,
                status=DownloadStatus.DONE,
                path=str(first.request.dest),
                size_bytes=100,
            )
        )
        session.commit()

        assert [
            item.request.dest.name for item in plan_downloads(session, QUERY, tmp_path)
        ] == ["tas_b.nc"]


def test_a_file_deleted_behind_our_back_is_planned_again(engine, stored, tmp_path):
    """
    The row alone is not trusted: the path has to still be there

    A download record is our note of what we did, not a snapshot of the filesystem.
    """
    with Session(engine) as session:
        plan = plan_downloads(session, QUERY, tmp_path)
        first = plan[0]
        session.add(
            Download(
                file_id=first.file_id,
                status=DownloadStatus.DONE,
                # Recorded as done, but nothing was ever written there.
                path=str(first.request.dest),
                size_bytes=100,
            )
        )
        session.commit()

        again = plan_downloads(session, QUERY, tmp_path)

    assert [item.request.dest.name for item in again] == ["tas_a.nc", "tas_b.nc"]


def test_a_wrong_sized_file_on_disk_is_planned_again(engine, stored, tmp_path):
    """Present but the wrong length is not the file we wanted"""
    with Session(engine) as session:
        plan = plan_downloads(session, QUERY, tmp_path)
        first = plan[0]
        first.request.dest.parent.mkdir(parents=True, exist_ok=True)
        first.request.dest.write_bytes(b"too short")
        session.add(
            Download(
                file_id=first.file_id,
                status=DownloadStatus.DONE,
                path=str(first.request.dest),
            )
        )
        session.commit()

        again = plan_downloads(session, QUERY, tmp_path)

    assert "tas_a.nc" in [item.request.dest.name for item in again]


def test_skip_done_false_plans_everything(engine, stored, tmp_path):
    """How to force a re-download, and it says which files are already here"""
    with Session(engine) as session:
        plan = plan_downloads(session, QUERY, tmp_path)
        first = plan[0]
        first.request.dest.parent.mkdir(parents=True, exist_ok=True)
        first.request.dest.write_bytes(b"x" * 100)
        session.add(
            Download(
                file_id=first.file_id,
                status=DownloadStatus.DONE,
                path=str(first.request.dest),
                size_bytes=100,
            )
        )
        session.commit()

        again = plan_downloads(session, QUERY, tmp_path, skip_done=False)

    assert len(again) == 2
    assert [item.already_here for item in again] == [True, False]


def test_the_summary_says_how_much_is_queued_and_broken(engine, stored, tmp_path):
    """The reporting the status column exists for"""
    first, second = stored["file_ids"]
    with Session(engine) as session:
        session.add(
            Download(
                file_id=first,
                status=DownloadStatus.DONE,
                path="/x",
                size_bytes=1_000,
            )
        )
        session.add(Download(file_id=second, status=DownloadStatus.FAILED))
        session.commit()

        summary = download_summary(session)

    assert summary.n_total == 2
    assert summary.n_by_status["done"] == 1
    assert summary.n_by_status["failed"] == 1
    assert summary.bytes_by_status["done"] == 1_000
    # A failure is retryable, so it is still outstanding.
    assert summary.n_outstanding == 1
    assert "2 files" in str(summary)


def test_given_up_files_are_not_counted_as_outstanding(engine, stored):
    """Nothing will change by asking again, so they are not work remaining"""
    first, second = stored["file_ids"]
    with Session(engine) as session:
        session.add(Download(file_id=first, status=DownloadStatus.GIVEN_UP))
        session.add(Download(file_id=second, status=DownloadStatus.QUEUED))
        session.commit()

        summary = download_summary(session)

    assert summary.n_outstanding == 1


def test_only_the_newest_version_is_planned_by_default(engine, stored, tmp_path):
    """
    "Download this data" means the current version, not its whole history

    Worth pinning deliberately: the live CMIP7 data this was developed against
    happens to publish exactly one version per dataset, so this path is not exercised
    by the demo and would otherwise be untested.
    """
    with Session(engine) as session:
        version = session.exec(select(DatasetVersion)).one()
        newer = DatasetVersion(
            dataset_id=version.dataset_id,
            version="20261101",
            is_latest=True,
            retracted=False,
        )
        session.add(newer)
        session.flush()
        session.add(
            File(
                dataset_version_id=newer.id,
                filename="tas_new.nc",
                size=300,
            )
        )
        session.commit()

        plan = plan_downloads(session, QUERY, tmp_path)

    # Only the new version's file, and under the new version's directory.
    assert [item.request.dest.name for item in plan] == ["tas_new.nc"]
    assert plan[0].request.dest.parent.name == "v20261101"


def test_every_version_can_be_planned_if_asked(engine, stored, tmp_path):
    """`latest_version_only=False` gets the lot, for a caller who wants the history"""
    with Session(engine) as session:
        version = session.exec(select(DatasetVersion)).one()
        newer = DatasetVersion(
            dataset_id=version.dataset_id,
            version="20261101",
            is_latest=True,
            retracted=False,
        )
        session.add(newer)
        session.flush()
        session.add(File(dataset_version_id=newer.id, filename="tas_new.nc", size=300))
        session.commit()

        plan = plan_downloads(session, QUERY, tmp_path, latest_version_only=False)

    assert sorted(item.request.dest.parent.name for item in plan) == [
        "v20200623",
        "v20200623",
        "v20261101",
    ]


def test_download_runs_the_whole_flow_offline(engine, stored, tmp_path):
    """
    Plan, queue, fetch, record -- through the real `download` entry point

    The live test covers this too, but it is opt-in, so without this the engine-taking
    entry point would be untested on an ordinary `make test`.
    """
    root = tmp_path / "downloads"
    bodies = {"tas_a.nc": b"a" * 100, "tas_b.nc": b"b" * 200}

    # Put the real digests on the stored rows, so verification is exercised rather
    # than skipped. The fixture's placeholder checksum would (correctly) fail.
    with Session(engine) as session:
        for row in session.exec(select(File)).all():
            row.checksum = hashlib.sha256(bodies[row.filename]).hexdigest()
            row.checksum_type = "SHA256"
            session.add(row)
        session.commit()

    def handler(request):
        name = str(request.url).rsplit("/", 1)[-1]
        return httpx.Response(200, content=bodies[name])

    client = httpx.Client(transport=httpx.MockTransport(handler))

    results = download(
        engine,
        QUERY,
        root,
        client=client,
        observer=record_data_node_calls(engine),
    )

    assert len(results) == 2
    assert all(result.succeeded for result in results)

    # The bytes are where the derived path says, with the published lengths.
    for result in results:
        assert result.fetch.path.read_bytes() == bodies[result.fetch.path.name]

    with Session(engine) as session:
        rows = {row.file_id: row for row in session.exec(select(Download)).all()}
        assert all(row.status == DownloadStatus.DONE for row in rows.values())
        assert all(row.path is not None for row in rows.values())
        assert all(row.checksum_verified for row in rows.values())
        assert all(row.checksum_type_used == "SHA256" for row in rows.values())
        # Which mirror served it, for provenance.
        assert all(row.served_by_data_access_id is not None for row in rows.values())

        assert download_summary(session).n_outstanding == 0

    # And a second run has nothing to do.
    assert download(engine, QUERY, root, client=client) == ()
