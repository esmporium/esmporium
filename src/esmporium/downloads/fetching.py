"""
Fetching one file's bytes from one data node
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx
from tenacity import Retrying

from esmporium.datasets import DownloadOutcome
from esmporium.downloads.errors import (
    ChecksumMismatchError,
    DataNodeBlockedError,
    DataNodeFetchError,
    DataNodeHostFaultError,
    DataNodeTimeoutError,
    DataNodeTransportError,
    NeedsCredentialsError,
    RouteMissingError,
    ShortReadError,
    SizeMismatchError,
    UnfetchableRouteError,
)
from esmporium.downloads.health import DataNodeCall, DataNodeCallObserver
from esmporium.downloads.retry import build_download_retrying
from esmporium.downloads.routes import Route, is_fetchable
from esmporium.http import log_request_as_url_and_curl

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_BYTES = 1 << 20
"""
How much to read, write and hash at a time (1 MiB)

Big enough that the per-chunk overhead is irrelevant beside the I/O, small enough that
a stalled transfer is noticed within one read timeout rather than one whole file.
"""

DEFAULT_CONNECT_TIMEOUT_SECONDS = 30.0
"""How long to wait for a data node to accept a connection"""

DEFAULT_READ_TIMEOUT_SECONDS = 60.0
"""
How long to wait for the *next chunk*, not for the whole transfer

A wall-clock deadline would fail exactly the downloads we most want to finish, because
a large file legitimately takes a long time. What is wrong is a transfer that has
stalled, which is what a per-read deadline catches.
"""

_PARTIAL_CONTENT = 206
_RANGE_NOT_SATISFIABLE = 416
_MISSING_STATUSES = frozenset({404, 410})
_BLOCK_STATUSES = frozenset({403, 429, 503})
_UNAUTHORISED = 401
_CLIENT_ERROR_FLOOR = 400


class Hasher(Protocol):
    """
    The part of a `hashlib` hasher this module uses

    Declared rather than imported because the type `hashlib.new` returns is
    `hashlib._Hash`, which is private and so not a name we are allowed to depend on.
    Only two methods are needed, so saying which two is clearer than an `Any`.
    """

    def update(self, data: bytes, /) -> None:
        """
        Add more bytes to the digest

        Parameters
        ----------
        data
            The bytes to add
        """
        ...

    def hexdigest(self) -> str:
        """
        Get the digest of everything added so far, as lower-case hex

        Returns
        -------
        :
            The digest
        """
        ...


ProgressCallback = Callable[[int, int | None], None]
"""
Told how a transfer is going, as `(bytes_so_far, bytes_expected)`

`bytes_expected` is `None` when the published size is not known. Called roughly once
per chunk,.

This exists because a multi-day download that prints nothing is indistinguishable
from one that has hung.

When several files are being fetched at once, this is called from each of their threads,
and a call says only how far *a* transfer has got -- not which file it is. So an
implementation which writes anywhere shared has to be safe to call from several threads,
and needs its own way of telling the transfers apart (the thread is one, because a
worker fetches one file at a time).
"""


@dataclass(frozen=True)
class FetchOutcome:
    """
    What came of fetching one route

    Only ever describes a *success*: a failure raises, carrying its own
    [DownloadOutcome][esmporium.datasets.DownloadOutcome], because the caller has to
    decide whether to try another mirror and an outcome object it might forget to
    check is a worse way to tell it.
    """

    path: Path
    """Where the bytes were written"""

    bytes_written: int
    """
    How many bytes this call transferred

    Counts only what *this* call moved, so a resumed fetch does not re-count what was
    already on disk. Compare [size_on_disk][(c).size_on_disk] for the whole file.
    """

    size_on_disk: int
    """How large the finished file is"""

    checksum_verified: bool
    """
    Whether the bytes were checked against a published digest and matched

    `False` means there was nothing to check against, never that a check failed -- a
    failed check raises
    [ChecksumMismatchError][esmporium.downloads.ChecksumMismatchError].
    """

    checksum_type_used: str | None
    """Which algorithm was run, or `None` if none was"""

    resumed: bool
    """Whether this call continued a part-downloaded file"""

    route: Route
    """The route that served the bytes, so a caller can record which mirror won"""


def part_path(dest: Path) -> Path:
    """
    Get the temporary path a download is streamed to before being put in place

    Beside the destination rather than in a temporary directory, so that the rename
    into place is within one filesystem and therefore atomic, and so that an
    interrupted download can be resumed on the next run by finding it.

    Parameters
    ----------
    dest
        Where the finished file will go

    Returns
    -------
    :
        The path to stream to

    Examples
    --------
    >>> from pathlib import Path
    >>> part_path(Path("/data/tas.nc")).name
    'tas.nc.part'
    """
    return dest.with_name(f"{dest.name}.part")


def _hasher(checksum_type: str | None) -> Hasher | None:
    """
    Get a hasher for a published checksum type, or `None` if we cannot make one

    Parameters
    ----------
    checksum_type
        The algorithm name, as
        [File.checksum_type][esmporium.db.schema.File.checksum_type] spells it (upper
        case), or `None`

    Returns
    -------
    :
        A new hasher, or `None` if `checksum_type` is `None` or not one we have
    """
    if checksum_type is None:
        return None

    try:
        return hashlib.new(checksum_type.lower())
    except ValueError:
        # An algorithm this Python does not have. Downloading without verifying is
        # better than refusing to download, and `checksum_verified=False` says so.
        logger.debug(
            "cannot verify with %r: no such hash algorithm here",
            checksum_type,
            extra={"checksum_type": checksum_type},
        )
        return None


def _classify_status(url: str, status: int, response: httpx.Response) -> None:
    """
    Turn a non-success status code into the matching error, or return

    Parameters
    ----------
    url
        The URL that was requested

    status
        The status code the data node answered with

    response
        The response, read for a `Retry-After` header

    Raises
    ------
    RouteMissingError
        404 or 410

    NeedsCredentialsError
        401

    DataNodeBlockedError
        403, 429 or 503

    DataNodeTransportError
        Any other status at or above 400
    """
    if status < _CLIENT_ERROR_FLOOR:
        return

    if status in _MISSING_STATUSES:
        raise RouteMissingError(
            url, f"nothing at this route (HTTP {status})", response_code=status
        )

    if status == _UNAUTHORISED:
        raise NeedsCredentialsError(
            url,
            f"this route needs credentials we were not given (HTTP {status})",
            response_code=status,
        )

    if status in _BLOCK_STATUSES:
        raise DataNodeBlockedError(
            url,
            f"the data node is refusing us for now (HTTP {status})",
            response_code=status,
            retry_after_seconds=_retry_after_seconds(response),
        )

    raise DataNodeTransportError(
        url, f"the data node answered HTTP {status}", response_code=status
    )


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """
    Read a `Retry-After` header as a number of seconds, if it is one we understand

    Only the delay-seconds form is read, not the HTTP-date form. A date would have to
    be parsed against the node's clock and ours agreeing, and nothing in ESGF sends
    one; a value we cannot read falls back to ordinary backoff, which is correct
    behaviour rather than a gap.

    Parameters
    ----------
    response
        The response to read

    Returns
    -------
    :
        The delay the data node asked for, or `None` if it did not ask or we could
        not read what it asked
    """
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None

    try:
        seconds = float(raw.strip())
    except ValueError:
        return None

    return seconds if seconds >= 0 else None


def fetch_to_path(  # noqa: PLR0913 - the keyword-only extras are deliberate injection seams
    client: httpx.Client,
    route: Route,
    dest: Path,
    *,
    expected_size: int | None = None,
    checksum: str | None = None,
    checksum_type: str | None = None,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    read_timeout: float = DEFAULT_READ_TIMEOUT_SECONDS,
    retrying: Retrying | None = None,
    resume: bool = True,
    observer: DataNodeCallObserver | None = None,
    progress: ProgressCallback | None = None,
    file_id: int | None = None,
) -> FetchOutcome:
    """
    Fetch one route's bytes to a path, verifying them on the way in

    Parameters
    ----------
    client
        The HTTP client to fetch with.

        **This is the credentials seam.** esmporium fetches anonymously; to reach
        data that needs authenticating, pass a client that already carries what is
        needed (`httpx.Client(cert=...)` for an ESGF X.509 client certificate,
        or `auth=...`). Nothing here inspects or alters the client's credentials.

    route
        The route to fetch. Must be fetchable; see
        [is_fetchable][esmporium.downloads.is_fetchable].

    dest
        Where to put the finished file. Its parent directory is created if needed.

    expected_size
        How many bytes the search API said there would be, if it said.

        Checked, not trusted: a transfer that ends short raises
        [ShortReadError][esmporium.downloads.ShortReadError]. This matters most for a
        file with no checksum, where truncation is otherwise invisible.

    checksum
        The published digest to verify against, as a bare hex string, if there is one.

        Already normalised across both search APIs by
        [decode_multihash_checksum][esmporium.search.decode_multihash_checksum], so a
        value from either means the same thing here.

    checksum_type
        Which algorithm `checksum` is of, e.g. `SHA256`.

        `None`, or an algorithm this Python does not have, means the bytes are
        downloaded and `checksum_verified` comes back `False`.

    chunk_bytes
        How much to read, write and hash at a time.

    connect_timeout
        How long to wait for the data node to accept a connection.

    read_timeout
        How long to wait for the next chunk. See
        [DEFAULT_READ_TIMEOUT_SECONDS][(m).DEFAULT_READ_TIMEOUT_SECONDS] for why this
        is not a deadline for the whole transfer.

    retrying
        The retry policy for this one route. If `None`, a default is built with
        [build_download_retrying][esmporium.downloads.build_download_retrying].

        Retries happen against *this* route only. Moving to another mirror is the
        caller's decision, because only the caller knows what the other mirrors are.

    resume
        Whether to continue an existing `.part` file with a `Range` request.

        On by default. Pass `False` to always start again, e.g. if you suspect a
        `.part` on disk is not what it claims to be.

    observer
        Told about each attempt, on both the success and the failure path.
        If `None` (the default), nothing is recorded.
        See [esmporium.downloads.health][] for how to build one.

    progress
        Called with `(bytes_so_far, bytes_expected)` as the transfer proceeds.

    file_id
        Which [File][esmporium.db.schema.File] this is, recorded on each observation
        so health rows can be tied back to what was being fetched. Purely
        informational here.

    Returns
    -------
    :
        What was fetched, and what we could verify about it

    Raises
    ------
    UnfetchableRouteError
        `route` is not an HTTPS route to actual bytes. Raised before anything is
        attempted, and deliberately not a
        [DataNodeFetchError][esmporium.downloads.DataNodeFetchError]: no data node is
        implicated, so it must not count against one or fall through to the next
        mirror.

    DataNodeFetchError
        The fetch failed. The subclass says how, and its `outcome` is the
        [DownloadOutcome][esmporium.datasets.DownloadOutcome] to record.
    """
    if not is_fetchable(route):
        raise UnfetchableRouteError(
            route.url,
            service=str(route.service),
            container_format=str(route.container_format),
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = part_path(dest)
    policy = retrying if retrying is not None else build_download_retrying()

    attempt = 0

    def _attempt() -> FetchOutcome:
        # One fetch attempt: sent, streamed, checked and recorded here, so it observes
        # itself (with everything in scope) before returning or raising. The retry
        # policy calls this once per attempt, so recording here records them all --
        # exactly as `fire` does for search.
        nonlocal attempt
        attempt += 1
        started = time.monotonic()
        response_code: int | None = None
        transferred = 0
        resumed = False

        try:
            already = _resumable_bytes(part, expected_size) if resume else 0
            outcome = _stream_one(
                client=client,
                route=route,
                dest=dest,
                part=part,
                already=already,
                expected_size=expected_size,
                checksum=checksum,
                checksum_type=checksum_type,
                chunk_bytes=chunk_bytes,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
                progress=progress,
            )
        except DataNodeFetchError as exc:
            response_code = getattr(exc, "response_code", None)
            _observe(
                observer,
                route=route,
                file_id=file_id,
                attempt=attempt,
                outcome=exc.outcome,
                response_code=response_code,
                error=exc,
                transferred=getattr(exc, "bytes_transferred", transferred),
                seconds=time.monotonic() - started,
                resumed=getattr(exc, "resumed", resumed),
            )
            raise

        _observe(
            observer,
            route=route,
            file_id=file_id,
            attempt=attempt,
            outcome=DownloadOutcome.SUCCESS,
            response_code=outcome.response_code,
            error=None,
            transferred=outcome.fetch.bytes_written,
            seconds=time.monotonic() - started,
            resumed=outcome.fetch.resumed,
        )
        return outcome.fetch

    return policy(_attempt)


@dataclass(frozen=True)
class _StreamResult:
    """What one successful stream produced, plus the status code to record"""

    fetch: FetchOutcome
    response_code: int


def _resumable_bytes(part: Path, expected_size: int | None) -> int:
    """
    Decide how many bytes of an existing `.part` file we can continue from

    A stale `.part` which is already at or beyond the published size is discarded
    rather than resumed. Resuming one asks for a range starting at the end of the
    file, which a correct server answers with **416**, and a 416 that was retried
    rather than recovered from is how the prototype stranded files permanently: the
    complete `.part` sat on disk forever while the download was recorded as failed.

    Parameters
    ----------
    part
        The partial file to consider

    expected_size
        The published size, if known

    Returns
    -------
    :
        How many bytes to resume after, or `0` to start again
    """
    if not part.exists():
        return 0

    existing = part.stat().st_size
    if existing == 0:
        return 0

    if expected_size is not None and existing >= expected_size:
        # Either already complete (but never verified or renamed) or corrupt. Either
        # way, re-fetching is the cheap, certain option.
        logger.debug(
            "discarding a .part of %d bytes against a published %d: starting again",
            existing,
            expected_size,
            extra={"part_bytes": existing, "expected_size": expected_size},
        )
        part.unlink(missing_ok=True)
        return 0

    return existing


def _stream_one(  # noqa: PLR0913 - one argument per thing a stream has to be told
    *,
    client: httpx.Client,
    route: Route,
    dest: Path,
    part: Path,
    already: int,
    expected_size: int | None,
    checksum: str | None,
    checksum_type: str | None,
    chunk_bytes: int,
    connect_timeout: float,
    read_timeout: float,
    progress: ProgressCallback | None,
) -> _StreamResult:
    """
    Make one HTTP request and stream its body into place

    Split out of [fetch_to_path][(m).fetch_to_path] so that the retry bookkeeping and
    the byte-moving are separately readable.

    Parameters
    ----------
    client
        The HTTP client to fetch with

    route
        The route being fetched

    dest
        Where the finished file goes

    part
        The temporary file to stream into

    already
        How many bytes of `part` to keep and resume after

    expected_size
        The published size, if known

    checksum
        The published digest, if any

    checksum_type
        Which algorithm `checksum` is of, if known

    chunk_bytes
        How much to read, write and hash at a time

    connect_timeout
        How long to wait for a connection

    read_timeout
        How long to wait for the next chunk

    progress
        Told how the transfer is going

    Returns
    -------
    :
        The fetch outcome and the status code that produced it

    Raises
    ------
    DataNodeFetchError
        The request or the stream failed, or what arrived did not check out
    """
    headers = {"Range": f"bytes={already}-"} if already else {}
    timeout = httpx.Timeout(
        connect=connect_timeout,
        read=read_timeout,
        write=read_timeout,
        pool=connect_timeout,
    )
    request = client.build_request("GET", route.url, headers=headers, timeout=timeout)
    log_request_as_url_and_curl(
        logger,
        route.data_node,
        request,
        what="download",
        host_field="data_node",
        extra={"resume_from": already} if already else None,
    )

    # Declared before the `try` because they are read after it. Python would leak
    # them out of the `with` anyway, but relying on that makes it look as though the
    # post-stream checks are inside the stream, and a later edit could easily make
    # one of them genuinely unbound.
    status_code: int = 0
    hasher: Hasher | None = None
    resumed = False
    written = 0
    # What the node said it would send for *this* response. With a resume that is
    # the remaining range, so the promise for the whole file is this plus what was
    # already on disk.
    promised: int | None = None

    try:
        with client.stream("GET", route.url, headers=headers, timeout=timeout) as resp:
            status_code = resp.status_code
            if status_code == _RANGE_NOT_SATISFIABLE and already:
                # Our `.part` is longer than the server thinks the file is. Throw it
                # away and let the next attempt start clean, rather than retrying a
                # range that can never be satisfied.
                part.unlink(missing_ok=True)
                raise DataNodeTransportError(
                    route.url,
                    "the data node rejected our resume range (HTTP 416); "
                    "the partial file has been discarded so the next try starts fresh",
                    response_code=_RANGE_NOT_SATISFIABLE,
                )

            _classify_status(route.url, status_code, resp)

            # A server that ignores `Range` answers 200 with the whole body. Appending
            # to our existing bytes would then duplicate the overlap, so start again.
            resumed = bool(already) and status_code == _PARTIAL_CONTENT
            promised = _promised_whole_file(resp, already)

            hasher = _hasher(checksum_type) if checksum else None
            if hasher is not None and resumed:
                # Seed the digest with the bytes we are keeping, or it would only
                # describe the part fetched this time.
                _feed_existing(part, hasher, chunk_bytes)

            written = _write_body(
                resp,
                part=part,
                append=resumed,
                hasher=hasher,
                chunk_bytes=chunk_bytes,
                already=already if resumed else 0,
                expected_size=expected_size,
                progress=progress,
            )
    except httpx.ConnectTimeout as exc:
        raise DataNodeTimeoutError(
            route.url, f"connecting timed out after {connect_timeout:g}s"
        ) from exc
    except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as exc:
        raise DataNodeTimeoutError(
            route.url, f"no data for {read_timeout:g}s: the transfer stalled"
        ) from exc
    except httpx.ConnectError as exc:
        raise DataNodeHostFaultError(
            route.url, f"could not reach the data node: {exc}"
        ) from exc
    except DataNodeFetchError:
        raise
    except httpx.HTTPError as exc:
        raise DataNodeTransportError(
            route.url, f"the transfer failed: {type(exc).__name__}: {exc}"
        ) from exc

    total = part.stat().st_size
    # `promised` is already the whole file's size, whether this was a fresh 200 or a
    # resumed 206, so there is nothing to add to it here.
    _check_size(
        route.url,
        part,
        total,
        expected_size,
        promised=promised,
        response_code=status_code,
    )
    verified = _check_digest(
        route.url,
        part,
        hasher,
        checksum,
        checksum_type,
        response_code=status_code,
    )

    # Atomic within a filesystem: after this, `dest` is either the old file or this
    # complete one, never a half-written one.
    os.replace(part, dest)

    return _StreamResult(
        fetch=FetchOutcome(
            path=dest,
            bytes_written=written,
            size_on_disk=total,
            checksum_verified=verified,
            checksum_type_used=checksum_type if verified else None,
            resumed=resumed,
            route=route,
        ),
        response_code=status_code,
    )


def _feed_existing(part: Path, hasher: Hasher, chunk_bytes: int) -> None:
    """
    Feed the bytes already on disk into a hasher, so a resumed digest is of the whole

    Parameters
    ----------
    part
        The partial file

    hasher
        The hasher to update

    chunk_bytes
        How much to read at a time
    """
    with part.open("rb") as handle:
        while chunk := handle.read(chunk_bytes):
            hasher.update(chunk)


def _write_body(  # noqa: PLR0913 - one argument per thing writing a body has to be told
    response: httpx.Response,
    *,
    part: Path,
    append: bool,
    hasher: Hasher | None,
    chunk_bytes: int,
    already: int,
    expected_size: int | None,
    progress: ProgressCallback | None,
) -> int:
    """
    Stream a response body into the partial file

    Parameters
    ----------
    response
        The streaming response to read

    part
        The file to write into

    append
        Whether to append (a resumed transfer) or truncate (a fresh one)

    hasher
        Updated with each chunk, or `None` if there is nothing to verify against

    chunk_bytes
        How much to read, write and hash at a time

    already
        How many bytes were already on disk, for the progress total

    expected_size
        The published size, for the progress total

    progress
        Told how the transfer is going

    Returns
    -------
    :
        How many bytes this call wrote
    """
    written = 0
    with part.open("ab" if append else "wb") as handle:
        for chunk in response.iter_bytes(chunk_bytes):
            if not chunk:
                continue

            handle.write(chunk)
            if hasher is not None:
                hasher.update(chunk)

            written += len(chunk)
            if progress is not None:
                progress(already + written, expected_size)

        handle.flush()
        # Ask the OS to actually put it on the disk before we rename it into place.
        # Without this, a crash just after the rename can leave a correctly-named
        # file whose contents never landed.
        os.fsync(handle.fileno())

    return written


def _promised_whole_file(response: httpx.Response, already: int) -> int | None:
    """
    Read how big the data node says the *whole* file is

    Not simply `Content-Length`, because for a partial response that is the length of
    the returned range rather than of the file. A `206` states the whole size in
    `Content-Range: bytes <first>-<last>/<total>`, and that total is what a size
    disagreement has to be judged against -- a node which returns a *shorter* range
    than we asked for has cut the transfer off, and adding `Content-Length` to what
    was already on disk would make that look like a complete file of the wrong size.

    Parameters
    ----------
    response
        The response to read

    already
        How many bytes were already on disk before this response

    Returns
    -------
    :
        What the node says the whole file is, or `None` if it did not say

        `None` for a chunked response, which makes no promise at all; the node then
        cannot be held to anything, which is what [_check_size][(m).] falls back on.
    """
    total = _content_range_total(response)
    if total is not None:
        return total

    length = _header_int(response, "Content-Length")
    if length is None:
        return None

    # A 200 answers with the whole body, so `already` is being discarded and the
    # length *is* the whole file. A 206 without a readable Content-Range is odd
    # enough that treating the range as running to the end is the best guess left.
    return length + (already if response.status_code == _PARTIAL_CONTENT else 0)


def _content_range_total(response: httpx.Response) -> int | None:
    """
    Read the total size out of a `Content-Range` header

    Parameters
    ----------
    response
        The response to read

    Returns
    -------
    :
        The total, or `None` if the header is absent, unreadable, or says `*`
        (which a node sends when it does not know the total)
    """
    raw = response.headers.get("Content-Range")
    if raw is None or "/" not in raw:
        return None

    total = raw.rsplit("/", 1)[-1].strip()
    if total == "*":
        return None

    try:
        return int(total)
    except ValueError:
        return None


def _header_int(response: httpx.Response, name: str) -> int | None:
    """
    Read a header as an integer, or `None` if it is absent or not one

    Parameters
    ----------
    response
        The response to read

    name
        The header's name

    Returns
    -------
    :
        The value, or `None`
    """
    raw = response.headers.get(name)
    if raw is None:
        return None

    try:
        return int(raw.strip())
    except ValueError:
        return None


def _check_size(  # noqa: PLR0913 - one argument per thing a check has to be told
    url: str,
    part: Path,
    total: int,
    expected_size: int | None,
    *,
    promised: int | None = None,
    response_code: int | None = None,
) -> None:
    """
    Check the finished file against what was published, and say whose fault it is not

    Two different failures live here, and separating them is the whole point:

    - the transfer was **cut off** -- fewer bytes arrived than the data node's own
      `Content-Length` promised. That is the node failing to deliver, so it is a
      [ShortReadError][esmporium.downloads.ShortReadError], worth retrying and
      counted against the node;
    - the transfer **completed** and the file is simply not the size the search index
      publishes. The node delivered exactly what it promised, so the index is what is
      wrong: a [SizeMismatchError][esmporium.downloads.SizeMismatchError], not
      retried and **not** counted against the node.

    Without the split, a per-file publication error would slowly retire a working
    replica from the health ranking. See
    [DownloadOutcome.SIZE_MISMATCH][esmporium.datasets.DownloadOutcome.SIZE_MISMATCH]
    for the live case that showed this up.

    Parameters
    ----------
    url
        The URL the bytes came from

    part
        The partial file, removed if the check fails

    total
        How large it is

    expected_size
        How large the search index said it would be, if it said

    promised
        How large the data node said this file would be, if it said

        `None` for a chunked response, which makes no promise. The failure is then
        reported as a [ShortReadError][esmporium.downloads.ShortReadError], because
        with nothing to attribute it to the conservative answer is to treat it as a
        transfer problem and try again.

    response_code
        The status code the data node answered with, for the record

    Raises
    ------
    ShortReadError
        The transfer delivered less than the data node promised

    SizeMismatchError
        The transfer completed, and disagrees with what the index published
    """
    if expected_size is None or total == expected_size:
        return

    # Keep nothing either way: a file of the wrong length is worse than no file,
    # because on the next run it looks like a complete one.
    part.unlink(missing_ok=True)

    if promised is not None and total == promised:
        # The node did exactly what it said. The disagreement is with the index.
        raise SizeMismatchError(
            url,
            published=expected_size,
            served=total,
            response_code=response_code,
        )

    raise ShortReadError(
        url, expected=expected_size, got=total, response_code=response_code
    )


def _check_digest(  # noqa: PLR0913 - one argument per thing a check has to be told
    url: str,
    part: Path,
    hasher: Hasher | None,
    checksum: str | None,
    checksum_type: str | None,
    *,
    response_code: int | None = None,
) -> bool:
    """
    Check the finished file's digest against what was published

    Parameters
    ----------
    url
        The URL the bytes came from

    part
        The partial file, removed if the check fails

    hasher
        The hasher fed with every byte, or `None` if there was nothing to check with

    checksum
        The published digest, if any

    checksum_type
        Which algorithm it is of, if known

    response_code
        The status code the data node answered with, for the record

    Returns
    -------
    :
        Whether the digest was checked *and* matched.

        `False` means there was nothing to check against, never that a check failed.

    Raises
    ------
    ChecksumMismatchError
        The digest did not match what was published
    """
    if hasher is None or checksum is None:
        return False

    got = hasher.hexdigest()
    if got.lower() == checksum.lower():
        return True

    part.unlink(missing_ok=True)
    raise ChecksumMismatchError(
        url,
        expected=checksum,
        got=got,
        algorithm=checksum_type or "unknown",
        response_code=response_code,
    )


def _observe(  # noqa: PLR0913 - one keyword-only argument per recorded field
    observer: DataNodeCallObserver | None,
    *,
    route: Route,
    file_id: int | None,
    attempt: int,
    outcome: DownloadOutcome,
    response_code: int | None,
    error: Exception | None,
    transferred: int,
    seconds: float,
    resumed: bool,
) -> None:
    """
    Tell the observer, if any, how one attempt went

    Parameters
    ----------
    observer
        The observer to tell, or `None` to record nothing

    route
        The route that was tried

    file_id
        Which file was being fetched

    attempt
        Which attempt this was, 1-based

    outcome
        How it turned out

    response_code
        The status code, if anything answered

    error
        The exception that ended it, or `None` on success

    transferred
        How many bytes this attempt moved

    seconds
        How long it took

    resumed
        Whether it continued a part-downloaded file
    """
    if observer is None:
        return

    observer(
        DataNodeCall(
            data_node=route.data_node,
            url=route.url,
            http_method="GET",
            response_code=response_code,
            outcome=outcome,
            error=error,
            bytes_transferred=transferred,
            seconds=seconds,
            attempt_number=attempt,
            resumed=resumed,
            file_id=file_id,
            data_access_id=route.data_access_id,
        )
    )
