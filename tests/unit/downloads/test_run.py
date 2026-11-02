"""
Tests of the mirror-fallback loop and the many-file driver

The loop is the same shape as `search_single_project`'s, and these pin the parts that
matter: that a dead mirror falls through to the next, that every failure is kept, and
that "try again tomorrow" is distinguished from "this will never work".
"""

import httpx
import pytest

from esmporium.datasets import AccessService, ContainerFormat, DownloadOutcome
from esmporium.downloads import (
    DownloadRequest,
    Route,
    SelectorOfferedNoRouteError,
    build_list_selector,
    download_file,
    download_files,
    totals,
)


def route_on(data_node, *, scheme="https"):
    """Build an ordinary fetchable route served by a given data node"""
    return Route(
        url=f"{scheme}://{data_node}/tas.nc",
        data_node=data_node,
        service=AccessService.HTTPS,
        container_format=ContainerFormat.NETCDF,
        data_access_id=1,
    )


@pytest.fixture
def request_for(tmp_path):
    """Get a factory for a download request over some data nodes"""

    def factory(*data_nodes, name="tas.nc", **kwargs):
        return DownloadRequest(
            routes=tuple(route_on(node) for node in data_nodes),
            dest=tmp_path / name,
            file_id=7,
            **kwargs,
        )

    return factory


def by_host(answers, default=None):
    """
    Build a handler which answers differently per host

    Keyed by host so a test reads as "this node is down, that one works".
    """

    def handler(request):
        answer = answers.get(request.url.host, default)
        if answer is None:
            return httpx.Response(200, content=b"bytes")
        if isinstance(answer, Exception):
            raise answer
        return answer

    return handler


def test_the_first_working_mirror_wins(request_for, body, client_for, no_retry):
    """A dead node falls through to the next, and the download succeeds"""
    client = client_for(
        by_host(
            {
                "dead.example.org": httpx.Response(404),
                "alive.example.org": httpx.Response(200, content=body),
            }
        )
    )

    result = download_file(
        client,
        request_for("dead.example.org", "alive.example.org"),
        retrying=no_retry,
    )

    assert result.succeeded
    assert result.fetch.route.data_node == "alive.example.org"
    assert result.request.dest.read_bytes() == body
    # The failure is kept even though the download worked: "we got it, but only from
    # the second mirror" is worth knowing.
    assert [f.outcome for f in result.failures] == [DownloadOutcome.MISSING]


def test_every_failure_is_kept_when_no_mirror_works(request_for, client_for, no_retry):
    """
    A file nothing serves comes back as a result, not an exception

    One file failing is not a reason to abandon a run of ten thousand.
    """
    client = client_for(
        by_host(
            {
                "a.example.org": httpx.Response(404),
                "b.example.org": httpx.Response(503),
                "c.example.org": httpx.ConnectError("gone"),
            }
        )
    )

    result = download_file(
        client,
        request_for("a.example.org", "b.example.org", "c.example.org"),
        retrying=no_retry,
    )

    assert not result.succeeded
    assert result.fetch is None
    assert [f.outcome for f in result.failures] == [
        DownloadOutcome.MISSING,
        DownloadOutcome.BLOCKED,
        DownloadOutcome.HOST_FAULT,
    ]
    assert [f.route.data_node for f in result.failures] == [
        "a.example.org",
        "b.example.org",
        "c.example.org",
    ]


def test_all_mirrors_gone_means_give_up(request_for, client_for, no_retry):
    """
    Every route a 404: retrying tomorrow changes nothing

    This is what separates `GIVEN_UP` from `FAILED`, and it is why outcomes are
    classified rather than collapsed into a boolean.
    """
    client = client_for(by_host({}, default=httpx.Response(404)))

    result = download_file(
        client, request_for("a.example.org", "b.example.org"), retrying=no_retry
    )

    assert not result.succeeded
    assert result.give_up


def test_needing_credentials_everywhere_also_means_give_up(
    request_for, client_for, no_retry
):
    """Nothing changes by retrying until a client with credentials is supplied"""
    client = client_for(by_host({}, default=httpx.Response(401)))

    result = download_file(client, request_for("a.example.org"), retrying=no_retry)

    assert result.give_up


def test_one_transient_failure_among_permanent_ones_means_try_again(
    request_for, client_for, no_retry
):
    """
    A single timeout makes the whole file worth another go

    Giving up here would abandon a file over one node having a bad afternoon.
    """
    client = client_for(
        by_host(
            {
                "gone.example.org": httpx.Response(404),
                "slow.example.org": httpx.ReadTimeout("stalled"),
            }
        )
    )

    result = download_file(
        client,
        request_for("gone.example.org", "slow.example.org"),
        retrying=no_retry,
    )

    assert not result.succeeded
    assert not result.give_up


def test_max_routes_stops_a_file_exhausting_thirty_dead_mirrors(
    request_for, client_for, no_retry
):
    """A cap on mirrors, so one hopeless file cannot eat a whole run's time"""
    client = client_for(by_host({}, default=httpx.Response(404)))
    nodes = [f"n{index}.example.org" for index in range(10)]

    result = download_file(client, request_for(*nodes), retrying=no_retry, max_routes=3)

    assert len(result.failures) == 3


def test_a_selector_with_nothing_to_offer_is_loud(request_for, client_for, no_retry):
    """
    Asking for a download is asking for it to happen

    Returning an empty answer would read as "we tried and nobody had it", when nobody
    was tried at all.
    """
    client = client_for(by_host({}))

    with pytest.raises(SelectorOfferedNoRouteError):
        download_file(
            client,
            request_for("a.example.org"),
            selector=build_list_selector([]),
            retrying=no_retry,
        )


def test_an_unfetchable_only_file_is_loud_rather_than_silently_failing(
    tmp_path, client_for, no_retry
):
    """
    A file whose only routes are OPeNDAP has nothing to try, so it raises

    The routes get filtered before the selector sees them, which would otherwise look
    exactly like "every mirror failed".
    """
    client = client_for(by_host({}))
    request = DownloadRequest(
        routes=(
            Route(
                url="https://a.example.org/tas.nc.html",
                data_node="a.example.org",
                service=AccessService.OPENDAP,
                container_format=ContainerFormat.HTML,
            ),
        ),
        dest=tmp_path / "tas.nc",
    )

    with pytest.raises(SelectorOfferedNoRouteError):
        download_file(client, request, retrying=no_retry)


def test_many_files_are_downloaded_in_order(tmp_path, body, client_for, no_retry):
    """The driver returns one result per request, in the order given"""
    client = client_for(by_host({}, default=httpx.Response(200, content=body)))
    requests = [
        DownloadRequest(
            routes=(route_on("a.example.org"),), dest=tmp_path / f"{index}.nc"
        )
        for index in range(3)
    ]

    results = download_files(requests, client=client, retrying=no_retry)

    assert [r.request.dest.name for r in results] == ["0.nc", "1.nc", "2.nc"]
    assert all(r.succeeded for r in results)


def test_on_result_is_called_as_each_file_finishes(
    tmp_path, body, client_for, no_retry
):
    """
    Not at the end: this is what makes a run killed on day two keep day one

    The database layer uses this seam to write one row per file as the run proceeds.
    """
    client = client_for(by_host({}, default=httpx.Response(200, content=body)))
    requests = [
        DownloadRequest(
            routes=(route_on("a.example.org"),), dest=tmp_path / f"{index}.nc"
        )
        for index in range(3)
    ]
    seen = []

    download_files(
        requests,
        client=client,
        retrying=no_retry,
        on_result=lambda result: seen.append(result.request.dest.name),
    )

    assert seen == ["0.nc", "1.nc", "2.nc"]


def test_one_file_failing_does_not_stop_the_run(tmp_path, body, client_for, no_retry):
    """A run of many keeps going past a file it cannot get"""
    client = client_for(
        by_host(
            {
                "bad.example.org": httpx.Response(404),
                "good.example.org": httpx.Response(200, content=body),
            }
        )
    )
    requests = [
        DownloadRequest(routes=(route_on("good.example.org"),), dest=tmp_path / "a.nc"),
        DownloadRequest(routes=(route_on("bad.example.org"),), dest=tmp_path / "b.nc"),
        DownloadRequest(routes=(route_on("good.example.org"),), dest=tmp_path / "c.nc"),
    ]

    results = download_files(requests, client=client, retrying=no_retry)

    assert [r.succeeded for r in results] == [True, False, True]

    run = totals(results)
    assert run.n_files == 3
    assert run.n_fetched == 2
    assert run.n_failed == 1
    assert run.n_given_up == 1
    assert run.bytes_fetched == 2 * len(body)
    assert "2 fetched" in str(run)


def test_the_driver_builds_and_closes_its_own_client_if_not_given_one(tmp_path):
    """The repo's borrow-or-create idiom, as search uses it"""
    # No client passed, and no routes offered, so nothing is actually fetched -- the
    # point is that building and closing the client does not raise.
    with pytest.raises(SelectorOfferedNoRouteError):
        download_files(
            [DownloadRequest(routes=(), dest=tmp_path / "a.nc")],
        )
