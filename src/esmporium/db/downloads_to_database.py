"""
Turning a query into downloads, and recording what came of them
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from sqlmodel import Session, col, select

from esmporium.datasets import AccessService, ContainerFormat
from esmporium.db.database_catalogue import find_datasets
from esmporium.db.schema import (
    DataAccess,
    DataNode,
    Dataset,
    DatasetVersion,
    Download,
    DownloadStatus,
    File,
)
from esmporium.downloads import (
    DEFAULT_DATA_NODE_SELECTOR,
    DEFAULT_PER_DATA_NODE,
    DataNodeCallObserver,
    DataNodeSelector,
    DownloadRequest,
    DownloadResult,
    FileTarget,
    PathBuilder,
    ProgressCallback,
    Route,
    download_files,
    esmporium_layout,
    local_path,
)
from esmporium.requirements import VersionFilter

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from esmporium.query import QueryProtocol

logger = logging.getLogger(__name__)


def _utcnow() -> datetime.datetime:
    """
    Get the current time, as a timezone-aware UTC datetime

    Returns
    -------
    :
        Now, in UTC
    """
    return datetime.datetime.now(datetime.timezone.utc)


@dataclass(frozen=True)
class DownloadPlanItem:
    """
    One file a plan says to get, with everything needed to get it and record it
    """

    request: DownloadRequest
    """What to hand [download_files][esmporium.downloads.download_files]"""

    file_id: int
    """Which [File][esmporium.db.schema.File] this is"""

    dataset_version_id: int
    """Which version it belongs to, for reporting"""

    already_here: bool = False
    """
    Whether this file looks like it is already on disk

    A plan reports these rather than silently dropping them, so that a caller can see
    "forty files, thirty already here" instead of being handed ten and left to wonder.
    """


@dataclass(frozen=True)
class DownloadSummary:
    """
    How much is queued, done and broken

    What the status column on [Download][esmporium.db.schema.Download] exists for.
    """

    n_by_status: Mapping[str, int]
    """How many files are in each status"""

    bytes_by_status: Mapping[str, int]
    """
    How many bytes are accounted for in each status

    For [DONE][esmporium.db.schema.DownloadStatus.DONE] this is what is on disk. For
    anything else it is `0`, because nothing has landed -- so the outstanding *size*
    of a queue is not knowable from here. That is deliberate: it would mean trusting
    [File.size][esmporium.db.schema.File.size], which this layer only ever records
    and never trusts.
    """

    @property
    def n_total(self) -> int:
        """
        Get how many files are tracked at all

        Returns
        -------
        :
            The total across every status
        """
        return sum(self.n_by_status.values())

    @property
    def n_outstanding(self) -> int:
        """
        Get how many files are still to get

        Returns
        -------
        :
            How many files a further run would try
        """
        return sum(
            self.n_by_status.get(status.value, 0)
            for status in (
                DownloadStatus.QUEUED,
                DownloadStatus.RUNNING,
                DownloadStatus.FAILED,
            )
        )

    def __str__(self) -> str:
        """
        Render as one line, for a log or a console

        Returns
        -------
        :
            A human-readable summary
        """
        parts = ", ".join(
            f"{count} {status}" for status, count in sorted(self.n_by_status.items())
        )

        return f"{self.n_total} files ({parts or 'none'})"


def routes_for_file(session: Session, file_id: int) -> tuple[Route, ...]:
    """
    Get every route we know of for one file

    Parameters
    ----------
    session
        The session to read from

    file_id
        The file to find routes for

    Returns
    -------
    :
        Its routes, including ones which are not fetchable

        Unfetchable routes are **kept** rather than filtered here, because this
        function answers "what did the APIs publish", which is a fact. Deciding what
        can be downloaded is
        [prepare_routes][esmporium.downloads.prepare_routes]'s job, and keeping the
        two apart is what lets a caller see that a file was published only over
        OPeNDAP.
    """
    rows = session.exec(
        select(DataAccess, DataNode.data_node)
        .join(DataNode, col(DataAccess.data_node_id) == col(DataNode.id))
        .where(col(DataAccess.file_id) == file_id)
    ).all()

    return tuple(
        Route(
            url=access.url,
            data_node=data_node,
            service=AccessService(access.service),
            container_format=ContainerFormat(access.container_format),
            data_access_id=access.id,
        )
        for access, data_node in rows
    )


def _looks_present(path: Path, expected_size: int | None) -> bool:
    """
    Decide whether a file already on disk can be taken as the one we want

    Existence and size only -- **never a re-read of the bytes**. The prototype
    full-hashed every already-downloaded file, serially, before any download started,
    which on a large archive is hours of I/O on every run to re-learn what it
    already knew.

    Parameters
    ----------
    path
        Where the file should be

    expected_size
        How large the API said it is, if it said

    Returns
    -------
    :
        Whether to treat it as already here
    """
    if not path.exists():
        return False

    if expected_size is None:
        # Nothing to check against. Present is the best answer available, and
        # re-downloading every unsized file on every run would be worse.
        return True

    return path.stat().st_size == expected_size


def plan_downloads(  # noqa: PLR0913 - one argument per thing a plan has to be told
    session: Session,
    query: QueryProtocol,
    root: Path,
    *,
    versions: VersionFilter = VersionFilter.NOT_RETRACTED,
    layout: PathBuilder = esmporium_layout,
    latest_version_only: bool = True,
    skip_done: bool = True,
) -> tuple[DownloadPlanItem, ...]:
    """
    Work out which files a query asks for, and which of them we do not have

    Parameters
    ----------
    session
        The session to read from

    query
        What to download. The same query object a search takes, so "download what I
        searched for" needs no translation.

    root
        The directory to download into. See [esmporium.downloads.paths][].

    versions
        Which of a dataset's versions count. Passed to
        [find_datasets][esmporium.db.find_datasets].

    layout
        Where under `root` each file goes. See
        [PathBuilder][esmporium.downloads.PathBuilder].

    latest_version_only
        Whether to plan only the newest qualifying version of each dataset.

        `True` by default, because "download this data" almost always means the
        current version, and planning every version we have ever seen would quietly
        multiply a download by its history. Pass `False` to get the lot.

    skip_done
        Whether to leave out files we already have.

        A file is "already here" if its
        [Download][esmporium.db.schema.Download] row says
        [DONE][esmporium.db.schema.DownloadStatus.DONE] *and* the path still exists
        with the right size -- so a file deleted behind our back is noticed and
        planned again. `False` plans everything, which is how to force a re-download.

    Returns
    -------
    :
        One item per file, in a stable order (by version, then filename)

        Files already here are included with
        [already_here][esmporium.db.DownloadPlanItem.already_here] set when
        `skip_done` is `False`, and left out entirely when it is `True`.
    """
    entries = find_datasets(session, query, versions=versions)
    dataset_ids = [entry.id for entry in entries]
    if not dataset_ids:
        return ()

    version_rows = _versions_to_plan(
        session, dataset_ids, latest_version_only=latest_version_only
    )

    items: list[DownloadPlanItem] = []
    for version, id_project_specific in version_rows:
        files = session.exec(
            select(File)
            .where(col(File.dataset_version_id) == version.id)
            .order_by(col(File.filename))
        ).all()

        for each in files:
            if each.id is None:  # pragma: no cover - a stored row always has an id
                continue

            dest = local_path(
                root,
                FileTarget(
                    id_project_specific=id_project_specific,
                    version=version.version,
                    filename=each.filename,
                ),
                layout=layout,
            )
            present = _already_done(session, each.id, dest, each.size)
            if present and skip_done:
                continue

            items.append(
                DownloadPlanItem(
                    request=DownloadRequest(
                        routes=routes_for_file(session, each.id),
                        dest=dest,
                        file_id=each.id,
                        expected_size=each.size,
                        checksum=each.checksum,
                        checksum_type=each.checksum_type,
                    ),
                    file_id=each.id,
                    dataset_version_id=version.id if version.id is not None else -1,
                    already_here=present,
                )
            )

    return tuple(items)


def _versions_to_plan(
    session: Session, dataset_ids: Sequence[int], *, latest_version_only: bool
) -> tuple[tuple[DatasetVersion, str], ...]:
    """
    Get the versions to plan downloads for, with their datasets' project-specific ids

    Parameters
    ----------
    session
        The session to read from

    dataset_ids
        The datasets whose versions to consider

    latest_version_only
        Whether to keep only the newest version of each dataset

    Returns
    -------
    :
        Each version to plan, paired with its dataset's `id_project_specific`
    """
    rows = session.exec(
        select(DatasetVersion, Dataset.id_project_specific)
        .join(Dataset, col(DatasetVersion.dataset_id) == col(Dataset.id))
        .where(col(DatasetVersion.dataset_id).in_(dataset_ids))
        .order_by(col(DatasetVersion.dataset_id), col(DatasetVersion.version))
    ).all()

    if not latest_version_only:
        return tuple((version, id_ps) for version, id_ps in rows)

    # Keep the last row per dataset. The query is ordered by version, and ESGF
    # versions are dates or integers, so the last one sorted as a string is the
    # newest -- except for CMIP5 replicas, whose version strings can disagree across
    # data nodes (a date on one, `1` on another). `is_latest` is not used instead
    # because it is a snapshot of what ESGF said when we searched, and more than one
    # version of a dataset can carry it.
    newest: dict[int, tuple[DatasetVersion, str]] = {}
    for version, id_ps in rows:
        newest[version.dataset_id] = (version, id_ps)

    return tuple(newest.values())


def _already_done(
    session: Session, file_id: int, dest: Path, expected_size: int | None
) -> bool:
    """
    Decide whether a file is already downloaded

    Both halves have to agree: the database has to say we got it, *and* the file has
    to still be there. The row alone would miss a file deleted behind our back; the
    disk alone would re-verify files we already know about.

    Parameters
    ----------
    session
        The session to read from

    file_id
        The file to check

    dest
        Where it should be

    expected_size
        How large the API said it is, if it said

    Returns
    -------
    :
        Whether it can be taken as already here
    """
    row = session.exec(select(Download).where(col(Download.file_id) == file_id)).first()
    if row is None or row.status != DownloadStatus.DONE:
        return False

    return _looks_present(Path(row.path) if row.path else dest, expected_size)


def queue_downloads(
    session: Session, items: Iterable[DownloadPlanItem]
) -> tuple[Download, ...]:
    """
    Record that we intend to download these files

    Does not commit: the caller owns the transaction boundary, as everywhere else in
    this layer.

    Parameters
    ----------
    session
        The session to write to

    items
        The files to queue

    Returns
    -------
    :
        One row per item, flushed so their ids are assigned
    """
    rows: list[Download] = []
    for item in items:
        row = session.exec(
            select(Download).where(col(Download.file_id) == item.file_id)
        ).first()

        if row is None:
            row = Download(file_id=item.file_id, status=DownloadStatus.QUEUED)
        elif row.status != DownloadStatus.DONE:
            # Anything unfinished goes back in the queue. `DONE` is left alone so
            # that queueing a plan does not un-record work already finished.
            row.status = DownloadStatus.QUEUED

        session.add(row)
        rows.append(row)

    session.flush()

    return tuple(rows)


def mark_running(session: Session, file_id: int) -> Download:
    """
    Record that a file is being downloaded right now

    Written before the transfer starts, so that what a run is working on is a question
    the database can answer while the run is still going, and so that a run which is
    killed leaves its unfinished files saying
    [RUNNING][esmporium.db.schema.DownloadStatus.RUNNING] rather than looking as though
    they were never started.

    Does not commit; see [queue_downloads][(m).queue_downloads].

    Parameters
    ----------
    session
        The session to write to

    file_id
        The file whose download is starting

    Returns
    -------
    :
        The updated row, created if this file had none
    """
    row = session.exec(select(Download).where(col(Download.file_id) == file_id)).first()
    if row is None:
        row = Download(file_id=file_id, status=DownloadStatus.QUEUED)

    row.status = DownloadStatus.RUNNING
    row.started_at = _utcnow()

    session.add(row)
    session.flush()

    return row


def record_result(session: Session, result: DownloadResult) -> Download:
    """
    Record what came of trying to download one file

    Written per file rather than per run, which is what makes a run killed on day two
    keep day one.

    Does not commit; see [queue_downloads][(m).queue_downloads].

    Parameters
    ----------
    session
        The session to write to

    result
        What came of the file. Its `request.file_id` says which file.

    Returns
    -------
    :
        The updated row

    Raises
    ------
    ValueError
        `result` is for a request with no `file_id`, so there is no row to update.
        A download run outside the database is allowed (that is the point of the
        split), but recording one is not.
    """
    file_id = result.request.file_id
    if file_id is None:
        msg = (
            "Cannot record a download with no file_id: there is no row to update. "
            "This result came from a DownloadRequest built without one, which is "
            "allowed for a download outside the database but cannot be recorded."
        )
        raise ValueError(msg)

    row = session.exec(select(Download).where(col(Download.file_id) == file_id)).first()
    if row is None:
        row = Download(file_id=file_id, status=DownloadStatus.QUEUED)

    row.attempts += max(len(result.failures), 1)
    row.last_error = result.last_error
    row.finished_at = _utcnow()

    if result.fetch is not None:
        row.status = DownloadStatus.DONE
        row.path = str(result.fetch.path)
        row.size_bytes = result.fetch.size_on_disk
        row.checksum_verified = result.fetch.checksum_verified
        row.checksum_type_used = result.fetch.checksum_type_used
        row.served_by_data_access_id = result.fetch.route.data_access_id
    elif result.skipped:
        row.status = DownloadStatus.DONE
    else:
        row.status = (
            DownloadStatus.GIVEN_UP if result.give_up else DownloadStatus.FAILED
        )

    session.add(row)
    session.flush()

    return row


def download_summary(session: Session) -> DownloadSummary:
    """
    Get how much is queued, done and broken

    Parameters
    ----------
    session
        The session to read from

    Returns
    -------
    :
        The summary
    """
    rows = session.exec(select(Download)).all()

    n_by_status: dict[str, int] = {}
    bytes_by_status: dict[str, int] = {}
    for row in rows:
        status = str(row.status)
        n_by_status[status] = n_by_status.get(status, 0) + 1
        bytes_by_status[status] = bytes_by_status.get(status, 0) + (row.size_bytes or 0)

    return DownloadSummary(n_by_status=n_by_status, bytes_by_status=bytes_by_status)


def download(  # noqa: PLR0913 - one argument per thing a download has to be told
    engine: Engine,
    query: QueryProtocol,
    root: Path,
    *,
    selector: DataNodeSelector = DEFAULT_DATA_NODE_SELECTOR,
    versions: VersionFilter = VersionFilter.NOT_RETRACTED,
    layout: PathBuilder = esmporium_layout,
    latest_version_only: bool = True,
    client: httpx.Client | None = None,
    observer: DataNodeCallObserver | None = None,
    progress: ProgressCallback | None = None,
    max_routes: int | None = None,
    max_workers: int | None = None,
    per_data_node: int | None = DEFAULT_PER_DATA_NODE,
    requeue_passes: int = 1,
) -> tuple[DownloadResult, ...]:
    """
    Download everything a query asks for that we do not already have

    The whole flow in one call: plan, queue, fetch, record.

    Each file's row is written as that file starts and again as it finishes, each in its
    own transaction, so a run interrupted on day two keeps everything from day one and
    says which files it was part way through.

    Pass `max_workers > 1` to download several files at once; see
    [download_files][esmporium.downloads.download_files] for how that interacts with
    `per_data_node`.

    Parameters
    ----------
    engine
        The database to plan from and record into.

    query
        What to download.

    root
        The directory to download into.

    selector
        Chooses which data node to try for each file.

        To have a long run learn from its own experience, pass
        [build_refreshing_data_node_health_selector][esmporium.db.build_refreshing_data_node_health_selector]
        and wire `observer` to
        [record_data_node_calls][esmporium.db.record_data_node_calls]: the attempts
        this run records are what the next refresh reads.

    versions
        Which of a dataset's versions count. See [plan_downloads][(m).plan_downloads].

    layout
        Where under `root` each file goes.

    latest_version_only
        Whether to download only the newest qualifying version of each dataset.

    client
        The HTTP client to fetch with. **The credentials seam**; see
        [fetch_to_path][esmporium.downloads.fetch_to_path]. If `None`, an anonymous
        one is built for the call and closed at the end.

    observer
        Told about every attempt against every data node.

    progress
        Told how each transfer is going.

    max_routes
        The most mirrors to try per file.

    max_workers
        How many files to download at once. See
        [download_files][esmporium.downloads.download_files].

        On a shared SQLite database, note that the rows written per file take the write
        lock in turn (see [esmporium.db.configure_sqlite_for_concurrency][]), which is
        nothing beside a transfer but is why the engine has to be configured for it.

    per_data_node
        The most transfers to run at once against any one data node. See
        [download_files][esmporium.downloads.download_files].

    requeue_passes
        How many extra passes to make over files which failed for a reason that might
        not recur. See [download_files][esmporium.downloads.download_files].

    Returns
    -------
    :
        One result per file attempted, in the order they were planned

        Files already on disk are not attempted and so are not here; what the run as
        a whole holds is [download_summary][(m).download_summary].
    """
    with Session(engine) as session:
        plan = plan_downloads(
            session,
            query,
            root,
            versions=versions,
            layout=layout,
            latest_version_only=latest_version_only,
        )
        queue_downloads(session, plan)
        session.commit()

    logger.info(
        "planned %d file(s) to download into %s",
        len(plan),
        root,
        extra={"n_files": len(plan), "root": str(root)},
    )

    def on_start(request: DownloadRequest) -> None:
        # Every request a plan builds names its file, so this only guards against a
        # request from somewhere else: a download outside the database is allowed, but
        # there would be no row to mark.
        if request.file_id is None:  # pragma: no cover - a planned request has one
            return

        with Session(engine) as session:
            mark_running(session, request.file_id)
            session.commit()

    def on_result(result: DownloadResult) -> None:
        # A session per file, committed on its own: this is what makes the run
        # resumable, and it is what makes this safe to call from several workers at
        # once.
        with Session(engine) as session:
            record_result(session, result)
            session.commit()

    return download_files(
        (item.request for item in plan),
        client=client,
        selector=selector,
        observer=observer,
        progress=progress,
        max_routes=max_routes,
        on_start=on_start,
        on_result=on_result,
        max_workers=max_workers,
        per_data_node=per_data_node,
        requeue_passes=requeue_passes,
    )
