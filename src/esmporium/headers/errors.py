"""
What can go wrong reading a file's header, from a data node or anywhere else
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from esmporium.datasets import HeaderReadOutcome

if TYPE_CHECKING:
    from collections.abc import Mapping


class HeaderReadError(OSError):
    """
    Base for every way one attempt to read one route's header can fail
    """

    outcome: HeaderReadOutcome
    """Which outcome this failure is. Set by each subclass."""

    def __init__(
        self, url: str, detail: str, *, response_code: int | None = None
    ) -> None:
        """
        Initialise the error

        Parameters
        ----------
        url
            The URL the attempt went to

        detail
            What went wrong, as a sentence fragment

        response_code
            The HTTP status code the data node answered with, if it answered

            `None` means nothing answered at all -- a timeout or a host fault -- which
            is the distinction
            [HeaderReadCallRecord.response_code][esmporium.db.schema.HeaderReadCallRecord.response_code]
            relies on, so losing it here would make a 404 and a dead host look the
            same in the database.
        """
        self.url = url
        self.detail = detail
        self.response_code = response_code
        # One argument to `OSError`, deliberately. Passing two makes the first the
        # `errno`, so `str(exc)` renders as `[Errno https://...] detail`.
        super().__init__(f"{detail}: {url}")


class HeaderRouteMissingError(HeaderReadError):
    """
    The data node says there is nothing at this URL (404 or 410)

    Not retried, not counted against the data node, and it does **not** stop us trying
    that node's other routes: this is an answer about one file. See
    [HeaderReadOutcome.MISSING][esmporium.datasets.HeaderReadOutcome.MISSING].
    """

    outcome = HeaderReadOutcome.MISSING


class HeaderNeedsCredentialsError(HeaderReadError):
    """
    The data node wants us to authenticate (401)

    Has never been observed: header reads go anonymously to public THREDDS
    `fileServer` endpoints. A caller supplies credentials by passing an
    already-authenticated `httpx.Client`, as for downloads.
    """

    outcome = HeaderReadOutcome.NEEDS_CREDENTIALS


class HeaderNodeBlockedError(HeaderReadError):
    """
    The data node is refusing us for now (403, 429 or 503)

    The one failure here worth retrying against the same route. Carries
    `retry_after_seconds` when the node said how long to wait, because a node that
    tells us is a node worth listening to.
    """

    outcome = HeaderReadOutcome.BLOCKED

    def __init__(
        self,
        url: str,
        detail: str,
        *,
        response_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        """
        Initialise the error

        Parameters
        ----------
        url
            The URL the attempt went to

        detail
            What went wrong

        response_code
            The status code the data node answered with

        retry_after_seconds
            How long the data node asked us to wait, if it sent a `Retry-After`
        """
        self.retry_after_seconds = retry_after_seconds
        super().__init__(url, detail, response_code=response_code)


class HeaderReadTimeoutError(HeaderReadError):
    """
    The data node connected and then stopped sending, or never connected

    **Not retried against the same route, and it abandons that node's other routes
    too.** A header is a few kilobytes; a node which cannot send that much is not
    going to on the second ask, and its other URLs hang the same way.
    """

    outcome = HeaderReadOutcome.TIMEOUT


class HeaderNodeHostFaultError(HeaderReadError):
    """
    We never reached the data node: DNS, TLS, or a connection that timed out

    **A refused connection is deliberately not one of these** -- it is a
    [HeaderTransportError][(m).HeaderTransportError] -- because a refusal can be a
    momentarily overloaded node where a DNS or TLS failure cannot. Retrying the
    latter gives the identical failure and its other URLs fail identically, so this
    gives up on the node; the former keeps its retries.
    """

    outcome = HeaderReadOutcome.HOST_FAULT


class HeaderTransportError(HeaderReadError):
    """
    Any other transient failure: a 5xx which is not 503, or a refused connection

    The catch-all for "worth trying again", and the home of a plain connection
    refusal. See [HeaderNodeHostFaultError][(m).HeaderNodeHostFaultError] for why a
    refusal lands here rather than there.
    """

    outcome = HeaderReadOutcome.TRANSPORT_ERROR


class RangesNotHonouredError(HeaderReadError):
    """
    The data node will not serve part of a file, only all of it from the start

    A node which answers a range request with `200` and the whole body still gives us
    the start of the file, so reading a header that fits in that first block works.
    Anything further in would mean streaming the file from byte zero to reach it,
    which turns a header read into a download, so it is refused instead.

    **Stops this data node**, whose other routes are served the same way, and is not
    retried. Another mirror may well honour ranges.
    """

    outcome = HeaderReadOutcome.RANGES_NOT_HONOURED


class HeaderParseError(ValueError):
    """
    Base for every way a file's bytes can fail to yield a header

    A `ValueError` rather than an `OSError`, and **not** a
    [HeaderReadError][(m).HeaderReadError]: nothing is wrong with the transfer, and no
    data node is implicated. The bytes arrived exactly as asked for and are not what
    we need.
    """

    outcome: HeaderReadOutcome
    """Which outcome this failure is. Set by each subclass."""


class TruncatedHeaderError(HeaderParseError):
    """
    The bytes are a format we understand, and reading them needs more than we may have

    Raised when a read needs bytes the source does not hold, or which fetching would
    take past the source's budget of bytes or requests.

    **A bigger budget is the fix; another mirror is not.** Where a file keeps its
    header and its time values is a property of the file, so every replica of it
    needs the same bytes. Not counted against the data node, which served precisely
    what we asked for.
    """

    outcome = HeaderReadOutcome.TRUNCATED_HEADER

    def __init__(self, bytes_needed: int, *, reason: str) -> None:
        """
        Initialise the error

        Parameters
        ----------
        bytes_needed
            How far into the file the read needed to go, as an offset one past the
            last byte it wanted

            A **lower bound**, not an answer: it is how far the reader had got when it
            ran out, and reading on from there may need more still.

        reason
            Why those bytes could not be had, as a sentence fragment
        """
        self.bytes_needed = bytes_needed
        self.reason = reason
        super().__init__(
            f"reading this file needs its bytes up to offset {bytes_needed}, and "
            f"{reason}. That offset is a lower bound: reading on from it may need "
            "more."
        )


class UnreadableHeaderError(HeaderParseError):
    """
    The bytes announce a format we understand and then do not parse as it

    A corrupt or mangled header. **Counted against the data node**, and the one
    [HeaderParseError][(m).HeaderParseError] where another mirror is worth trying:
    this is bad data rather than too little of it.
    """

    outcome = HeaderReadOutcome.UNREADABLE_BYTES


class UnsupportedHeaderFormatError(HeaderParseError):
    """
    The bytes are not a format this package can read a header out of

    Not the data node's fault and not worth retrying anywhere: every mirror of a file
    has the same first four bytes.
    """

    outcome = HeaderReadOutcome.UNSUPPORTED_FORMAT

    def __init__(self, magic: bytes) -> None:
        """
        Initialise the error

        Parameters
        ----------
        magic
            The first few bytes of the file, which named a format we do not read
        """
        self.magic = magic
        super().__init__(
            f"the file starts with {magic!r}, which is neither classic netCDF "
            r"(`CDF\x01` or `CDF\x02`) nor netCDF-4/HDF5 "
            r"(`\x89HDF\r\n\x1a\n`), the two layouts ESGF serves"
        )


class HeaderReaderUnavailableError(UnsupportedHeaderFormatError):
    """
    The bytes are netCDF-4/HDF5 and the optional `headers` extra is not installed

    A subclass of [UnsupportedHeaderFormatError][(m).UnsupportedHeaderFormatError]
    because the consequence is identical -- we cannot read this file, and no mirror or
    retry changes that -- but it is a *different problem*, and one the person running
    this can fix in one command. Hence its own type and its own message.

    Reading netCDF-4/HDF5 needs `h5netcdf` and the compiled `h5py` beneath it, which
    are an optional extra rather than a dependency: a CMIP5 archive is mostly classic
    netCDF and needs nothing installed, so making every user carry a compiled HDF5
    library would be charging all of them for one project's file format. CMIP6 is
    entirely netCDF-4, so anyone reading CMIP6 headers does need it.
    """

    def __init__(self, magic: bytes) -> None:
        """
        Initialise the error

        Parameters
        ----------
        magic
            The first few bytes of the file, which named netCDF-4/HDF5
        """
        # Deliberately not calling `UnsupportedHeaderFormatError.__init__`: that one
        # says the format is unreadable, and this one says it is readable once
        # something is installed. Only the attribute assignment is shared.
        self.magic = magic
        HeaderParseError.__init__(
            self,
            "this file is netCDF-4/HDF5, and reading it needs h5netcdf, which is "
            "not installed. Install the optional extra with "
            "`pip install esmporium[headers]` (or `uv add 'esmporium[headers]'`). "
            "Classic netCDF files need nothing installed, which is why h5netcdf is "
            "an extra rather than a dependency.",
        )


class NoSuchVariableError(LookupError):
    """
    A file has no variable of the name asked for
    """

    def __init__(self, name: str, *, found: tuple[str, ...]) -> None:
        """
        Initialise the error

        Parameters
        ----------
        name
            The variable asked for

        found
            Every variable the file does have, in the order it declares them
        """
        self.name = name
        self.found = found
        listed = ", ".join(found) or "none"
        super().__init__(
            f"this file has no variable called {name!r}. Variables found: {listed}."
        )


class MissingVariableAttributesError(LookupError):
    """
    A variable does not carry every attribute asked for
    """

    def __init__(
        self, name: str, *, missing: tuple[str, ...], found: Mapping[str, object]
    ) -> None:
        """
        Initialise the error

        Parameters
        ----------
        name
            The variable whose attributes were read

        missing
            The attributes asked for and not found

        found
            Every attribute the variable does carry
        """
        self.name = name
        self.missing = missing
        self.found = found
        listed = ", ".join(found) or "none"
        super().__init__(
            f"the variable {name!r} does not carry the attribute(s) "
            f"{', '.join(repr(each) for each in missing)}. Attributes found: "
            f"{listed}."
        )
