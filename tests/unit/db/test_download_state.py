"""
Tests of the download queue and its state transitions
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import httpx
import pytest
from sqlmodel import Session, create_engine, select

from esmporium.datasets import AccessService, ContainerFormat, DownloadOutcome
from esmporium.db import (
    METADATA,
    DataAccess,
    DataNode,
    Dataset,
    DatasetVersion,
    Download,
    DownloadPlanItem,
    DownloadStatus,
    File,
    configure_sqlite_for_concurrency,
    download,
    download_summary,
    mark_running,
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


def store_one_dataset(engine, get_dataset_kwargs):
    """
    Put one dataset, one version, two files and their routes in a database

    A plain function rather than a fixture, so that a test which needs these rows in a
    *file-backed* database can have them too.

    Parameters
    ----------
    engine
        The database to write to

    get_dataset_kwargs
        The shared factory for a dataset row's columns

    Returns
    -------
    :
        The version's id and its files' ids
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


@pytest.fixture
def stored(engine, get_dataset_kwargs):
    """Put one dataset, one version, two files and their routes in the database"""
    return store_one_dataset(engine, get_dataset_kwargs)


@pytest.fixture
def stored_on_disk(tmp_path, get_dataset_kwargs):
    """
    Get a file-backed database holding the same rows, and the bodies its files serve

    Separate from [stored][(m).stored] because the shared in-memory engine keeps every
    session on one connection, which is not the shape a parallel run has. A file gives
    each worker its own connection, so their transactions really do have to take turns.
    """
    engine = configure_sqlite_for_concurrency(
        create_engine(f"sqlite:///{tmp_path / 'esmporium.db'}")
    )
    METADATA.create_all(engine)
    store_one_dataset(engine, get_dataset_kwargs)

    bodies = {"tas_a.nc": b"a" * 100, "tas_b.nc": b"b" * 200}
    # Real digests, so verification is exercised rather than skipped.
    with Session(engine) as session:
        for row in session.exec(select(File)).all():
            row.checksum = hashlib.sha256(bodies[row.filename]).hexdigest()
            row.checksum_type = "SHA256"
            session.add(row)
        session.commit()

    yield engine, bodies

    engine.dispose()


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


def test_a_skipped_file_is_recorded_as_done(engine, stored, tmp_path):
    """
    Already on disk is a finished download, not an unfinished one

    A skip carries no `FetchOutcome`, which is what a failure looks like too. Without
    the `skipped` branch the row would go back as `FAILED` and the next run would
    download a file we already have.
    """
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
        skipped=True,
    )

    with Session(engine) as session:
        row = record_result(session, result)
        session.commit()

        assert row.status == DownloadStatus.DONE
        # Nothing was fetched, so there is nothing to say about where from or how
        # it was verified.
        assert row.served_by_data_access_id is None
        assert row.last_error is None


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


def test_a_file_the_api_never_sized_is_taken_as_done_if_it_is_there(
    engine, stored, tmp_path
):
    """
    With no published size, being there is the best answer available

    The counterpart to the test above: the size check is skipped rather than failed.
    Failing it instead would re-download every unsized file on every run, forever.
    """
    with Session(engine) as session:
        plan = plan_downloads(session, QUERY, tmp_path)
        first = plan[0]

        # ESGF does not always publish a size, and then there is nothing to check
        # what is on disk against.
        unsized = session.get(File, first.file_id)
        unsized.size = None
        session.add(unsized)

        first.request.dest.parent.mkdir(parents=True, exist_ok=True)
        # Deliberately not the length the fixture published: with nothing to check
        # against, the length must not matter.
        first.request.dest.write_bytes(b"whatever landed here")
        session.add(
            Download(
                file_id=first.file_id,
                status=DownloadStatus.DONE,
                path=str(first.request.dest),
            )
        )
        session.commit()

        again = plan_downloads(session, QUERY, tmp_path)

    assert [item.request.dest.name for item in again] == ["tas_b.nc"]


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


def test_a_file_being_downloaded_right_now_says_so(engine, stored):
    """
    `RUNNING` is written before the transfer, so what is in flight is answerable

    The point of a non-terminal status: a run which is killed part way leaves its
    unfinished files saying `RUNNING`, rather than looking as though they were never
    started.
    """
    file_id = stored["file_ids"][0]

    with Session(engine) as session:
        row = mark_running(session, file_id)
        session.commit()

        assert row.status == DownloadStatus.RUNNING
        assert row.started_at is not None

        summary = download_summary(session)
        assert summary.n_by_status["running"] == 1
        # Still outstanding: it has not landed.
        assert summary.n_outstanding == 1


def test_a_running_file_moves_on_when_it_finishes(engine, stored, tmp_path):
    """`RUNNING` is a stage, not a resting place: a finished file is terminal"""
    file_id = stored["file_ids"][0]
    dest = tmp_path / "tas_a.nc"
    dest.write_bytes(b"a" * 100)

    with Session(engine) as session:
        mark_running(session, file_id)
        row = record_result(
            session,
            DownloadResult(
                request=DownloadRequest(routes=(), dest=dest, file_id=file_id),
                fetch=FetchOutcome(
                    path=dest,
                    bytes_written=100,
                    size_on_disk=100,
                    checksum_verified=True,
                    checksum_type_used="SHA256",
                    resumed=False,
                    route=Route(
                        url="https://esgf.example.org/tas_a.nc",
                        data_node="esgf.example.org",
                        service=AccessService.HTTPS,
                        container_format=ContainerFormat.NETCDF,
                        data_access_id=1,
                    ),
                ),
            ),
        )
        session.commit()

        assert row.status == DownloadStatus.DONE
        # Both timestamps survive, so how long the download took is answerable.
        assert row.started_at is not None
        assert row.finished_at is not None


def test_downloading_in_parallel_records_every_file(stored_on_disk, tmp_path):
    """
    Several workers writing one SQLite database all get their rows saved

    A file-backed database with each worker on its own connection, because that is
    what a real run has; the engine is configured so their writing transactions take
    turns (see `esmporium.db.configure_sqlite_for_concurrency`).
    """
    engine, bodies = stored_on_disk
    root = tmp_path / "downloads"
    barrier = threading.Barrier(2, timeout=5)

    def handler(request):
        # Both files have to be in flight together, so the test fails rather than
        # passes quietly if the workers turn out to be running one after another.
        barrier.wait()
        name = str(request.url).rsplit("/", 1)[-1]

        return httpx.Response(200, content=bodies[name])

    results = download(
        engine,
        QUERY,
        root,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        observer=record_data_node_calls(engine),
        max_workers=2,
    )

    assert len(results) == 2
    assert all(result.succeeded for result in results)

    with Session(engine) as session:
        rows = session.exec(select(Download)).all()
        assert len(rows) == 2
        assert all(row.status == DownloadStatus.DONE for row in rows)
        assert all(row.checksum_verified for row in rows)
        # Written by the `on_start` seam, from inside the worker.
        assert all(row.started_at is not None for row in rows)

        assert download_summary(session).n_outstanding == 0
