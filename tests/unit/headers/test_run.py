"""
Tests of reading many headers, across routes, nodes and threads
"""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
from tests.conftest import CMIP5_FILE_SIZE

from esmporium.datasets import AccessService, ContainerFormat, HeaderReadOutcome
from esmporium.downloads import (
    Route,
    SelectorOfferedNoRouteError,
    build_list_selector,
)
from esmporium.headers import (
    HeaderRequest,
    read_headers,
    read_one_header,
)


def route_on(node: str, name: str = "tas.nc") -> Route:
    """
    Build an HTTPS route to netCDF bytes on a named data node

    Parameters
    ----------
    node
        The data node's name

    name
        The file's name

    Returns
    -------
    :
        The route
    """
    return Route(
        url=f"https://{node}/thredds/fileServer/{name}",
        data_node=node,
        service=AccessService.HTTPS,
        container_format=ContainerFormat.NETCDF,
    )


def served(prefix: bytes) -> httpx.Response:
    """
    Build the `206` a well-behaved node answers with

    Parameters
    ----------
    prefix
        The bytes to serve

    Returns
    -------
    :
        The response
    """
    return httpx.Response(
        206,
        content=prefix,
        headers={"Content-Range": f"bytes 0-{len(prefix) - 1}/{CMIP5_FILE_SIZE}"},
    )


def test_the_first_route_that_serves_it_wins(
    client_for, no_retry, cmip5_netcdf3_prefix
):
    request = HeaderRequest(
        routes=(route_on("dead.example.org"), route_on("alive.example.org")),
        filename="tas.nc",
    )

    def handler(http_request: httpx.Request) -> httpx.Response:
        if "dead" in str(http_request.url):
            return httpx.Response(404)

        return served(cmip5_netcdf3_prefix)

    with client_for(handler) as client:
        result = read_one_header(client, request, retrying=no_retry)

    assert result.succeeded
    assert result.header is not None
    assert result.header.attributes["parent_experiment_id"] == "piControl"
    # The failure on the way is kept: "it took two mirrors" is worth knowing.
    assert [failure.outcome for failure in result.failures] == [
        HeaderReadOutcome.MISSING
    ]


def test_a_file_no_route_serves_comes_back_rather_than_raising(client_for, no_retry):
    """
    One file failing is not a reason to abandon a run

    The prototype drove its parent searches through `list(executor.map(...))`, which
    re-raises the first worker's exception -- so a single `504` from one node killed a
    whole hop and lost every other result in it.
    """
    request = HeaderRequest(routes=(route_on("a.example.org"),), filename="tas.nc")

    with client_for(lambda http_request: httpx.Response(404)) as client:
        result = read_one_header(client, request, retrying=no_retry)

    assert not result.succeeded
    assert result.header is None
    assert result.give_up
    assert result.last_error is not None


def test_a_selector_with_nothing_to_offer_is_loud(client_for, no_retry):
    """
    "Nobody was asked" is a different answer from "we tried and nobody had it"

    Only one of them is a finished job, so the quiet version would read as the wrong
    one.
    """
    request = HeaderRequest(routes=(), filename="tas.nc")

    with client_for(lambda http_request: httpx.Response(404)) as client:
        with pytest.raises(SelectorOfferedNoRouteError):
            read_one_header(client, request, retrying=no_retry)


class TestGivingUpOnADataNodeNotJustARoute:
    """
    The death-spiral fix, at the route level

    A node that is unreachable or hanging fails its other URLs identically, so walking
    them spends the time that should have gone on the next mirror. The prototype did
    walk them -- 660 to 1162 URLs against one dead node, three attempts each -- which
    is about 36 minutes for one node of one simulation.
    """

    @staticmethod
    def _request() -> HeaderRequest:
        # Two URLs on the dead node, as ESGF really publishes (an https route and its
        # http twin), then a working one elsewhere.
        return HeaderRequest(
            routes=(
                route_on("dead.example.org", "tas_part1.nc"),
                route_on("dead.example.org", "tas_part2.nc"),
                route_on("alive.example.org"),
            ),
            filename="tas.nc",
        )

    def test_an_unreachable_node_costs_one_attempt_not_one_per_url(
        self, client_for, no_retry, cmip5_netcdf3_prefix
    ):
        tried: list[str] = []

        def handler(http_request: httpx.Request) -> httpx.Response:
            tried.append(str(http_request.url))
            if "dead" in str(http_request.url):
                raise httpx.ConnectError(  # noqa: TRY003 - simulating a library error
                    "no route", request=http_request
                ) from socket.gaierror(8)

            return served(cmip5_netcdf3_prefix)

        with client_for(handler) as client:
            result = read_one_header(
                client,
                self._request(),
                selector=build_list_selector(self._request().routes),
                retrying=no_retry,
            )

        assert result.succeeded
        # The dead node was asked once, not once per URL it holds.
        assert sum("dead" in url for url in tried) == 1

    def test_a_per_file_failure_does_not_abandon_the_node(
        self, client_for, no_retry, cmip5_netcdf3_prefix
    ):
        """
        A 404 is an answer about one file, so the node's other URLs are still worth it

        Getting this wrong is how the prototype evicted a node that was 93% healthy.
        """
        tried: list[str] = []

        def handler(http_request: httpx.Request) -> httpx.Response:
            tried.append(str(http_request.url))
            if "part1" in str(http_request.url):
                return httpx.Response(404)

            return served(cmip5_netcdf3_prefix)

        request = self._request()
        with client_for(handler) as client:
            result = read_one_header(
                client,
                request,
                selector=build_list_selector(request.routes),
                retrying=no_retry,
            )

        assert result.succeeded
        # The second URL on the same node was still tried, and served it.
        assert sum("dead" in url for url in tried) == 2


def test_results_come_back_in_the_order_they_were_asked_for(
    client_for, no_retry, cmip5_netcdf3_prefix
):
    requests = tuple(
        HeaderRequest(routes=(route_on(f"node{index}.example.org"),), file_id=index)
        for index in range(6)
    )

    def handler(http_request: httpx.Request) -> httpx.Response:
        return served(cmip5_netcdf3_prefix)

    with client_for(handler) as client:
        results = read_headers(
            requests, client=client, retrying=no_retry, max_workers=4
        )

    assert [result.request.file_id for result in results] == list(range(6))
    assert all(result.succeeded for result in results)


def test_one_file_failing_does_not_stop_the_others(
    client_for, no_retry, cmip5_netcdf3_prefix
):
    requests = tuple(
        HeaderRequest(routes=(route_on(f"node{index}.example.org"),), file_id=index)
        for index in range(5)
    )

    def handler(http_request: httpx.Request) -> httpx.Response:
        if "node2" in str(http_request.url):
            return httpx.Response(500)

        return served(cmip5_netcdf3_prefix)

    with client_for(handler) as client:
        results = read_headers(
            requests, client=client, retrying=no_retry, max_workers=3
        )

    assert [result.succeeded for result in results] == [
        True,
        True,
        False,
        True,
        True,
    ]


def test_each_result_is_handed_over_as_it_finishes(
    client_for, no_retry, cmip5_netcdf3_prefix
):
    """
    What makes a long run survive being killed

    The prototype persisted at the end and lost an hour and three quarters of header
    reads to a session that was interrupted first.
    """
    seen: list[int | None] = []
    guard = threading.Lock()

    def on_result(result) -> None:
        with guard:
            seen.append(result.request.file_id)

    requests = tuple(
        HeaderRequest(routes=(route_on(f"node{index}.example.org"),), file_id=index)
        for index in range(4)
    )

    with client_for(lambda http_request: served(cmip5_netcdf3_prefix)) as client:
        read_headers(
            requests,
            client=client,
            retrying=no_retry,
            max_workers=2,
            on_result=on_result,
        )

    assert sorted(seen) == [0, 1, 2, 3]


def test_no_more_than_the_cap_runs_against_one_node_at_once(
    client_for, no_retry, cmip5_netcdf3_prefix
):
    """
    The per-node cap, which is what keeps us a polite guest on a shared archive

    Written so it cannot pass by accident: it waits until the cap is actually
    saturated before asserting, so a run where nothing started would time out rather
    than quietly report "never exceeded the cap".
    """
    in_flight = 0
    high_water = 0
    guard = threading.Lock()
    saturated = threading.Event()
    release = threading.Event()

    def handler(http_request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, high_water
        with guard:
            in_flight += 1
            high_water = max(high_water, in_flight)
            if in_flight >= 2:
                saturated.set()

        # Hold here so that, if the cap were not working, a third worker would arrive
        # and be counted.
        release.wait(timeout=5.0)

        with guard:
            in_flight -= 1

        return served(cmip5_netcdf3_prefix)

    requests = tuple(
        HeaderRequest(routes=(route_on("busy.example.org", f"tas{index}.nc"),))
        for index in range(6)
    )

    results: list[tuple] = []
    with client_for(handler) as client:
        worker = threading.Thread(
            target=lambda: results.append(
                read_headers(
                    requests,
                    client=client,
                    retrying=no_retry,
                    max_workers=6,
                    per_data_node=2,
                )
            )
        )
        worker.start()
        try:
            assert saturated.wait(timeout=5.0), "no two reads ever ran at once"
            # Give any worker the cap should be holding back time to slip through.
            time.sleep(0.2)

            assert high_water == 2
        finally:
            release.set()
            worker.join(timeout=10.0)

    assert len(results[0]) == 6


def test_no_requests_is_an_empty_answer():
    assert read_headers(()) == ()
