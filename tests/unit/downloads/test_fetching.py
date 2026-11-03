"""
Tests of fetching one route's bytes to a path
"""

import httpx
import pytest

from esmporium.datasets import AccessService, ContainerFormat, DownloadOutcome
from esmporium.downloads import (
    ChecksumMismatchError,
    DataNodeBlockedError,
    DataNodeHostFaultError,
    DataNodeTimeoutError,
    DataNodeTransportError,
    NeedsCredentialsError,
    Route,
    RouteMissingError,
    ShortReadError,
    SizeMismatchError,
    UnfetchableRouteError,
    fetch_to_path,
    part_path,
)

from .conftest import NODE, URL


def test_a_plain_download_writes_the_bytes_and_verifies_them(  # noqa: PLR0913 - fixtures
    tmp_path, route, body, sha256_of, client_for, no_retry
):
    """The happy path: bytes land at `dest`, the digest is checked, no `.part` left"""
    dest = tmp_path / "tas.nc"
    client = client_for(lambda request: httpx.Response(200, content=body))

    outcome = fetch_to_path(
        client,
        route,
        dest,
        expected_size=len(body),
        checksum=sha256_of(body),
        checksum_type="SHA256",
        retrying=no_retry,
    )

    assert dest.read_bytes() == body
    assert outcome.bytes_written == len(body)
    assert outcome.size_on_disk == len(body)
    assert outcome.checksum_verified
    assert outcome.checksum_type_used == "SHA256"
    assert not outcome.resumed
    # The temporary file is gone, so nothing is left to be mistaken for a download.
    assert not part_path(dest).exists()


def test_the_parent_directory_is_created(tmp_path, route, body, client_for, no_retry):
    """A download into a tree that does not exist yet makes the tree"""
    dest = tmp_path / "CMIP7" / "CMIP" / "CCCma" / "v20261018" / "tas.nc"
    client = client_for(lambda request: httpx.Response(200, content=body))

    fetch_to_path(client, route, dest, retrying=no_retry)

    assert dest.read_bytes() == body


def test_a_file_with_no_checksum_downloads_but_is_not_verified(
    tmp_path, route, body, client_for, no_retry
):
    """No published digest is not a failure, it is an unverified success"""
    dest = tmp_path / "tas.nc"
    client = client_for(lambda request: httpx.Response(200, content=body))

    outcome = fetch_to_path(client, route, dest, retrying=no_retry)

    assert dest.read_bytes() == body
    # `False` here means "nothing to check against", never "a check failed".
    assert not outcome.checksum_verified
    assert outcome.checksum_type_used is None


def test_a_digest_with_no_algorithm_name_downloads_but_is_not_verified(  # noqa: PLR0913 - fixtures
    tmp_path, route, body, sha256_of, client_for, no_retry
):
    """
    A published digest with nothing saying how to read it is unusable, not fatal

    The index records the digest and its algorithm in two separate fields and does
    not always fill in the second. Distinct from no digest at all, which is the test
    above: here there *is* something to check against and we still cannot use it.
    """
    dest = tmp_path / "tas.nc"
    client = client_for(lambda request: httpx.Response(200, content=body))

    outcome = fetch_to_path(
        client,
        route,
        dest,
        checksum=sha256_of(body),
        checksum_type=None,
        retrying=no_retry,
    )

    assert dest.read_bytes() == body
    assert not outcome.checksum_verified
    assert outcome.checksum_type_used is None


def test_an_unknown_checksum_algorithm_downloads_but_is_not_verified(
    tmp_path, route, body, client_for, no_retry
):
    """An algorithm this Python does not have is worked around, not fatal"""
    dest = tmp_path / "tas.nc"
    client = client_for(lambda request: httpx.Response(200, content=body))

    outcome = fetch_to_path(
        client,
        route,
        dest,
        checksum="whatever",
        checksum_type="NOT-A-REAL-HASH",
        retrying=no_retry,
    )

    assert dest.read_bytes() == body
    assert not outcome.checksum_verified


def test_a_checksum_mismatch_raises_and_keeps_nothing(
    tmp_path, route, body, client_for, no_retry
):
    """Bad bytes are discarded: a wrong file is worse than a missing one"""
    dest = tmp_path / "tas.nc"
    client = client_for(lambda request: httpx.Response(200, content=body))

    with pytest.raises(ChecksumMismatchError) as caught:
        fetch_to_path(
            client,
            route,
            dest,
            checksum="00" * 32,
            checksum_type="SHA256",
            retrying=no_retry,
        )

    assert caught.value.outcome == DownloadOutcome.CHECKSUM_FAILED
    assert not dest.exists()
    assert not part_path(dest).exists()


def test_a_cut_off_transfer_is_a_short_read(
    tmp_path, route, body, client_for, no_retry
):
    """
    The node promised more than it sent, so the transfer is what failed

    `Content-Length` says the whole body, the body stops early: that is the node
    failing to deliver, so it is worth retrying and counts against the node.
    """
    dest = tmp_path / "tas.nc"
    client = client_for(
        lambda request: httpx.Response(
            200,
            content=body[:100],
            # Promises the lot, delivers 100 bytes.
            headers={"Content-Length": str(len(body))},
        )
    )

    with pytest.raises(ShortReadError) as caught:
        fetch_to_path(client, route, dest, expected_size=len(body), retrying=no_retry)

    assert caught.value.outcome == DownloadOutcome.SHORT_READ
    assert caught.value.expected == len(body)
    assert caught.value.got == 100
    assert not dest.exists()
    assert not part_path(dest).exists()


def test_a_complete_file_of_the_wrong_size_is_a_publication_fault(
    tmp_path, route, body, client_for, no_retry
):
    """
    The node delivered exactly what it promised, so the *index* is wrong

    Kept apart from a short read because only one of the two is the data node's
    fault, and nodes are ranked by their fault rate. Charging this to the node would
    steer later downloads away from a working replica over an error it had no part
    in.
    """
    dest = tmp_path / "tas.nc"
    served = body[:100]
    client = client_for(
        # A complete, consistent response -- just not the size the index claims.
        lambda request: httpx.Response(200, content=served)
    )

    with pytest.raises(SizeMismatchError) as caught:
        fetch_to_path(client, route, dest, expected_size=len(body), retrying=no_retry)

    assert caught.value.outcome == DownloadOutcome.SIZE_MISMATCH
    assert caught.value.published == len(body)
    assert caught.value.served == 100
    # Not the node's fault, so its health must not move.
    assert not DownloadOutcome.SIZE_MISMATCH.counts_against_data_node
    # And retrying would get the same bytes.
    assert not DownloadOutcome.SIZE_MISMATCH.is_worth_retrying
    assert not dest.exists()
    assert not part_path(dest).exists()


def test_an_unreadable_content_length_is_treated_as_promising_nothing(
    tmp_path, route, body, client_for, no_retry
):
    """
    A `Content-Length` which is not a number is no promise at all

    The same answer as a chunked response, and for the same reason: with nothing to
    hold the node to, a file of the wrong length is reported as a transfer problem
    and retried rather than blamed on the index. A *readable* length matching what
    arrived would have made this a `SizeMismatchError` instead.
    """
    dest = tmp_path / "tas.nc"

    def handler(request):
        return httpx.Response(
            200,
            content=body[:100],
            headers={"Content-Length": "not-a-number"},
        )

    with pytest.raises(ShortReadError) as caught:
        fetch_to_path(
            client_for(handler),
            route,
            dest,
            expected_size=len(body),
            retrying=no_retry,
        )

    assert caught.value.got == 100
    assert caught.value.expected == len(body)
    # Retryable, which is the conservative answer when there is nobody to blame.
    assert DownloadOutcome.SHORT_READ.is_worth_retrying


def test_a_chunked_response_of_the_wrong_size_is_treated_as_a_short_read(
    tmp_path, route, body, client_for, no_retry
):
    """
    With no `Content-Length` there is no promise, so blame cannot be assigned

    The conservative answer is to call it a transfer problem and allow a retry,
    rather than declaring the index wrong on no evidence.
    """
    dest = tmp_path / "tas.nc"

    def handler(request):
        # An iterator body, so httpx sends it chunked with no Content-Length.
        return httpx.Response(200, content=iter([body[:100]]))

    with pytest.raises(ShortReadError):
        fetch_to_path(
            client_for(handler),
            route,
            dest,
            expected_size=len(body),
            retrying=no_retry,
        )

    assert not dest.exists()


def test_a_file_longer_than_published_is_also_refused(
    tmp_path, route, body, client_for, no_retry
):
    """
    Longer is just as wrong as shorter, and it is the *publication* that is wrong

    An earlier version only checked `total < expected`, which would have accepted a
    duplicated body -- exactly what a mishandled resume produces.

    Which of the two size errors this is matters as much as that it raises. The node
    served exactly as many bytes as it said it would, so nothing about the transfer
    went wrong and this must not count against the node: that is
    [SizeMismatchError][esmporium.downloads.SizeMismatchError], not
    [ShortReadError][esmporium.downloads.ShortReadError]. Accepting either here would
    tolerate the one confusion that would let mis-published files retire healthy
    replicas from the health ranking.
    """
    dest = tmp_path / "tas.nc"
    client = client_for(lambda request: httpx.Response(200, content=body + body))

    with pytest.raises(SizeMismatchError) as excinfo:
        fetch_to_path(client, route, dest, expected_size=len(body), retrying=no_retry)

    assert excinfo.value.outcome == DownloadOutcome.SIZE_MISMATCH
    assert excinfo.value.published == len(body)
    assert excinfo.value.served == 2 * len(body)

    assert not dest.exists()


@pytest.mark.parametrize(
    "status, error, outcome",
    (
        (404, RouteMissingError, DownloadOutcome.MISSING),
        (410, RouteMissingError, DownloadOutcome.MISSING),
        (401, NeedsCredentialsError, DownloadOutcome.NEEDS_CREDENTIALS),
        (403, DataNodeBlockedError, DownloadOutcome.BLOCKED),
        (429, DataNodeBlockedError, DownloadOutcome.BLOCKED),
        (503, DataNodeBlockedError, DownloadOutcome.BLOCKED),
        (500, DataNodeTransportError, DownloadOutcome.TRANSPORT_ERROR),
        (418, DataNodeTransportError, DownloadOutcome.TRANSPORT_ERROR),
    ),
)
def test_status_codes_are_classified(  # noqa: PLR0913 - parametrised, plus fixtures
    tmp_path, route, client_for, no_retry, status, error, outcome
):
    """Each status code becomes the one error whose outcome says what to do next"""
    dest = tmp_path / "tas.nc"
    client = client_for(lambda request: httpx.Response(status))

    with pytest.raises(error) as caught:
        fetch_to_path(client, route, dest, retrying=no_retry)

    assert caught.value.outcome == outcome
    # The code is kept, so a 404 and an unreachable host are distinguishable later.
    assert caught.value.response_code == status


def test_a_retry_after_is_honoured(tmp_path, route, client_for, no_retry):
    """A data node which says how long to wait has that read off the response"""
    dest = tmp_path / "tas.nc"
    client = client_for(
        lambda request: httpx.Response(429, headers={"Retry-After": "7"})
    )

    with pytest.raises(DataNodeBlockedError) as caught:
        fetch_to_path(client, route, dest, retrying=no_retry)

    assert caught.value.retry_after_seconds == 7.0


def test_an_unreadable_retry_after_is_ignored_rather_than_fatal(
    tmp_path, route, client_for, no_retry
):
    """
    The HTTP-date form of `Retry-After` falls back to ordinary backoff

    Only the delay-seconds form is read. Nothing in ESGF sends a date, and falling
    back is correct behaviour rather than a gap.
    """
    dest = tmp_path / "tas.nc"
    client = client_for(
        lambda request: httpx.Response(
            503, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
        )
    )

    with pytest.raises(DataNodeBlockedError) as caught:
        fetch_to_path(client, route, dest, retrying=no_retry)

    assert caught.value.retry_after_seconds is None


@pytest.mark.parametrize(
    "raised, error, outcome",
    (
        (
            httpx.ConnectTimeout("too slow"),
            DataNodeTimeoutError,
            DownloadOutcome.TIMEOUT,
        ),
        (httpx.ReadTimeout("stalled"), DataNodeTimeoutError, DownloadOutcome.TIMEOUT),
        (
            httpx.ConnectError("no route to host"),
            DataNodeHostFaultError,
            DownloadOutcome.HOST_FAULT,
        ),
        (
            httpx.RemoteProtocolError("peer closed"),
            DataNodeTransportError,
            DownloadOutcome.TRANSPORT_ERROR,
        ),
    ),
)
def test_transport_failures_are_classified(  # noqa: PLR0913 - parametrised, plus fixtures
    tmp_path, route, client_for, no_retry, raised, error, outcome
):
    """
    Transport failures are classified by exception *type*

    Never by matching substrings in the message, which is what the prototype did
    (`"refused" in str(exc).lower()`) and which depends on the locale and the version
    of the library that raised it.
    """
    dest = tmp_path / "tas.nc"

    def handler(request):
        raise raised

    with pytest.raises(error) as caught:
        fetch_to_path(client_for(handler), route, dest, retrying=no_retry)

    assert caught.value.outcome == outcome
    # Nothing answered, so there is no status code -- which is how this is told apart
    # from a node that said no.
    assert caught.value.response_code is None


@pytest.mark.parametrize(
    "service, container_format",
    (
        (AccessService.OPENDAP, ContainerFormat.HTML),
        (AccessService.GLOBUS, ContainerFormat.HTML),
        (AccessService.GRIDFTP, ContainerFormat.NETCDF),
        (AccessService.HTTPS, ContainerFormat.ZARR),
        (AccessService.HTTPS, ContainerFormat.ICECHUNK),
    ),
)
def test_an_unfetchable_route_is_refused_before_anything_is_attempted(
    tmp_path, client_for, no_retry, service, container_format
):
    """
    A route that is not bytes over HTTPS raises, and is not a data node failure

    `UnfetchableRouteError` is deliberately not a `DataNodeFetchError`: nothing was
    attempted, so it must not count against a node or fall through to the next mirror.
    """
    dest = tmp_path / "tas.nc"

    def handler(request):  # pragma: no cover - must never be reached
        msg = "an unfetchable route must not be requested"
        raise AssertionError(msg)

    route = Route(
        url=URL,
        data_node=NODE,
        service=service,
        container_format=container_format,
    )

    with pytest.raises(UnfetchableRouteError):
        fetch_to_path(client_for(handler), route, dest, retrying=no_retry)
