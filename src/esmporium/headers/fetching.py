"""
Reading a file's header from a data node, one HTTP range request at a time

[RangeSource][(m).RangeSource] is the [ByteSource][esmporium.headers.ByteSource] over
a route: it fetches the blocks of a file the readers ask for, and nothing else.
[read_header][(m).read_header] reads one file's header through it.
"""

from __future__ import annotations

import dataclasses
import logging
import socket
import ssl
import time
from typing import TYPE_CHECKING

import httpx

from esmporium.datasets import HeaderReadOutcome
from esmporium.headers.contents import DEFAULT_TIME_VARIABLE
from esmporium.headers.errors import (
    HeaderNeedsCredentialsError,
    HeaderNodeBlockedError,
    HeaderNodeHostFaultError,
    HeaderParseError,
    HeaderReadError,
    HeaderReadTimeoutError,
    HeaderRouteMissingError,
    HeaderTransportError,
    RangesNotHonouredError,
    UnreadableHeaderError,
)
from esmporium.headers.health import HeaderReadCall
from esmporium.headers.reading import read_file_header
from esmporium.headers.retry import build_header_retrying
from esmporium.headers.sources import (
    DEFAULT_BLOCK_BYTES,
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_REQUESTS,
    BlockCache,
    check_within_file,
)
from esmporium.http import log_request_as_url_and_curl

if TYPE_CHECKING:
    from types import TracebackType

    from tenacity import Retrying

    from esmporium.downloads import Route
    from esmporium.headers.contents import FileHeader
    from esmporium.headers.health import HeaderReadCallObserver

logger = logging.getLogger(__name__)

DEFAULT_CONNECT_TIMEOUT_SECONDS = 30.0
"""
How long to wait for a data node to accept a connection

A connection that never completes is the signature of a black-holed node -- one that
accepts the packet and answers nothing.
"""

DEFAULT_READ_TIMEOUT_SECONDS = 30.0
"""
How long to wait for the bytes of one range once connected

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


def _classify_status(
    url: str, status: int, response: httpx.Response, *, start: int
) -> None:
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

    start
        The offset the requested range started at

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
        # Ranges are only ever asked for inside the file's reported length, so a 416
        # says the file served is shorter than that: empty, if the range started at
        # zero. That is a publication fault rather than a node fault, and it has the
        # same consequences as a 404: do not retry, do not blame the node, try
        # another mirror in case it holds the real file.
        what = (
            "which means it is serving an empty file"
            if start == 0
            else "which means the file it is serving is shorter than it said"
        )
        raise HeaderRouteMissingError(
            url,
            f"the data node cannot serve a range from offset {start} of this file "
            f"(HTTP {status}), {what}",
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


@dataclasses.dataclass(frozen=True)
class _Served:
    """What one successful range request delivered"""

    body: bytes
    status: int
    range_honoured: bool
    file_size: int | None


def _get_range(  # noqa: PLR0913 - one argument per thing the request needs
    client: httpx.Client,
    route: Route,
    *,
    start: int,
    end: int,
    connect_timeout: float,
    read_timeout: float,
) -> _Served:
    """
    Make one range request, and read at most the range from the response

    Parameters
    ----------
    client
        The client to request with

    route
        The route to request

    start
        The first byte to ask for

    end
        One past the last byte to ask for

    connect_timeout
        How long to wait for the node to accept a connection

    read_timeout
        How long to wait for the bytes

    Returns
    -------
    :
        What was served

    Raises
    ------
    HeaderReadError
        The node did not serve the bytes. Which subclass says why.
    """
    want = end - start
    headers = {"Range": f"bytes={start}-{end - 1}"}
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
        what="header range",
        host_field="data_node",
        extra={"range_start": start, "range_end": end},
    )

    try:
        with client.stream(
            "GET", route.url, headers=headers, timeout=timeout
        ) as response:
            _classify_status(route.url, response.status_code, response, start=start)

            honoured = response.status_code == _PARTIAL_CONTENT
            if not honoured and start > 0:
                # The body is the file from byte zero, and what we want is further
                # in. Reaching it would mean streaming everything before it.
                raise RangesNotHonouredError(
                    route.url,
                    f"asked for bytes from offset {start}, the data node answered "
                    f"HTTP {response.status_code} with the whole file",
                    response_code=response.status_code,
                )

            if honoured and _content_range_start(response) not in (None, start):
                raise RangesNotHonouredError(
                    route.url,
                    f"asked for bytes from offset {start}, the data node served a "
                    f"range starting elsewhere ({response.headers['Content-Range']})",
                    response_code=response.status_code,
                )

            chunks: list[bytes] = []
            got = 0
            for chunk in response.iter_bytes():
                chunks.append(chunk)
                got += len(chunk)
                if got >= want:
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

            return _Served(
                body=b"".join(chunks)[:want],
                status=response.status_code,
                range_honoured=honoured,
                file_size=file_size,
            )
    except httpx.ConnectTimeout as error:
        raise HeaderNodeHostFaultError(
            route.url,
            f"connecting timed out after {connect_timeout:g}s, which is what a "
            "black-holed data node looks like",
        ) from error
    except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as error:
        raise HeaderReadTimeoutError(
            route.url,
            f"no bytes after {read_timeout:g}s: the data node connected and stalled",
        ) from error
    except httpx.ConnectError as error:
        raise _connect_error(route.url, error) from error
    except HeaderReadError:
        raise
    except httpx.HTTPError as error:
        raise HeaderTransportError(
            route.url, f"the request failed: {type(error).__name__}: {error}"
        ) from error


def _content_range_start(response: httpx.Response) -> int | None:
    """
    Read where the served range starts out of a `Content-Range` header

    Parameters
    ----------
    response
        The response to read

    Returns
    -------
    :
        The first byte served, or `None` if the header is absent or unreadable
    """
    raw = response.headers.get("Content-Range")
    if raw is None:
        return None

    first = raw.strip().removeprefix("bytes").strip().split("-", 1)[0]
    try:
        return int(first)
    except ValueError:
        return None


class RangeSource(BlockCache):
    """
    A file on a data node, read one HTTP range request at a time

    Fetches only the blocks a reader asks for, and keeps them, so reading a header
    and then a few values from far into the file costs a handful of small requests
    rather than the file. Refuses to go past its budget of bytes and requests.

    Each request is one call: tried under the retry policy, and told to the observer
    as a [HeaderReadCall][esmporium.headers.HeaderReadCall]. A call that delivered
    bytes is told to the observer once the next call starts or the read finishes, and
    if the read fails on the bytes themselves -- they do not parse, or more were
    needed than may be fetched -- the read's last call is the one that carries that
    outcome. So "did asking this node for this file's header work" is answered by
    the calls, as it is for a read that takes one request.

    Use as a context manager, so the last call is told to the observer however the
    read ends.
    """

    def __init__(  # noqa: PLR0913 - the keyword-only extras are deliberate injection seams
        self,
        client: httpx.Client,
        route: Route,
        *,
        block_bytes: int = DEFAULT_BLOCK_BYTES,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        published_size: int | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        read_timeout: float = DEFAULT_READ_TIMEOUT_SECONDS,
        retrying: Retrying | None = None,
        observer: HeaderReadCallObserver | None = None,
        file_id: int | None = None,
    ) -> None:
        """
        Initialise. Nothing is fetched until something is read.

        Parameters
        ----------
        client
            The client to request with

            **The credentials seam**: header reads go anonymously, and a caller who
            needs otherwise passes an already-authenticated client. No ESGF node has
            been seen to ask.

        route
            The route to read from

            Expected to be HTTPS to actual bytes. Filter with
            [is_fetchable][esmporium.downloads.is_fetchable] first.

        block_bytes
            The least to fetch whenever a read misses what is held

        max_bytes
            The most to fetch from the file in total

            Without a ceiling, a pathological file could be fetched in full, one
            request at a time, which is the cost reading a header exists to avoid.

        max_requests
            The most range requests to make of the file

        published_size
            How long the search index says the file is, if known

            Only a fallback, used when the data node does not report a size. The
            node's own figure is preferred because the index and the served file are
            known to disagree, and HDF5 checks its superblock against the file it is
            really given.

        connect_timeout
            How long to wait for the node to accept a connection

        read_timeout
            How long to wait for the bytes of one range once connected

        retrying
            The retry policy for one request, or `None` for
            [build_header_retrying][esmporium.headers.build_header_retrying]

            Retries happen against *this* route only. Moving to another route, or
            abandoning this data node entirely, is the caller's decision -- and for
            header reads those are two different decisions, which is what
            [stops_this_data_node][esmporium.datasets.HeaderReadOutcome.stops_this_data_node]
            is for.

        observer
            Told about each call

        file_id
            Which [File][esmporium.db.schema.File] this is, recorded on each call
        """
        super().__init__(
            block_bytes=block_bytes, max_bytes=max_bytes, max_requests=max_requests
        )
        self.client = client
        self.route = route
        self.published_size = published_size
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.retrying = retrying if retrying is not None else build_header_retrying()
        self.observer = observer
        self.file_id = file_id

        self._size: int | None = None
        self._range_honoured = True
        self._attempts = 0
        self._pending: HeaderReadCall | None = None

    @property
    def range_honoured(self) -> bool:
        """
        Whether the data node has answered every range request with a range

        Returns
        -------
        :
            `False` once it has answered with the whole file instead
        """
        return self._range_honoured

    @property
    def size(self) -> int:
        """
        How long the whole file is

        The data node reports it on the first response, so asking before anything has
        been read fetches the first block.

        Returns
        -------
        :
            The length
        """
        return self._file_size()

    def read(self, offset: int, count: int) -> bytes:
        """
        Get `count` bytes of the file, starting at `offset`

        Parameters
        ----------
        offset
            Where to start

        count
            How many bytes to get

        Returns
        -------
        :
            Exactly `count` bytes

        Raises
        ------
        esmporium.headers.TruncatedHeaderError
            Fetching them would go past the budget.

        RangesNotHonouredError
            They are past the first block, and the data node serves only whole files.

        HeaderReadError
            The data node did not serve them. Which subclass says why.
        """
        if self._size is None:
            self._fetch_first_block()

        return self._read_through_cache(offset, count)

    def _file_size(self) -> int:
        if self._size is None:
            self._fetch_first_block()

        if self._size is None:
            msg = (
                f"{self.route.url}: the data node did not say how long the file is, "
                "and no published size was given, so its bytes cannot be placed"
            )
            raise UnreadableHeaderError(msg)

        return self._size

    def _fetch_first_block(self) -> None:
        """
        Fetch the first block of the file, which is also how its size is learnt
        """
        served = self._call(0, self.block_bytes)
        self._range_honoured = served.range_honoured
        self._size = (
            served.file_size if served.file_size is not None else self.published_size
        )
        if self._size is not None and len(served.body) > self._size:
            msg = (
                f"{self.route.url}: the data node served {len(served.body)} bytes of "
                f"a file it says is {self._size} bytes long"
            )
            raise UnreadableHeaderError(msg)

        self._pieces[0] = served.body
        self._bytes_read += len(served.body)
        self._n_requests += 1

    def _fetch(self, start: int, end: int) -> bytes:
        if not self._range_honoured:
            raise RangesNotHonouredError(
                self.route.url,
                f"the read needs bytes from offset {start}, past the first block, and "
                "the data node serves this file only whole",
            )

        served = self._call(start, end)
        if len(served.body) != end - start:
            msg = (
                f"asked for {end - start} bytes from offset {start} and the data "
                f"node served {len(served.body)}"
            )
            raise HeaderTransportError(self.route.url, msg, response_code=served.status)

        return served.body

    def _call(self, start: int, end: int) -> _Served:
        """
        Make one range request under the retry policy, telling the observer

        Parameters
        ----------
        start
            The first byte to ask for

        end
            One past the last byte to ask for

        Returns
        -------
        :
            What was served
        """
        if self._size is not None:
            check_within_file(start, end - start, size=self._size)

        def attempt() -> _Served:
            # The previous call delivered its bytes and the read went on, so it was
            # a success.
            self._tell_pending(None)
            self._attempts += 1
            started = time.monotonic()

            try:
                served = _get_range(
                    self.client,
                    self.route,
                    start=start,
                    end=end,
                    connect_timeout=self.connect_timeout,
                    read_timeout=self.read_timeout,
                )
            except HeaderReadError as error:
                self._tell(
                    HeaderReadCall(
                        data_node=self.route.data_node,
                        url=self.route.url,
                        http_method="GET",
                        response_code=error.response_code,
                        outcome=error.outcome,
                        error=error,
                        bytes_requested=end - start,
                        bytes_transferred=0,
                        seconds=time.monotonic() - started,
                        attempt_number=self._attempts,
                        # A failure says nothing about range handling, unless range
                        # handling is what failed.
                        range_honoured=not isinstance(error, RangesNotHonouredError),
                        file_id=self.file_id,
                        data_access_id=self.route.data_access_id,
                    )
                )
                raise

            self._pending = HeaderReadCall(
                data_node=self.route.data_node,
                url=self.route.url,
                http_method="GET",
                response_code=served.status,
                outcome=HeaderReadOutcome.SUCCESS,
                error=None,
                bytes_requested=end - start,
                bytes_transferred=len(served.body),
                seconds=time.monotonic() - started,
                attempt_number=self._attempts,
                range_honoured=served.range_honoured,
                file_id=self.file_id,
                data_access_id=self.route.data_access_id,
            )

            return served

        return self.retrying(attempt)

    def _tell(self, call: HeaderReadCall) -> None:
        if self.observer is not None:
            self.observer(call)

    def _tell_pending(
        self, error: HeaderParseError | RangesNotHonouredError | None
    ) -> None:
        """
        Tell the observer about the call that last delivered bytes, if not yet told

        Parameters
        ----------
        error
            Why the read failed on those bytes, or `None` if it did not
        """
        if self._pending is None:
            return

        call = self._pending
        self._pending = None
        if error is not None:
            call = dataclasses.replace(call, outcome=error.outcome, error=error)

        self._tell(call)

    def finish(self, error: BaseException | None = None) -> None:
        """
        Tell the observer about the last call, now that the read has ended

        Parameters
        ----------
        error
            Why the read ended, if it failed

            A failure on the bytes themselves -- a
            [HeaderParseError][esmporium.headers.HeaderParseError], or a
            [RangesNotHonouredError][esmporium.headers.RangesNotHonouredError] raised
            without making a request -- becomes the last call's outcome. Anything
            else leaves it a success, because its bytes did arrive.
        """
        if isinstance(error, HeaderParseError | RangesNotHonouredError):
            self._tell_pending(error)
        else:
            self._tell_pending(None)

    def __enter__(self) -> RangeSource:
        """
        Use as a context manager

        Returns
        -------
        :
            This source
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Tell the observer about the last call, however the block ended"""
        self.finish(exc)


def read_header(  # noqa: PLR0913 - the keyword-only extras are deliberate injection seams
    client: httpx.Client,
    route: Route,
    *,
    time_bounds: bool = False,
    time_variable: str = DEFAULT_TIME_VARIABLE,
    block_bytes: int = DEFAULT_BLOCK_BYTES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    published_size: int | None = None,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    read_timeout: float = DEFAULT_READ_TIMEOUT_SECONDS,
    retrying: Retrying | None = None,
    observer: HeaderReadCallObserver | None = None,
    file_id: int | None = None,
) -> FileHeader:
    """
    Read one file's header over one route

    Fetches the first block of the file with a range request and reads the header
    out of it, fetching further blocks only if the reader needs them. Across sampled
    CMIP5 and CMIP6 files the whole header -- global and variable attributes -- fits
    in the first 64 KiB, so a header alone is one request.

    **Fetching and parsing are one read, not two**, which is why there is no public
    "just get me the bytes" function beside this. The question an observer needs
    answered is "did asking this node for this header work", and bytes that arrive
    and do not parse are a no.

    Parameters
    ----------
    client
        The client to request with. See [RangeSource][(m).RangeSource].

    route
        The route to read from

    time_bounds
        Whether to read the first and last values and bounds of `time_variable` too

        Each end of the time coordinate is usually stored apart from the header, so
        this costs a few more small requests. Off unless asked for.

    time_variable
        Which variable is the time coordinate

    block_bytes
        The least to fetch per request

    max_bytes
        The most to fetch from the file in total

    max_requests
        The most range requests to make of the file

    published_size
        How long the search index says the file is, used only if the node does not say

    connect_timeout
        How long to wait for the node to accept a connection

    read_timeout
        How long to wait for the bytes of one range once connected

    retrying
        The retry policy for one request, or `None` for the default

    observer
        Told about each call

    file_id
        Which [File][esmporium.db.schema.File] this is, recorded on each call

    Returns
    -------
    :
        Everything the header declares, the time values if asked for, what reading
        them cost, and the route that served them

    Raises
    ------
    HeaderReadError
        The data node did not serve the bytes. Read
        [outcome][esmporium.headers.HeaderReadError.outcome] off the error to decide
        what to try next.

    esmporium.headers.HeaderParseError
        The bytes are not a header we can read. A
        [TruncatedHeaderError][esmporium.headers.TruncatedHeaderError] here means the
        read needed more than `max_bytes` or `max_requests` allow.
    """
    with RangeSource(
        client,
        route,
        block_bytes=block_bytes,
        max_bytes=max_bytes,
        max_requests=max_requests,
        published_size=published_size,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        retrying=retrying,
        observer=observer,
        file_id=file_id,
    ) as source:
        header = read_file_header(
            source, time_bounds=time_bounds, time_variable=time_variable
        )

    return dataclasses.replace(header, route=route)
