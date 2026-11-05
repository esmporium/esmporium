"""
What can go wrong reading a file's header from a data node

Classification is by **HTTP status code and exception type**, never by matching
substrings in an exception's message -- the same rule
[esmporium.downloads.errors][] states, and worth restating here because the prototype
this replaces had no choice but to break it. It read headers through a netCDF library
which did its own HTTP, so an HTTP status reached it only as text inside an `OSError`
and had to be recovered by searching that text for `"429"` or `"ssl"`. Doing the
requests ourselves is what makes a real taxonomy possible.

The errors come in two families, and the split is the one that matters when deciding
what to do next. A [HeaderReadError][(m).HeaderReadError] is about the **data node**:
somebody else's machine did or did not answer, and another mirror may do better. A
[HeaderParseError][(m).HeaderParseError] is about the **bytes**: every mirror of a file
serves the same bytes, so trying another one cannot help.
"""

from __future__ import annotations

from esmporium.datasets import HeaderReadOutcome


class HeaderReadError(OSError):
    """
    Base for every way one attempt to read one route's header can fail

    The twin of [DataNodeFetchError][esmporium.downloads.DataNodeFetchError], with the
    same three pieces of state, so that whatever records an attempt can treat the two
    alike.
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
    going to on the second ask, and its other URLs hang the same way. That is the
    whole difference between this and
    [DataNodeTimeoutError][esmporium.downloads.DataNodeTimeoutError], which retries
    because finishing a large transfer is worth another go.
    """

    outcome = HeaderReadOutcome.TIMEOUT


class HeaderNodeHostFaultError(HeaderReadError):
    """
    We never reached the data node: DNS, TLS, or a connection that timed out

    **A refused connection is deliberately not one of these** -- it is a
    [HeaderTransportError][(m).HeaderTransportError] -- because a refusal can be a
    momentarily overloaded node where a DNS or TLS failure cannot. Retrying the
    latter gives the identical failure and its other URLs fail identically, so this
    gives up on the node; the former keeps its retries. Getting that distinction
    backwards is what the prototype measured at roughly seven and a half minutes
    wasted per simulation per dead node.
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


class HeaderParseError(ValueError):
    """
    Base for every way a prefix of bytes can fail to yield a header

    A `ValueError` rather than an `OSError`, and **not** a
    [HeaderReadError][(m).HeaderReadError]: nothing is wrong with the transfer, and no
    data node is implicated. The bytes arrived exactly as asked for and are not what
    we need.

    That distinction decides what happens next. A node-level failure means try
    another mirror; one of these means do not, because every mirror of a file serves
    the same bytes. The one exception is
    [UnreadableHeaderError][(m).UnreadableHeaderError], where the bytes are *corrupt*
    rather than merely unusable, and another copy genuinely may be fine.

    Carries no URL, deliberately, because a parser is handed bytes and is told nothing
    about where they came from. Whatever orchestrates the read knows the route and
    records it.
    """

    outcome: HeaderReadOutcome
    """Which outcome this failure is. Set by each subclass."""


class TruncatedHeaderError(HeaderParseError):
    """
    The bytes are a format we understand, and there are not enough of them

    **Asking for more is the fix; another mirror is not.** Where a file keeps its
    header is a property of the file, so every replica of it needs the same number of
    bytes. Not counted against the data node, which served precisely what we asked
    for.

    In practice this should be rare: a netCDF-3 file's global attributes end a few
    kilobytes in, and netCDF-4/HDF5 files sampled from ESGF needed under 42 KiB
    against a default ask of 64 KiB. It exists because HDF5 does not *promise* to put
    the root group's object header near the start.
    """

    outcome = HeaderReadOutcome.TRUNCATED_HEADER

    def __init__(self, bytes_needed: int, *, bytes_given: int) -> None:
        """
        Initialise the error

        Parameters
        ----------
        bytes_needed
            How many bytes from the start of the file the reader needed

            A **lower bound**, not an answer: it is how far the reader had got when it
            ran out, and reading on from there may need more still. So a caller
            escalating on this should ask for comfortably more than this, not exactly
            it.

        bytes_given
            How many bytes the reader was given
        """
        self.bytes_needed = bytes_needed
        self.bytes_given = bytes_given
        super().__init__(
            f"the header needs at least {bytes_needed} bytes from the start of the "
            f"file and only {bytes_given} were fetched. That is a lower bound, so "
            "fetch comfortably more than it rather than exactly it."
        )


class UnreadableHeaderError(HeaderParseError):
    """
    The bytes announce a format we understand and then do not parse as it

    A corrupt or mangled header. **Counted against the data node**, and the one
    [HeaderParseError][(m).HeaderParseError] where another mirror is worth trying:
    this is bad data rather than too little of it, which is the same argument
    [ChecksumMismatchError][esmporium.downloads.ChecksumMismatchError] makes.
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


class HeaderParserUnavailableError(UnsupportedHeaderFormatError):
    """
    The bytes are netCDF-4/HDF5 and the optional `headers` extra is not installed

    A subclass of [UnsupportedHeaderFormatError][(m).UnsupportedHeaderFormatError]
    because the consequence is identical -- we cannot read this file, and no mirror or
    retry changes that -- but it is a *different problem*, and one the person running
    this can fix in one command. Hence its own type and its own message.

    Reading netCDF-4/HDF5 attributes needs `h5py`, which is an optional extra rather
    than a dependency: a CMIP5 archive is mostly classic netCDF and needs nothing
    installed, so making every user carry a compiled HDF5 library would be charging
    all of them for one project's file format. CMIP6 is entirely netCDF-4, so anyone
    reading CMIP6 headers does need it.
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
            "this file is netCDF-4/HDF5, and reading its attributes needs h5py, "
            "which is not installed. Install the optional extra with "
            "`pip install esmporium[headers]` (or `uv add 'esmporium[headers]'`). "
            "Classic netCDF files need nothing installed, which is why h5py is an "
            "extra rather than a dependency.",
        )
