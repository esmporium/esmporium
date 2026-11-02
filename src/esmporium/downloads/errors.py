"""
What can go wrong fetching bytes from a data node

Every error here except [UnfetchableRouteError][(m).] subclasses
[DataNodeFetchError][(m).], and each one maps onto exactly one
[DownloadOutcome][esmporium.datasets.DownloadOutcome]. That pairing is the whole
point: the retry policy and the mirror-fallback loop both decide what to do next by
looking at the outcome, so a failure which cannot be classified cannot be acted on.

Classification is by **HTTP status code and exception type**, never by matching
substrings in an exception's message. The prototype this replaces decided whether a
connection error was retryable with `"refused" in str(exc).lower()`, which depends on
the locale and the version of the library that raised it.
"""

from __future__ import annotations

from esmporium.datasets import DownloadOutcome


class DataNodeFetchError(OSError):
    """
    Base for every way one attempt at one route can fail

    Subclasses `OSError` rather than `Exception` because that is what a failed attempt
    at reading bytes *is*, and because it lets a caller which does not care why
    catch the lot with one familiar type.

    Carries the [outcome][(c).outcome] it corresponds to, so that whatever recorded
    the attempt does not have to re-derive it from the exception's class.
    """

    outcome: DownloadOutcome
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

            Carried on the error so that whatever records the attempt can write it
            down without re-deriving it. `None` means nothing answered at all -- a
            timeout or a transport fault -- which is the distinction
            [DataNodeCallRecord.response_code][esmporium.db.schema.DataNodeCallRecord.response_code]
            relies on, so losing it here would make a 404 and a dead host look the
            same in the database.
        """
        self.url = url
        self.detail = detail
        self.response_code = response_code
        # One argument to `OSError`, deliberately. Passing two makes the first the
        # `errno`, so `str(exc)` renders as `[Errno https://...] detail` -- which is
        # what the prototype did, and it put that nonsense in the database.
        super().__init__(f"{detail}: {url}")


class RouteMissingError(DataNodeFetchError):
    """
    The data node says there is nothing at this URL (404 or 410)

    Not retried, and not counted against the data node: see
    [DownloadOutcome.MISSING][esmporium.datasets.DownloadOutcome.MISSING].
    """

    outcome = DownloadOutcome.MISSING


class NeedsCredentialsError(DataNodeFetchError):
    """
    The data node wants us to authenticate (401)

    esmporium fetches anonymously; a caller supplies credentials by passing an
    already-authenticated `httpx.Client`. See
    [DownloadOutcome.NEEDS_CREDENTIALS][esmporium.datasets.DownloadOutcome.NEEDS_CREDENTIALS].
    """

    outcome = DownloadOutcome.NEEDS_CREDENTIALS


class DataNodeBlockedError(DataNodeFetchError):
    """
    The data node is refusing us for now (403, 429 or 503)

    Carries [retry_after_seconds][(c).retry_after_seconds] when the node said how long
    to wait, because a node that tells us is a node worth listening to.
    """

    outcome = DownloadOutcome.BLOCKED

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


class DataNodeTimeoutError(DataNodeFetchError):
    """
    The data node stopped sending, or never started

    See [DownloadOutcome.TIMEOUT][esmporium.datasets.DownloadOutcome.TIMEOUT] for why
    this is measured per read rather than over the whole transfer.
    """

    outcome = DownloadOutcome.TIMEOUT


class DataNodeHostFaultError(DataNodeFetchError):
    """
    We never reached the data node at all: DNS, TLS, or a refused connection
    """

    outcome = DownloadOutcome.HOST_FAULT


class ChecksumMismatchError(DataNodeFetchError):
    """
    The bytes arrived in full and did not match the published digest

    The one failure that says the data node served bad data. Whatever arrived is
    discarded rather than kept.
    """

    outcome = DownloadOutcome.CHECKSUM_FAILED

    def __init__(
        self,
        url: str,
        *,
        expected: str,
        got: str,
        algorithm: str,
        response_code: int | None = None,
    ) -> None:
        """
        Initialise the error

        Parameters
        ----------
        url
            The URL the bytes came from

        expected
            The digest the search API published

        got
            The digest of what actually arrived

        algorithm
            Which algorithm both digests are of

        response_code
            The status code the data node answered with. Normally a success code:
            this failure happens *after* a perfectly good HTTP response.
        """
        self.expected = expected
        self.got = got
        self.algorithm = algorithm
        super().__init__(
            url,
            f"the bytes arrived but their {algorithm} digest is {got}, "
            f"where {expected} was published",
            response_code=response_code,
        )


class ShortReadError(DataNodeFetchError):
    """
    The transfer was cut off: fewer bytes arrived than the data node promised

    "Promised" is the response's own `Content-Length`, so this is the node failing to
    deliver what it said it would. Compare
    [SizeMismatchError][(m).SizeMismatchError], which is the node delivering exactly
    what it promised when the *index* promised something else.
    """

    outcome = DownloadOutcome.SHORT_READ

    def __init__(
        self, url: str, *, expected: int, got: int, response_code: int | None = None
    ) -> None:
        """
        Initialise the error

        Parameters
        ----------
        url
            The URL the bytes came from

        expected
            How many bytes the search API said there would be

        got
            How many actually arrived

        response_code
            The status code the data node answered with
        """
        self.expected = expected
        self.got = got
        super().__init__(
            url,
            f"only {got} of the published {expected} bytes arrived",
            response_code=response_code,
        )


class DataNodeTransportError(DataNodeFetchError):
    """
    Any other transient failure, e.g. a 5xx which is not 503, or a reset connection
    """

    outcome = DownloadOutcome.TRANSPORT_ERROR


class UnfetchableRouteError(ValueError):
    """
    Raised when asked to fetch a route which is not bytes over HTTPS

    **Deliberately not a [DataNodeFetchError][(m).]**, because nothing was attempted
    and no data node is implicated. Trying an OPeNDAP landing page or a Globus
    web-app link is a mistake in the calling code, not a failure of the network, so it
    should not be retried, should not fall through to the next mirror, and should not
    appear in a data node's health.

    `DataAccess` records every route an API published, including ones that are not
    fetchable -- that is the point of recording them -- so something has to say no.
    """

    def __init__(self, url: str, *, service: str, container_format: str) -> None:
        """
        Initialise the error

        Parameters
        ----------
        url
            The route's URL

        service
            How the route says its bytes move. See
            [AccessService][esmporium.datasets.AccessService].

        container_format
            What the route says its bytes are. See
            [ContainerFormat][esmporium.datasets.ContainerFormat].
        """
        self.url = url
        self.service = service
        self.container_format = container_format
        super().__init__(
            f"Cannot fetch {url!r}: it is a {service!r} route to "
            f"{container_format!r}, and only HTTPS routes to actual bytes can be "
            "downloaded. An OPeNDAP URL is a landing page, a Globus URL is a web app "
            "or a non-URL endpoint reference, and a GridFTP URL needs a client this "
            "package does not depend on. Filter the routes you pass in, e.g. with "
            "esmporium.downloads.is_fetchable."
        )


class SizeMismatchError(DataNodeFetchError):
    """
    The data node served a complete file of a different size than the index published

    The transfer was fine -- exactly `Content-Length` bytes arrived -- so what is
    wrong is the published metadata: the file on the node is not the file the search
    index describes.

    Kept apart from [ShortReadError][(m).ShortReadError] because only one of them is
    the data node's fault, and `esmporium` ranks data nodes by their fault rate. See
    [DownloadOutcome.SIZE_MISMATCH][esmporium.datasets.DownloadOutcome.SIZE_MISMATCH]
    for the live example that prompted the split.
    """

    outcome = DownloadOutcome.SIZE_MISMATCH

    def __init__(
        self,
        url: str,
        *,
        published: int,
        served: int,
        response_code: int | None = None,
    ) -> None:
        """
        Initialise the error

        Parameters
        ----------
        url
            The URL the bytes came from

        published
            How many bytes the search index said there would be

        served
            How many the data node served, completely

        response_code
            The status code the data node answered with
        """
        self.published = published
        self.served = served
        super().__init__(
            url,
            f"the data node served a complete file of {served} bytes, but the search "
            f"index publishes {published}: the metadata and the file disagree, so "
            "this is a publication fault rather than a bad data node",
            response_code=response_code,
        )
