"""
Fetching the first bytes of a remote file, and reading a header out of them

One HTTP range request per attempt.
"""

from __future__ import annotations

import logging
import socket
import ssl
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from esmporium.datasets import HeaderReadOutcome
from esmporium.headers.errors import (
    HeaderNeedsCredentialsError,
    HeaderNodeBlockedError,
    HeaderNodeHostFaultError,
    HeaderParseError,
    HeaderReadError,
    HeaderReadTimeoutError,
    HeaderRouteMissingError,
    HeaderTransportError,
    TruncatedHeaderError,
)
from esmporium.headers.health import HeaderReadCall
from esmporium.headers.parsing import read_global_attributes
from esmporium.headers.retry import build_header_retrying
from esmporium.http import log_request_as_url_and_curl

if TYPE_CHECKING:
    from collections.abc import Mapping

    from tenacity import Retrying

    from esmporium.downloads import Route
    from esmporium.headers.health import HeaderReadCallObserver

logger = logging.getLogger(__name__)

DEFAULT_WANT_BYTES = 64 * 1024
"""
How many bytes from the start of a file to ask for

Sized from measurement rather than guessed.

Asking for too little is recoverable: a parser raises
[TruncatedHeaderError][esmporium.headers.TruncatedHeaderError] and
[read_header][(m).read_header] asks for more.
"""

DEFAULT_MAX_WANT_BYTES = 1024 * 1024
"""
The most we will ever ask for before giving up on a file
"""

DEFAULT_CONNECT_TIMEOUT_SECONDS = 30.0
"""
How long to wait for a data node to accept a connection

A connection that never completes is the signature of a black-holed node -- one that
accepts the packet and answers nothing.
"""

DEFAULT_READ_TIMEOUT_SECONDS = 30.0
"""
How long to wait for the bytes once connected

A *whole-read* deadline in effect, unlike
[esmporium.downloads.DEFAULT_READ_TIMEOUT_SECONDS][], which is per chunk because a
large file legitimately takes a long time. Here there is only one small range to
deliver, so a node that has not managed it in this long is stalled rather than slow.
"""

_PARTIAL_CONTENT = 206
_RANGE_NOT_SATISFIABLE = 416
_MISSING_STATUSES = frozenset({404, 410})
_BLOCK_STATUSES = frozenset({403, 429, 503})
_UNAUTHORISED = 401
_CLIENT_ERROR_FLOOR = 400

# Causes of an `httpx.ConnectError` which mean the node is momentarily unhappy rather
# than unreachable, so they keep their retry. Everything else -- DNS failure, TLS
# failure, anything unrecognised -- is treated as the node being unreachable.
#
# Classifying on the *cause's type* is what lets this be exact without matching
# substrings in a message, which `esmporium.downloads.errors` forbids and which the
# prototype had no way to avoid: it read headers through a library that reported an
# HTTP status as English prose inside an `OSError`, so it carried tables of signature
# strings like `"ssl"` and `"could not resolve host"`.
_RETRYABLE_CONNECT_CAUSES: tuple[type[BaseException], ...] = (
    ConnectionRefusedError,
    ConnectionResetError,
)

_UNREACHABLE_CONNECT_CAUSES: tuple[type[BaseException], ...] = (
    socket.gaierror,  # DNS: the name does not resolve
    ssl.SSLError,  # TLS: a bad or untrusted certificate, a protocol mismatch
)


@dataclass(frozen=True)
class HeaderBytes:
    """
    The prefix of a file that one successful attempt fetched
    """

    prefix: bytes
    """
    The bytes, starting at byte zero of the file

    May be shorter than what was asked for, when the whole file is shorter.
    """

    file_size: int | None
    """
    How long the whole file is, as the data node reports it

    Read from `Content-Range` on a partial response and from `Content-Length` on a
    whole one. **Taken from the response rather than from the search index on
    purpose**: the two are known to disagree, and the one that matters here is what
    the node actually served, because that is what HDF5 will check its superblock
    against.

    `None` when the node did not say, which leaves a netCDF-4 file unreadable -- HDF5
    will not open a file whose length it does not know. Classic netCDF does not care.
    """

    range_honoured: bool
    """
    Whether the data node answered our range request with a range

    `False` means it answered `200` and started sending the whole file. We read our
    prefix and abandoned the rest, so the answer is still usable -- but the node made
    us open a connection it was going to pour a multi-gigabyte file down, which is
    worth knowing per node rather than silently absorbing.
    """

    route: Route
    """Which route served these bytes"""


@dataclass(frozen=True)
class FileHeader:
    """
    One file's global attributes, and where they came from
    """

    attributes: Mapping[str, object]
    """
    Every global attribute the file declares, exactly as it stores them

    Nothing is renamed or reinterpreted here. What `parent_experiment_rip` means is a
    question about CMIP5, not about netCDF.
    """

    bytes_read: HeaderBytes
    """The prefix these were read out of, and the route that served it"""

    @property
    def route(self) -> Route:
        """
        Which route served the bytes

        Returns
        -------
        :
            The route
        """
        return self.bytes_read.route


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
    HeaderRouteMissingError
        404, 410, or 416 -- see below for why a 416 lands here.

    HeaderNeedsCredentialsError
        401

    HeaderNodeBlockedError
        403, 429 or 503

    HeaderTransportError
        Any other status at or above 400
    """
    if status < _CLIENT_ERROR_FLOOR:
        return

    if status in _MISSING_STATUSES:
        raise HeaderRouteMissingError(
            url, f"nothing at this route (HTTP {status})", response_code=status
        )

    if status == _RANGE_NOT_SATISFIABLE:
        # Any range starting at zero is satisfiable for a file with bytes in it, so a
        # 416 here says the file is empty. That is a publication fault rather than a
        # node fault, and it has the same consequences as a 404: do not retry, do not
        # blame the node, try another mirror in case it holds the real file.
        raise HeaderRouteMissingError(
            url,
            f"the data node cannot serve a range from the start of this file "
            f"(HTTP {status}), which means it is serving an empty file",
            response_code=status,
        )

    if status == _UNAUTHORISED:
        raise HeaderNeedsCredentialsError(
            url,
            f"this route needs credentials we were not given (HTTP {status})",
            response_code=status,
        )

    if status in _BLOCK_STATUSES:
        raise HeaderNodeBlockedError(
            url,
            f"the data node is refusing us for now (HTTP {status})",
            response_code=status,
            retry_after_seconds=_retry_after_seconds(response),
        )

    raise HeaderTransportError(
        url, f"the data node answered HTTP {status}", response_code=status
    )


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """
    Read a `Retry-After` header as a number of seconds, if it is one we understand

    Only the delay-seconds form is read, not the HTTP-date form: a date needs the
    node's clock and ours to agree and nothing in ESGF sends one.

    Parameters
    ----------
    response
        The response to read

    Returns
    -------
    :
        The delay the data node asked for, or `None` if it did not ask or we could not
        read what it asked
    """
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None

    try:
        seconds = float(raw.strip())
    except ValueError:
        return None

    return seconds if seconds >= 0 else None


def _content_range_total(response: httpx.Response) -> int | None:
    """
    Read the whole file's size out of a `Content-Range` header

    Parameters
    ----------
    response
        The response to read

    Returns
    -------
    :
        The total, or `None` if the header is absent, unreadable, or says `*` (which a
        node sends when it does not know the total)
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
    Read a response header as an integer, tolerating a missing or malformed one

    Parameters
    ----------
    response
        The response to read

    name
        The header's name

    Returns
    -------
    :
        The value, or `None` if it is absent or not an integer
    """
    raw = response.headers.get(name)
    if raw is None:
        return None

    try:
        return int(raw.strip())
    except ValueError:
        return None


def _connect_error(url: str, error: httpx.ConnectError) -> HeaderReadError:
    """
    Decide whether a failure to connect is the node being unreachable or just unhappy

    Parameters
    ----------
    url
        The URL the attempt went to

    error
        The error `httpx` raised

    Returns
    -------
    :
        A [HeaderTransportError][esmporium.headers.HeaderTransportError] if this is
        worth another go, and a
        [HeaderNodeHostFaultError][esmporium.headers.HeaderNodeHostFaultError] if the
        node is unreachable
    """
    cause = error.__cause__

    # Checked before the retryable causes, because `ssl.SSLError` and `socket.gaierror`
    # are both `OSError` subclasses and so is `ConnectionRefusedError`; an `SSLError`
    # that happened to be a subclass of something retryable must still count as
    # unreachable.
    if isinstance(cause, _UNREACHABLE_CONNECT_CAUSES):
        return HeaderNodeHostFaultError(
            url, f"could not reach the data node: {type(cause).__name__}: {cause}"
        )

    if isinstance(cause, _RETRYABLE_CONNECT_CAUSES):
        return HeaderTransportError(
            url, f"the data node refused the connection: {cause}"
        )

    # Something we have not seen. Treated as unreachable rather than retryable,
    # because the expensive mistake is retrying a node that will never answer.
    return HeaderNodeHostFaultError(url, f"could not reach the data node: {error}")


def _read_prefix(
    client: httpx.Client,
    route: Route,
    *,
    want_bytes: int,
    connect_timeout: float,
    read_timeout: float,
) -> tuple[HeaderBytes, int]:
    """
    Make one range request and read at most `want_bytes` from the response

    Parameters
    ----------
    client
        The client to request with

    route
        The route to request

    want_bytes
        How many bytes from the start of the file to ask for

    connect_timeout
        How long to wait for the node to accept a connection

    read_timeout
        How long to wait for the bytes

    Returns
    -------
    :
        What was fetched, and the status code the node answered with

    Raises
    ------
    HeaderReadError
        The node did not serve the bytes. Which subclass says why.
    """
    headers = {"Range": f"bytes=0-{want_bytes - 1}"}
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
        what="header",
        host_field="data_node",
        extra={"want_bytes": want_bytes},
    )

    try:
        with client.stream(
            "GET", route.url, headers=headers, timeout=timeout
        ) as response:
            _classify_status(route.url, response.status_code, response)

            honoured = response.status_code == _PARTIAL_CONTENT
            chunks: list[bytes] = []
            got = 0
            for chunk in response.iter_bytes():
                chunks.append(chunk)
                got += len(chunk)
                if got >= want_bytes:
                    # Enough. Leaving the `with` block here closes the connection
                    # without reading the rest, which is the point: a node that
                    # ignored our range is part way through sending a file that can
                    # be hundreds of megabytes, and draining it would turn a header
                    # read into a download.
                    break

            if honoured:
                file_size = _content_range_total(response)
            else:
                # A `200` means the body *is* the whole file, so its length is the
                # file's length -- unlike a `206`, where `Content-Length` is only the
                # length of the range.
                file_size = _header_int(response, "Content-Length")

            fetched = HeaderBytes(
                prefix=b"".join(chunks)[:want_bytes],
                file_size=file_size,
                range_honoured=honoured,
                route=route,
            )

            return fetched, response.status_code
    except httpx.ConnectTimeout as error:
        raise HeaderNodeHostFaultError(
            route.url,
            f"connecting timed out after {connect_timeout:g}s, which is what a "
            "black-holed data node looks like",
        ) from error
    except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as error:
        raise HeaderReadTimeoutError(
            route.url,
            f"no header after {read_timeout:g}s: the data node connected and stalled",
        ) from error
    except httpx.ConnectError as error:
        raise _connect_error(route.url, error) from error
    except HeaderReadError:
        raise
    except httpx.HTTPError as error:
        raise HeaderTransportError(
            route.url, f"the request failed: {type(error).__name__}: {error}"
        ) from error


def _escalated(
    error: HeaderParseError,
    *,
    asking_for: int,
    max_want_bytes: int,
    file_size: int | None,
) -> int | None:
    """
    Work out how many bytes to ask for next, or `None` to give up

    Parameters
    ----------
    error
        Why reading the header failed

    asking_for
        How many bytes were asked for this time

    max_want_bytes
        The most to ask for before giving up

    file_size
        How long the whole file is, if the node said

    Returns
    -------
    :
        How many bytes to ask for next, or `None` if asking again cannot help

        `None` for every failure except a truncated header, for a ceiling already
        reached, and for a prefix which already covers the whole file -- in that last
        case there are no more bytes to be had, so the header is malformed rather than
        merely out of reach.
    """
    if not isinstance(error, TruncatedHeaderError):
        return None

    if asking_for >= max_want_bytes:
        return None

    if file_size is not None and asking_for >= file_size:
        # We already hold the entire file and still cannot read its header. More
        # requests would return the same bytes.
        return None

    # Twice the reported need, not exactly it: `bytes_needed` is how far the reader
    # got before running out, so reading on from there will want more again. Doubling
    # what we asked for is the floor, so a tiny reported need still makes real
    # progress.
    wanted = max(asking_for * 2, error.bytes_needed * 2)

    return min(wanted, max_want_bytes)


def _observe(  # noqa: PLR0913 - one keyword-only argument per recorded field
    observer: HeaderReadCallObserver | None,
    *,
    route: Route,
    outcome: HeaderReadOutcome,
    response_code: int | None,
    error: Exception | None,
    bytes_requested: int,
    bytes_transferred: int,
    range_honoured: bool,
    seconds: float,
    attempt_number: int,
    file_id: int | None,
) -> None:
    """
    Tell the observer about one attempt, if there is one

    Parameters
    ----------
    observer
        The observer, or `None` to record nothing

    route
        The route the attempt went to

    outcome
        How it turned out

    response_code
        The status the node answered with, or `None` if nothing answered

    error
        The exception that ended the attempt, or `None` on success

    bytes_requested
        How many bytes were asked for

    bytes_transferred
        How many arrived

    range_honoured
        Whether the node answered the range request with a range

    seconds
        How long the attempt took

    attempt_number
        Which attempt against this route this was, 1-based

    file_id
        Which file this is
    """
    if observer is None:
        return

    observer(
        HeaderReadCall(
            data_node=route.data_node,
            url=route.url,
            http_method="GET",
            response_code=response_code,
            outcome=outcome,
            error=error,
            bytes_requested=bytes_requested,
            bytes_transferred=bytes_transferred,
            seconds=seconds,
            attempt_number=attempt_number,
            range_honoured=range_honoured,
            file_id=file_id,
            data_access_id=route.data_access_id,
        )
    )


def read_header(  # noqa: PLR0913 - the keyword-only extras are deliberate injection seams
    client: httpx.Client,
    route: Route,
    *,
    want_bytes: int = DEFAULT_WANT_BYTES,
    max_want_bytes: int = DEFAULT_MAX_WANT_BYTES,
    published_size: int | None = None,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    read_timeout: float = DEFAULT_READ_TIMEOUT_SECONDS,
    retrying: Retrying | None = None,
    observer: HeaderReadCallObserver | None = None,
    file_id: int | None = None,
) -> FileHeader:
    """
    Read one file's global attributes over one route

    Fetches a prefix of the file with a range request and reads the header out of it.
    If the header turns out to reach past what was fetched, asks for more and tries
    again, up to `max_want_bytes`. Escalation should be rare -- nothing sampled from
    ESGF needed more than 42 kB against a default ask of 64 KiB -- and exists because
    HDF5 does not *promise* to put the root group's attributes near the start.

    **Fetching and parsing are one attempt, not two**, which is why there is no
    public "just get me the bytes" function beside this. The question an observer
    needs answered is "did asking this node for this header work", and bytes that
    arrive and do not parse are a no.

    Parameters
    ----------
    client
        The client to request with

        **The credentials seam**: header reads go anonymously,
        and a caller who needs otherwise passes an already-authenticated client. No
        ESGF node has been seen to ask.

    route
        The route to read from

        Expected to be HTTPS to actual bytes. Filter with
        [is_fetchable][esmporium.downloads.is_fetchable] first.

    want_bytes
        How many bytes from the start of the file to ask for first

    max_want_bytes
        The most to ask for before giving up

        Doubling without a ceiling would fetch a pathological file in full, one
        request at a time, which is the cost this package exists to avoid.

    published_size
        How long the search index says the file is, if known

        Only a fallback, used when the data node does not report a size. The node's
        own figure is preferred because the index and the served file are known to
        disagree, and HDF5 checks its superblock against the file it is really given.

    connect_timeout
        How long to wait for the node to accept a connection

    read_timeout
        How long to wait for the bytes once connected

    retrying
        The retry policy for one route, or `None` for
        [build_header_retrying][esmporium.headers.build_header_retrying]

        Retries happen against *this* route only. Moving to another route, or
        abandoning this data node entirely, is the caller's decision -- and for header
        reads those are two different decisions, which is what
        [stops_this_data_node][esmporium.datasets.HeaderReadOutcome.stops_this_data_node]
        is for.

    observer
        Told about each attempt as it happens, successful or not

    file_id
        Which [File][esmporium.db.schema.File] this is, recorded on each attempt

    Returns
    -------
    :
        The file's global attributes, and the bytes they were read out of

    Raises
    ------
    HeaderReadError
        The data node did not serve the bytes. Read
        [outcome][esmporium.headers.HeaderReadError.outcome] off the error to decide
        what to try next.

    esmporium.headers.HeaderParseError
        The bytes are not a header we can read. A
        [TruncatedHeaderError][esmporium.headers.TruncatedHeaderError] here means even
        `max_want_bytes` was not enough.
    """
    policy = retrying if retrying is not None else build_header_retrying()
    asking_for = want_bytes
    attempt = 0
    # How long the node last said the file is. Kept out here so that, when a header
    # does not fit, the escalation below can tell "ask for more" from "we already
    # hold the whole file" without making another request.
    served_file_size: int | None = None

    def _attempt() -> FileHeader:
        # One attempt: requested, read, parsed and recorded here, so it observes
        # itself with everything in scope before returning or raising. The retry
        # policy calls this once per attempt, so recording here records them all --
        # exactly as `fetch_to_path` and `fire` do.
        nonlocal attempt, served_file_size
        attempt += 1
        started = time.monotonic()

        try:
            fetched, status = _read_prefix(
                client,
                route,
                want_bytes=asking_for,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
            )
        except HeaderReadError as error:
            _observe(
                observer,
                route=route,
                outcome=error.outcome,
                response_code=error.response_code,
                error=error,
                bytes_requested=asking_for,
                bytes_transferred=0,
                # Nothing was served, so there is no range to have been honoured.
                # Recorded as `True` so that a failure never looks like evidence
                # about a node's range handling.
                range_honoured=True,
                seconds=time.monotonic() - started,
                attempt_number=attempt,
                file_id=file_id,
            )
            raise

        file_size = (
            fetched.file_size if fetched.file_size is not None else published_size
        )
        served_file_size = file_size
        try:
            attributes = read_global_attributes(
                fetched.prefix,
                # Falling back to the prefix's own length is wrong for HDF5, which
                # refuses a file whose length disagrees with its superblock. That
                # refusal is a clearer failure than reading a file we know nothing
                # about, and no node sampled has failed to report a size.
                file_size=file_size if file_size is not None else len(fetched.prefix),
            )
        except HeaderParseError as error:
            _observe(
                observer,
                route=route,
                outcome=error.outcome,
                response_code=status,
                error=error,
                bytes_requested=asking_for,
                bytes_transferred=len(fetched.prefix),
                range_honoured=fetched.range_honoured,
                seconds=time.monotonic() - started,
                attempt_number=attempt,
                file_id=file_id,
            )
            raise

        _observe(
            observer,
            route=route,
            outcome=HeaderReadOutcome.SUCCESS,
            response_code=status,
            error=None,
            bytes_requested=asking_for,
            bytes_transferred=len(fetched.prefix),
            range_honoured=fetched.range_honoured,
            seconds=time.monotonic() - started,
            attempt_number=attempt,
            file_id=file_id,
        )

        return FileHeader(attributes=attributes, bytes_read=fetched)

    while True:
        try:
            # A parse failure is not retried by the policy -- `is_worth_retrying` only
            # says yes to a node being busy or a broken connection -- so it comes
            # straight out here, which is where asking for more bytes is decided.
            return policy(_attempt)
        except HeaderParseError as error:
            escalated = _escalated(
                error,
                asking_for=asking_for,
                max_want_bytes=max_want_bytes,
                file_size=served_file_size,
            )
            if escalated is None:
                raise

            logger.debug(
                "a %d byte prefix of %s was not enough for its header; asking for %d",
                asking_for,
                route.url,
                escalated,
                extra={
                    "data_node": route.data_node,
                    "http_url": route.url,
                    "bytes_requested": asking_for,
                    "next_bytes_requested": escalated,
                },
            )
            asking_for = escalated
