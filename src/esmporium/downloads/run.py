"""
Downloading many files, falling back across mirrors

The entry points here know nothing about the database: they are handed
[DownloadRequest][(m).]s and hand back [DownloadResult][(m).]s. The engine-taking
entry point is [download][esmporium.db.download], in the database layer.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx
from tenacity import Retrying

from esmporium.datasets import DownloadOutcome
from esmporium.downloads.errors import DataNodeFetchError
from esmporium.downloads.fetching import (
    DEFAULT_CHUNK_BYTES,
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_READ_TIMEOUT_SECONDS,
    FetchOutcome,
    ProgressCallback,
    fetch_to_path,
)
from esmporium.downloads.health import DataNodeCallObserver
from esmporium.downloads.routes import Route
from esmporium.downloads.selectors import (
    DEFAULT_DATA_NODE_SELECTOR,
    DataNodeSelector,
    SelectorOfferedNoRouteError,
)

logger = logging.getLogger(__name__)

ResultCallback = Callable[["DownloadResult"], None]
"""
Told about each file as soon as it is finished

The seam the database layer uses to write one row per file as a run proceeds,
rather than at the end -- which is what makes a run killed on day two keep day
one.
"""

_PERMANENT_OUTCOMES = frozenset(
    {
        DownloadOutcome.MISSING,
        DownloadOutcome.NEEDS_CREDENTIALS,
        # The node serves a complete file that is not the one the index describes.
        # Retrying gets the same bytes; this needs the publication fixed, or the
        # search re-run to pick up corrected metadata.
        DownloadOutcome.SIZE_MISMATCH,
    }
)
"""
Outcomes which will not change by trying again later
"""


@dataclass(frozen=True)
class DownloadRequest:
    """
    One file to get: where from, where to, and what to check it against

    Deliberately carries no database rows, only ids.
    """

    routes: tuple[Route, ...]
    """
    Every route we know of for this file

    Handed to the selector, which decides the order and when to stop. May include
    routes which are not fetchable; they are filtered by
    [prepare_routes][esmporium.downloads.prepare_routes].
    """

    dest: Path
    """Where the finished file goes. See [esmporium.downloads.paths][]."""

    file_id: int | None = None
    """
    Which [File][esmporium.db.schema.File] this is, for the health records

    `None` is allowed so that this package can be used without a database at all.
    """

    expected_size: int | None = None
    """
    How many bytes the API published for this file, if it published one

    See [File.size][esmporium.db.schema.File.size]. Checked, not trusted.
    """

    checksum: str | None = None
    """
    The published digest, if there is one

    See [File.checksum][esmporium.db.schema.File.checksum].
    """

    checksum_type: str | None = None
    """
    Which algorithm `checksum` is of, if we know

    See [File.checksum_type][esmporium.db.schema.File.checksum_type].
    """

    @property
    def label(self) -> str:
        """
        Get something to call this file in a log line or an error

        Returns
        -------
        :
            The destination's name, which is the filename ESGF published
        """
        return self.dest.name


@dataclass(frozen=True)
class RouteFailure:
    """One route we tried for a file, and why it did not serve the bytes"""

    route: Route
    """The route that was tried"""

    outcome: DownloadOutcome
    """How it failed"""

    error: DataNodeFetchError
    """The failure itself, kept so a caller can inspect its cause"""


@dataclass(frozen=True)
class DownloadResult:
    """
    What came of trying to get one file

    Carries the request alongside the answer, because a result on its own does not say
    which file it is -- the same reason
    [LeafSearchOutcome][esmporium.search.LeafSearchOutcome] carries its query.
    """

    request: DownloadRequest
    """What was asked for"""

    fetch: FetchOutcome | None = None
    """
    What was fetched, or `None` if nothing was

    Present means the bytes are on disk at
    [FetchOutcome.path][esmporium.downloads.FetchOutcome.path] and whatever could be
    verified was.
    """

    failures: tuple[RouteFailure, ...] = ()
    """
    Every route that was tried and did not work, in the order they were tried
    """

    skipped: bool = False
    """
    Whether this file was already present and so was not fetched

    Distinct from a success with no failures, so that a run can report how much work
    it actually did.
    """

    @property
    def succeeded(self) -> bool:
        """
        Whether the file is now on disk

        Returns
        -------
        :
            `True` if it was fetched or was already there
        """
        return self.fetch is not None or self.skipped

    @property
    def give_up(self) -> bool:
        """
        Whether trying again later is pointless

        `True` only when every route we tried failed permanently -- all gone, or all
        needing credentials. A single timeout among them means the file is worth
        another go, so this stays `False`.

        Returns
        -------
        :
            `True` if nothing will change by retrying
        """
        if self.succeeded or not self.failures:
            return False

        return all(failure.outcome in _PERMANENT_OUTCOMES for failure in self.failures)

    @property
    def last_error(self) -> str | None:
        """
        Get the most recent failure, rendered for storing

        The format [Download.last_error][esmporium.db.schema.Download.last_error]
        expects.

        Returns
        -------
        :
            `"TypeName: message"` for the last route tried, or `None` if none failed
        """
        if not self.failures:
            return None

        error = self.failures[-1].error

        return f"{type(error).__name__}: {error}"


def download_file(  # noqa: PLR0913 - the keyword-only extras are deliberate injection seams
    client: httpx.Client,
    request: DownloadRequest,
    *,
    selector: DataNodeSelector = DEFAULT_DATA_NODE_SELECTOR,
    observer: DataNodeCallObserver | None = None,
    progress: ProgressCallback | None = None,
    max_routes: int | None = None,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    read_timeout: float = DEFAULT_READ_TIMEOUT_SECONDS,
    retrying: Retrying | None = None,
    resume: bool = True,
) -> DownloadResult:
    """
    Get one file, working through its mirrors until one serves it

    Parameters
    ----------
    client
        The HTTP client to fetch with. The credentials seam; see
        [fetch_to_path][esmporium.downloads.fetch_to_path].

    request
        The file to get

    selector
        Chooses which route to try at each attempt, and when to stop.
        See [DataNodeSelector][esmporium.downloads.DataNodeSelector].

    observer
        Told about every attempt against every route.
        See [esmporium.downloads.health][].

    progress
        Told how each transfer is going.

    max_routes
        The most mirrors to try before giving up on this file.

        `None` (the default) means try every route the selector offers. Set it to stop
        a file with thirty dead replicas from spending thirty timeouts proving it.

    chunk_bytes
        Passed through to [fetch_to_path][esmporium.downloads.fetch_to_path].

    connect_timeout
        Passed through to [fetch_to_path][esmporium.downloads.fetch_to_path].

    read_timeout
        Passed through to [fetch_to_path][esmporium.downloads.fetch_to_path].

    retrying
        Passed through to [fetch_to_path][esmporium.downloads.fetch_to_path], and so
        applies *within* one route. Moving to the next mirror is this function's job.

    resume
        Passed through to [fetch_to_path][esmporium.downloads.fetch_to_path].

    Returns
    -------
    :
        What came of it. A file no mirror served comes back with `fetch=None` and
        every failure recorded, rather than raising: one file failing is not a reason
        to abandon a run of ten thousand.

    Raises
    ------
    SelectorOfferedNoRouteError
        The selector offered nothing at all for this file, so nothing was tried.
        Asking for a download is asking for it to happen, so this is loud.
    """
    failures: list[RouteFailure] = []
    offered_anything = False
    attempt = 0

    while (route := selector(request.routes, attempt)) is not None:
        offered_anything = True

        if max_routes is not None and attempt >= max_routes:
            logger.debug(
                "giving up on %s after %d route(s)",
                request.label,
                attempt,
                extra={"filename": request.label, "max_routes": max_routes},
            )
            break

        try:
            fetched = fetch_to_path(
                client,
                route,
                request.dest,
                expected_size=request.expected_size,
                checksum=request.checksum,
                checksum_type=request.checksum_type,
                observer=observer,
                progress=progress,
                file_id=request.file_id,
                chunk_bytes=chunk_bytes,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
                retrying=retrying,
                resume=resume,
            )
        except DataNodeFetchError as exc:
            # Keep it and move on to the next mirror. Every failure is kept because
            # "it took four mirrors" is worth knowing, and because whether to give up
            # depends on *all* of them, not just the last.
            failures.append(RouteFailure(route=route, outcome=exc.outcome, error=exc))
            logger.debug(
                "%s failed for %s (%s), trying the next route",
                route.data_node,
                request.label,
                exc.outcome,
                extra={
                    "data_node": route.data_node,
                    "filename": request.label,
                    "outcome": str(exc.outcome),
                },
            )
            attempt += 1
            continue

        return DownloadResult(request=request, fetch=fetched, failures=tuple(failures))

    if not offered_anything:
        raise SelectorOfferedNoRouteError(request.routes, selector)

    return DownloadResult(request=request, fetch=None, failures=tuple(failures))


def download_files(  # noqa: PLR0913 - the keyword-only extras are deliberate injection seams
    requests: Iterable[DownloadRequest],
    *,
    client: httpx.Client | None = None,
    selector: DataNodeSelector = DEFAULT_DATA_NODE_SELECTOR,
    observer: DataNodeCallObserver | None = None,
    progress: ProgressCallback | None = None,
    max_routes: int | None = None,
    on_result: ResultCallback | None = None,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    read_timeout: float = DEFAULT_READ_TIMEOUT_SECONDS,
    retrying: Retrying | None = None,
    resume: bool = True,
) -> tuple[DownloadResult, ...]:
    """
    Get many files, one after another

    Parameters
    ----------
    requests
        The files to get, in the order to get them

    client
        The HTTP client to fetch with, shared across every file.
        If `None`, one is built for the call and closed at the end.

    selector
        Chooses which route to try for each file.

        Note that a *refreshing* selector
        ([build_refreshing_data_node_health_selector][esmporium.db.build_refreshing_data_node_health_selector])
        is what makes a long run learn as it goes: passed here, it re-reads health
        between files rather than being stuck with whatever was true when the run
        started.

    observer
        Told about every attempt against every route.

    progress
        Told how each transfer is going.

    max_routes
        The most mirrors to try per file.

    on_result
        Called with each [DownloadResult][(m).] as soon as that file is finished, so
        it can be acted on (e.g. recorded) the moment it is known rather than at the
        end of the run.

        This is the seam the database layer uses to write one row per file as the run
        proceeds, which is what makes a run killed on day two keep day one.

    chunk_bytes
        Passed through to [download_file][(m).download_file].

    connect_timeout
        Passed through to [download_file][(m).download_file].

    read_timeout
        Passed through to [download_file][(m).download_file].

    retrying
        Passed through to [download_file][(m).download_file].

    resume
        Passed through to [download_file][(m).download_file].

    Returns
    -------
    :
        One result per request, in the order the requests were given
    """
    owns_client = client is None
    client = client if client is not None else httpx.Client(follow_redirects=True)

    results: list[DownloadResult] = []
    try:
        for request in requests:
            result = download_file(
                client,
                request,
                selector=selector,
                observer=observer,
                progress=progress,
                max_routes=max_routes,
                chunk_bytes=chunk_bytes,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
                retrying=retrying,
                resume=resume,
            )
            results.append(result)
            if on_result is not None:
                on_result(result)
    finally:
        if owns_client:
            client.close()

    return tuple(results)


@dataclass(frozen=True)
class RunTotals:
    """
    What a run added up to

    A [DownloadResult][(m).] per file is the record; this is the headline. Kept as one
    type with one implementation so that a summary printed to a console and a summary
    written to a log cannot disagree.
    """

    n_files: int
    """How many files the run was asked for"""

    n_fetched: int
    """How many were downloaded"""

    n_skipped: int
    """How many were already present"""

    n_failed: int
    """How many the run did not get"""

    n_given_up: int
    """
    How many will not be got by trying again

    A subset of [n_failed][(c).n_failed]: every route for these files is gone, or
    needs credentials we do not have.
    """

    bytes_fetched: int
    """
    How many bytes moved

    Counts only what was transferred, so a resumed file contributes its remainder
    rather than its whole size.
    """

    def __str__(self) -> str:
        """
        Render as one line, for a log or a console

        Returns
        -------
        :
            A human-readable summary
        """
        given_up = f" ({self.n_given_up} for good)" if self.n_given_up else ""

        return (
            f"{self.n_files} files: {self.n_fetched} fetched, "
            f"{self.n_skipped} already here, {self.n_failed} failed{given_up}, "
            f"{self.bytes_fetched} bytes"
        )


def totals(results: Sequence[DownloadResult]) -> RunTotals:
    """
    Add a run's results up

    Parameters
    ----------
    results
        The run's results

    Returns
    -------
    :
        The totals

    Examples
    --------
    >>> print(totals([]))
    0 files: 0 fetched, 0 already here, 0 failed, 0 bytes
    """
    return RunTotals(
        n_files=len(results),
        n_fetched=sum(1 for r in results if r.fetch is not None),
        n_skipped=sum(1 for r in results if r.skipped),
        n_failed=sum(1 for r in results if not r.succeeded),
        n_given_up=sum(1 for r in results if r.give_up),
        bytes_fetched=sum(
            r.fetch.bytes_written for r in results if r.fetch is not None
        ),
    )
