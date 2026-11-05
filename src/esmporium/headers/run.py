"""
Reading many files' headers, across routes, nodes and threads

The shape is deliberately the same as [esmporium.downloads.run][]: a serial default, an
opt-in thread pool, results keyed by position so order survives the threads, and a
per-file loop which **returns** a failure rather than raising -- one file failing is
not a reason to abandon a run of ten thousand. That last point is not a stylistic
choice. The prototype before us drove its parent searches through
`list(executor.map(...))`, which re-raises the first worker's exception, so a single
`504` from one index node killed an entire parent hop and lost every other result in
it.

What this adds, and the download layer has no need for, is **giving up on a data node
rather than only on a route**. A node that is unreachable or hanging fails its other
URLs identically, so walking them spends the time that should have gone on the next
mirror. That is the "death spiral" the prototype's post-mortem is named after: it tried
every file of every variable against one dead node, 660 to 1162 URLs, three attempts
each, single file at a time -- about 36 minutes for one node of one simulation, during
which the simulations that *would* have worked were starved. Here
[stops_this_data_node][esmporium.datasets.HeaderReadOutcome.stops_this_data_node]
settles it after the first such failure.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from esmporium.datasets import HeaderReadOutcome
from esmporium.downloads import (
    DEFAULT_DATA_NODE_SELECTOR,
    DataNodeLimiter,
    SelectorOfferedNoRouteError,
    build_data_node_limiter,
    no_data_node_limit,
)
from esmporium.headers.errors import HeaderParseError, HeaderReadError
from esmporium.headers.fetching import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_MAX_WANT_BYTES,
    DEFAULT_READ_TIMEOUT_SECONDS,
    DEFAULT_WANT_BYTES,
    FileHeader,
    read_header,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from tenacity import Retrying

    from esmporium.downloads import DataNodeSelector, Route
    from esmporium.headers.health import HeaderReadCallObserver

logger = logging.getLogger(__name__)

DEFAULT_PER_DATA_NODE = 2
"""
How many header reads may run at once against any one data node, by default

Half the download layer's four, and lower for a reason that is about politeness rather
than throughput. A download holds one connection and streams; a header read opens a
connection, asks for a few kilobytes and closes it, so the same number in flight is a
much heavier pattern of connection churn on a shared archive. The prototype settled on
two for the same reason, and grew it only where a node proved it could take more.
"""

_PERMANENT_OUTCOMES = frozenset(
    {
        HeaderReadOutcome.MISSING,
        HeaderReadOutcome.NEEDS_CREDENTIALS,
        # Both are facts about the file rather than about any node, so every mirror
        # of it will answer the same way and a later run will too.
        HeaderReadOutcome.UNSUPPORTED_FORMAT,
        HeaderReadOutcome.TRUNCATED_HEADER,
    }
)
"""
Outcomes which will not change by trying again later

Note what is **not** here: [UNREADABLE_BYTES][esmporium.datasets.HeaderReadOutcome.UNREADABLE_BYTES]
is corrupt data rather than an unreadable format, and another copy of the file may well
be fine -- the same argument the download layer makes for a failed checksum.
"""  # noqa: E501


@dataclass(frozen=True)
class HeaderRequest:
    """
    One file whose header we want, and everything needed to go and get it

    Deliberately carries no database rows, only ids, so this layer stays testable
    without a database -- the same rule
    [DownloadRequest][esmporium.downloads.DownloadRequest] follows.
    """

    routes: tuple[Route, ...]
    """
    Every route we know of for this file

    Passed whole rather than pre-filtered, because choosing between them is the
    selector's job. [prepare_routes][esmporium.downloads.prepare_routes] drops what
    cannot be fetched and prefers HTTPS.
    """

    file_id: int | None = None
    """
    Which [File][esmporium.db.schema.File] this is

    Recorded on every observed attempt, and -- once a header is read -- on the parent
    facts that came out of it, so a fact read out of bytes stays traceable to the
    bytes.
    """

    filename: str | None = None
    """What to call this file in a log line, when there is something to call it"""

    published_size: int | None = None
    """
    How long the search index says the file is

    Only a fallback for when a data node does not report a size. See
    [read_header][esmporium.headers.read_header].
    """

    @property
    def label(self) -> str:
        """
        Something to call this file in a log line

        Returns
        -------
        :
            The filename if there is one, and otherwise the file id or a placeholder
        """
        if self.filename is not None:
            return self.filename

        return f"file {self.file_id}" if self.file_id is not None else "an unnamed file"


@dataclass(frozen=True)
class RouteFailure:
    """One route that was tried for a header and did not serve it"""

    route: Route
    """The route that was tried"""

    outcome: HeaderReadOutcome
    """How it failed"""

    error: Exception
    """The exception it failed with, kept live so a caller can inspect it"""


@dataclass(frozen=True)
class HeaderReadResult:
    """
    What came of trying to read one file's header

    Describes a failure as readily as a success: a file no route served comes back
    with `header=None` and every failure recorded, rather than raising.
    """

    request: HeaderRequest
    """What was asked for"""

    header: FileHeader | None = None
    """The header, or `None` if no route served one"""

    failures: tuple[RouteFailure, ...] = ()
    """
    Every route that was tried and failed, in the order they were tried

    Kept in full, including on a success: "it took four mirrors" is worth knowing, and
    whether to give up depends on *all* of them rather than only the last.
    """

    @property
    def succeeded(self) -> bool:
        """
        Whether a header was read

        Returns
        -------
        :
            `True` if there is a header
        """
        return self.header is not None

    @property
    def give_up(self) -> bool:
        """
        Whether this is worth trying again in a later run

        Returns
        -------
        :
            `True` if there is nothing to gain by asking again

            A read with no failures at all and no header counts as giving up: nothing
            was tried, so trying again changes nothing.
        """
        if self.succeeded:
            return False

        if not self.failures:
            return True

        return all(failure.outcome in _PERMANENT_OUTCOMES for failure in self.failures)

    @property
    def last_error(self) -> Exception | None:
        """
        The most recent failure's exception, if anything failed

        Returns
        -------
        :
            The exception, or `None` if nothing failed
        """
        return self.failures[-1].error if self.failures else None


ResultCallback = Callable[[HeaderReadResult], None]
"""
Told about each file as soon as its header is read or given up on

The seam a database layer writes through, one file at a time as a run proceeds rather
than all at the end. The prototype lost an hour and three quarters of header reads to
a run that was killed before its single final write, which is the whole reason this is
a callback rather than a return value.

Called from whichever thread finished that file, so an implementation touching anything
shared has to be safe to call from several threads at once. A database-backed one is,
because it opens and commits its own session per call.
"""


def read_one_header(  # noqa: PLR0913 - the keyword-only extras are deliberate injection seams
    client: httpx.Client,
    request: HeaderRequest,
    *,
    selector: DataNodeSelector = DEFAULT_DATA_NODE_SELECTOR,
    observer: HeaderReadCallObserver | None = None,
    max_routes: int | None = None,
    limiter: DataNodeLimiter = no_data_node_limit,
    want_bytes: int = DEFAULT_WANT_BYTES,
    max_want_bytes: int = DEFAULT_MAX_WANT_BYTES,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    read_timeout: float = DEFAULT_READ_TIMEOUT_SECONDS,
    retrying: Retrying | None = None,
) -> HeaderReadResult:
    """
    Read one file's header, working through its routes until one serves it

    Parameters
    ----------
    client
        The client to request with

    request
        The file to read, and its routes

    selector
        Which route to try next, given every route and a 0-based attempt index

        See [DataNodeSelector][esmporium.downloads.DataNodeSelector]. Shared with the
        download layer on purpose: which node holds a file is the same question
        whichever end of it you want.

    observer
        Told about each HTTP attempt as it happens

    max_routes
        The most routes to try before giving up, or `None` for no limit

    limiter
        Decides when a read against one data node may start

    want_bytes
        How many bytes to ask for first

    max_want_bytes
        The most to ask for before giving up on a file

    connect_timeout
        How long to wait for a node to accept a connection

    read_timeout
        How long to wait for the bytes once connected

    retrying
        The retry policy for one route, or `None` for the default

    Returns
    -------
    :
        What came of it, success or not

        A file no route served comes back with `header=None` and every failure
        recorded, rather than raising: one file failing is not a reason to abandon a
        run.

    Raises
    ------
    esmporium.downloads.SelectorOfferedNoRouteError
        The selector offered nothing at all, so nothing was tried. Asking for a header
        is asking for it to be read, and "we tried and nobody had it" is a different
        answer from "nobody was asked" -- so this one is loud.
    """
    failures: list[RouteFailure] = []
    abandoned_nodes: set[str] = set()
    offered_anything = False
    attempt = 0

    while (route := selector(request.routes, attempt)) is not None:
        offered_anything = True

        if max_routes is not None and attempt >= max_routes:
            logger.debug(
                "giving up on %s after %d route(s)",
                request.label,
                attempt,
                extra={"file_name": request.label, "max_routes": max_routes},
            )
            break

        if route.data_node in abandoned_nodes:
            # This node already failed in a way that its other URLs will repeat. Step
            # past them rather than spending a timeout each to learn the same thing.
            attempt += 1
            continue

        try:
            # The slot is held around the whole read, retries included, so a node
            # which is already busy is not asked for more while it works through what
            # it has.
            with limiter(route.data_node):
                header = read_header(
                    client,
                    route,
                    want_bytes=want_bytes,
                    max_want_bytes=max_want_bytes,
                    published_size=request.published_size,
                    connect_timeout=connect_timeout,
                    read_timeout=read_timeout,
                    retrying=retrying,
                    observer=observer,
                    file_id=request.file_id,
                )
        except (HeaderReadError, HeaderParseError) as error:
            failures.append(
                RouteFailure(route=route, outcome=error.outcome, error=error)
            )

            if error.outcome.stops_this_data_node:
                abandoned_nodes.add(route.data_node)

            logger.debug(
                "%s failed for %s (%s), trying the next route",
                route.data_node,
                request.label,
                error.outcome,
                extra={
                    "data_node": route.data_node,
                    "file_name": request.label,
                    "outcome": str(error.outcome),
                    "data_node_abandoned": error.outcome.stops_this_data_node,
                },
            )
            attempt += 1
            continue

        return HeaderReadResult(
            request=request, header=header, failures=tuple(failures)
        )

    if not offered_anything:
        raise SelectorOfferedNoRouteError(request.routes, selector)

    return HeaderReadResult(request=request, header=None, failures=tuple(failures))


def read_headers(  # noqa: PLR0913 - the keyword-only extras are deliberate injection seams
    requests: Iterable[HeaderRequest],
    *,
    client: httpx.Client | None = None,
    selector: DataNodeSelector = DEFAULT_DATA_NODE_SELECTOR,
    observer: HeaderReadCallObserver | None = None,
    max_routes: int | None = None,
    on_result: ResultCallback | None = None,
    want_bytes: int = DEFAULT_WANT_BYTES,
    max_want_bytes: int = DEFAULT_MAX_WANT_BYTES,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    read_timeout: float = DEFAULT_READ_TIMEOUT_SECONDS,
    retrying: Retrying | None = None,
    max_workers: int | None = None,
    per_data_node: int | None = DEFAULT_PER_DATA_NODE,
) -> tuple[HeaderReadResult, ...]:
    """
    Read the headers of many files, optionally several at once

    Parameters
    ----------
    requests
        The files to read

    client
        The client to request with, or `None` to build and close one

    selector
        Which route to try next for each file

    observer
        Told about each HTTP attempt as it happens

        Called from whichever thread made the attempt, so an implementation touching
        anything shared has to be safe for that.

    max_routes
        The most routes to try per file, or `None` for no limit

    on_result
        Told about each file as soon as it is finished

        See [ResultCallback][(m).ResultCallback]. This is how a long run survives
        being killed.

    want_bytes
        How many bytes to ask for first

    max_want_bytes
        The most to ask for before giving up on a file

    connect_timeout
        How long to wait for a node to accept a connection

    read_timeout
        How long to wait for the bytes once connected

    retrying
        The retry policy for one route, or `None` for the default

    max_workers
        How many files to read at once, or `None` to read them one at a time

        Serial by default, as downloading is: parallelism is worth asking for
        deliberately rather than being the thing that happens when nobody chose.

    per_data_node
        How many reads may be in flight against one data node, or `None` for no limit

        Only applied when reading in parallel, since one at a time cannot exceed it.

    Returns
    -------
    :
        One result per *request*, in the order the requests were given

        Position is preserved whatever order the threads finish in, so a result can be
        matched back to what was asked for.
    """
    ordered = tuple(requests)
    if not ordered:
        return ()

    in_parallel = max_workers is not None and max_workers > 1
    limiter = (
        build_data_node_limiter(per_data_node)
        if in_parallel and per_data_node is not None
        else no_data_node_limit
    )

    owns_client = client is None
    if client is None:
        client = httpx.Client(follow_redirects=True)

    def read_one(indexed: tuple[int, HeaderRequest]) -> tuple[int, HeaderReadResult]:
        # Carries the request's position with it, so results can be put back in the
        # order they were asked for however they come back.
        index, request = indexed
        result = read_one_header(
            client,
            request,
            selector=selector,
            observer=observer,
            max_routes=max_routes,
            limiter=limiter,
            want_bytes=want_bytes,
            max_want_bytes=max_want_bytes,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            retrying=retrying,
        )
        if on_result is not None:
            on_result(result)

        return index, result

    try:
        todo = tuple(enumerate(ordered))
        if in_parallel:
            # `map` keeps the input order and, if a worker raises, re-raises once the
            # executor has joined the rest -- so whatever the other workers handed to
            # `on_result` has already been acted on.
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                finished = list(executor.map(read_one, todo))
        else:
            finished = [read_one(each) for each in todo]
    finally:
        if owns_client:
            client.close()

    results = dict(finished)

    return tuple(results[index] for index in range(len(ordered)))
