"""
Tests of what a fetch observes about each attempt

The point of recording per *attempt* rather than per download is that a retried fetch
leaves a trail. These tests pin that: that every attempt is seen, that the attempt
numbers count up, and that what is recorded is enough to tell a 404 from a dead host.
"""

import httpx
import pytest

from esmporium.datasets import AccessService, ContainerFormat, DownloadOutcome
from esmporium.downloads import (
    DataNodeCall,
    DataNodeTimeoutError,
    Route,
    RouteMissingError,
    UnfetchableRouteError,
    fan_out,
    fetch_to_path,
)

from .conftest import NODE, URL


def test_a_success_is_observed_once(  # noqa: PLR0913 - fixtures
    tmp_path, route, body, client_for, no_retry, collect_calls
):
    """One attempt, one observation, carrying the route and the bytes moved"""
    observer, calls = collect_calls
    client = client_for(lambda request: httpx.Response(200, content=body))

    fetch_to_path(
        client,
        route,
        tmp_path / "tas.nc",
        expected_size=len(body),
        retrying=no_retry,
        observer=observer,
        file_id=42,
    )

    (call,) = calls
    assert call.outcome == DownloadOutcome.SUCCESS
    assert call.success
    # The data node, not the URL's host: health is tracked against the node.
    assert call.data_node == NODE
    assert call.url == URL
    assert call.http_method == "GET"
    assert call.response_code == 200
    assert call.error is None
    assert call.bytes_transferred == len(body)
    assert call.seconds >= 0.0
    assert call.attempt_number == 1
    assert not call.resumed
    assert call.file_id == 42
    assert call.data_access_id == 11


def test_every_retry_is_observed_with_its_own_attempt_number(  # noqa: PLR0913 - fixtures
    tmp_path, route, body, client_for, collect_calls, fast_retrying
):
    """
    A fetch that succeeds on the third go leaves three observations

    Recording inside the retry loop rather than around it is what makes this true,
    and it is what lets a node's flakiness be measured rather than hidden by an
    eventual success.
    """
    observer, calls = collect_calls
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(500)
        return httpx.Response(200, content=body)

    fetch_to_path(
        client_for(handler),
        route,
        tmp_path / "tas.nc",
        # Retries really happen, but without the real policy's backoff: the rule
        # being tested is "every attempt is observed", not how long we wait.
        retrying=fast_retrying(3),
        observer=observer,
        progress=None,
    )

    assert [c.attempt_number for c in calls] == [1, 2, 3]
    assert [c.outcome for c in calls] == [
        DownloadOutcome.TRANSPORT_ERROR,
        DownloadOutcome.TRANSPORT_ERROR,
        DownloadOutcome.SUCCESS,
    ]
    # Only the last one moved any bytes.
    assert [c.bytes_transferred for c in calls] == [0, 0, len(body)]


def test_a_failure_is_observed_before_it_is_raised(
    tmp_path, route, client_for, no_retry, collect_calls
):
    """A failed attempt is recorded, so a node that only ever fails still has health"""
    observer, calls = collect_calls
    client = client_for(lambda request: httpx.Response(404))

    with pytest.raises(RouteMissingError):
        fetch_to_path(
            client,
            route,
            tmp_path / "tas.nc",
            retrying=no_retry,
            observer=observer,
        )

    (call,) = calls
    assert call.outcome == DownloadOutcome.MISSING
    assert not call.success
    assert call.response_code == 404
    assert isinstance(call.error, RouteMissingError)


def test_a_dead_host_is_observed_with_no_response_code(
    tmp_path, route, client_for, no_retry, collect_calls
):
    """
    `response_code is None` is how "never spoke" is told apart from "said no"

    The same convention the search side uses.
    """
    observer, calls = collect_calls

    def handler(request):
        raise httpx.ConnectTimeout("nothing there")  # noqa: TRY003 - simulating a library error

    with pytest.raises(DataNodeTimeoutError):
        fetch_to_path(
            client_for(handler),
            route,
            tmp_path / "tas.nc",
            retrying=no_retry,
            observer=observer,
        )

    (call,) = calls
    assert call.response_code is None
    assert call.outcome == DownloadOutcome.TIMEOUT


def test_an_unfetchable_route_is_not_observed_at_all(
    tmp_path, client_for, no_retry, collect_calls
):
    """
    Refusing a route records nothing against any data node

    No request was made and no node is implicated, so an observation would put a
    failure in a node's health that the node had no part in.
    """
    observer, calls = collect_calls

    def handler(request):  # pragma: no cover - must never be reached
        msg = "an unfetchable route must not be requested"
        raise AssertionError(msg)

    with pytest.raises(UnfetchableRouteError):
        fetch_to_path(
            client_for(handler),
            Route(
                url=f"https://{NODE}/a.nc.html",
                data_node=NODE,
                service=AccessService.OPENDAP,
                container_format=ContainerFormat.HTML,
            ),
            tmp_path / "tas.nc",
            retrying=no_retry,
            observer=observer,
        )

    assert calls == []


def test_throughput_is_none_when_there_is_nothing_to_measure(
    tmp_path, route, client_for, no_retry, collect_calls
):
    """
    A failed attempt reports no throughput rather than `0.0`

    Reporting zero would drag a data node's measured speed down for failures that
    moved no bytes, which is a different thing from being slow.
    """
    observer, calls = collect_calls

    with pytest.raises(RouteMissingError):
        fetch_to_path(
            client_for(lambda request: httpx.Response(404)),
            route,
            tmp_path / "tas.nc",
            retrying=no_retry,
            observer=observer,
        )

    (call,) = calls
    assert call.throughput_mb_per_s is None


def test_throughput_is_megabytes_of_a_million_bytes_per_second():
    """
    A megabyte is 1,000,000 bytes, so the number means what a network tool means

    Built directly rather than fetched: the arithmetic is the whole point here, and
    a real transfer's elapsed time is not reproducible enough to assert on.
    """
    call = DataNodeCall(
        data_node=NODE,
        url=URL,
        http_method="GET",
        response_code=200,
        outcome=DownloadOutcome.SUCCESS,
        error=None,
        bytes_transferred=50_000_000,
        seconds=2.0,
        attempt_number=1,
    )

    assert call.throughput_mb_per_s == 25.0


def test_progress_is_reported_as_the_transfer_proceeds(
    tmp_path, route, body, client_for, no_retry
):
    """
    A long download is not silent
    """
    seen = []
    client = client_for(lambda request: httpx.Response(200, content=body))

    fetch_to_path(
        client,
        route,
        tmp_path / "tas.nc",
        expected_size=len(body),
        chunk_bytes=1024,
        retrying=no_retry,
        progress=lambda done, total: seen.append((done, total)),
    )

    assert len(seen) > 1, "a multi-chunk body should report progress more than once"
    # Monotonic, ends at the full size, and knows what it is aiming for.
    assert [done for done, _ in seen] == sorted(done for done, _ in seen)
    assert seen[-1] == (len(body), len(body))


def test_fan_out_tells_every_observer(tmp_path, route, body, client_for, no_retry):
    """`fan_out` composes several observers into the single one a fetch calls"""
    first, second = [], []
    client = client_for(lambda request: httpx.Response(200, content=body))

    fetch_to_path(
        client,
        route,
        tmp_path / "tas.nc",
        retrying=no_retry,
        observer=fan_out(first.append, second.append),
    )

    assert len(first) == len(second) == 1


def test_fan_out_with_no_observers_is_a_valid_no_op(
    tmp_path, route, body, client_for, no_retry
):
    """So a caller can always pass one rather than branching on `None`"""
    client = client_for(lambda request: httpx.Response(200, content=body))

    fetch_to_path(
        client, route, tmp_path / "tas.nc", retrying=no_retry, observer=fan_out()
    )
