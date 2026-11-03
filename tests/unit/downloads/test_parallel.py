"""
Tests of downloading several files at once, and of pacing each data node

Concurrency is asserted rather than hoped for: a `threading.Barrier` fails the test if
transfers do not really overlap, and a handler which counts how many requests are in
flight per host fails it if the cap is exceeded. Nothing here sleeps for longer than it
takes to prove the point, so the whole file runs in well under a second.
"""

from __future__ import annotations

import threading
import time

import httpx
import pytest

from esmporium.datasets import AccessService, ContainerFormat
from esmporium.downloads import (
    DownloadRequest,
    DuplicateDestinationError,
    Route,
    SelectorOfferedNoRouteError,
    build_data_node_limiter,
    build_data_node_list_selector,
    download_files,
    totals,
)

HELD_SECONDS = 0.02
"""
How long a handler holds a request open, so overlapping transfers really overlap

Short enough to be invisible in the suite's runtime, long enough that the workers are
genuinely in flight together rather than finishing one after another by luck.
"""


def route_on(data_node):
    """Build an ordinary fetchable route served by a given data node"""
    return Route(
        url=f"https://{data_node}/tas.nc",
        data_node=data_node,
        service=AccessService.HTTPS,
        container_format=ContainerFormat.NETCDF,
        data_access_id=1,
    )


@pytest.fixture
def requests_over(tmp_path, body):
    """
    Get a factory for several requests, each published by the same data nodes

    Each gets its own destination, because two requests writing one path is refused;
    see [DuplicateDestinationError][esmporium.downloads.DuplicateDestinationError].
    """

    def factory(*data_nodes, n_files=1, prefix="tas"):
        return [
            DownloadRequest(
                routes=tuple(route_on(node) for node in data_nodes),
                dest=tmp_path / f"{prefix}{index}.nc",
                file_id=index,
                expected_size=len(body),
            )
            for index in range(n_files)
        ]

    return factory


class PerHostWatcher:
    """
    Answers every request, recording how many were in flight against each host

    `peak` is what the tests assert on: it is the most requests this host ever had open
    at one time, which is exactly what a per-data-node cap is supposed to bound.
    """

    def __init__(self, body, *, answers=None):
        self.body = body
        self.answers = answers or {}
        self.lock = threading.Lock()
        self.in_flight = {}
        self.peak = {}

    def handler(self, request):
        """Answer one request, counting it in and out again"""
        host = request.url.host
        with self.lock:
            self.in_flight[host] = self.in_flight.get(host, 0) + 1
            self.peak[host] = max(self.peak.get(host, 0), self.in_flight[host])

        try:
            time.sleep(HELD_SECONDS)
            answer = self.answers.get(host)

            return (
                answer if answer is not None else httpx.Response(200, content=self.body)
            )
        finally:
            with self.lock:
                self.in_flight[host] -= 1


def test_several_files_are_really_fetched_at_once(
    requests_over, body, client_for, no_retry
):
    """
    With `max_workers > 1` the transfers are genuinely in flight together
    """
    barrier = threading.Barrier(2, timeout=5)

    def handler(request):
        barrier.wait()
        return httpx.Response(200, content=body)

    results = download_files(
        requests_over("a.example.org", n_files=2),
        client=client_for(handler),
        retrying=no_retry,
        max_workers=2,
    )

    assert all(result.succeeded for result in results)


def test_the_order_is_the_same_run_in_parallel(
    requests_over, body, client_for, no_retry
):
    """`max_workers` changes when the files are fetched, not the order they come back"""
    requests = requests_over("a.example.org", n_files=6)
    answer = client_for(lambda request: httpx.Response(200, content=body))

    sequential = download_files(requests, client=answer, retrying=no_retry)
    for each in requests:
        each.dest.unlink()
    parallel = download_files(requests, client=answer, retrying=no_retry, max_workers=6)

    def shape(results):
        return [result.request.dest.name for result in results]

    assert shape(sequential) == shape(parallel)
    assert shape(parallel) == [f"tas{index}.nc" for index in range(6)]


def test_no_more_than_the_cap_runs_against_one_data_node(
    requests_over, body, client_for, no_retry
):
    """
    The point of the cap: many workers, one node, and it is never over-asked

    `max_workers` is deliberately far above the cap, so a cap which did nothing would
    show up as a peak of eight rather than two.
    """
    watcher = PerHostWatcher(body)

    results = download_files(
        requests_over("a.example.org", n_files=8),
        client=client_for(watcher.handler),
        retrying=no_retry,
        max_workers=8,
        per_data_node=2,
    )

    assert watcher.peak["a.example.org"] == 2
    assert all(result.succeeded for result in results)


def test_the_cap_is_per_data_node_not_a_global_limit(
    requests_over, body, client_for, no_retry
):
    """
    Two nodes at a cap of one still work at the same time as each other

    Without this, a "cap" would just be a smaller thread pool: the whole reason to
    count per node is that a busy node must not hold up a different one.
    """
    watcher = PerHostWatcher(body)
    requests = requests_over("a.example.org", n_files=3, prefix="a")
    requests += requests_over("b.example.org", n_files=3, prefix="b")

    download_files(
        requests,
        client=client_for(watcher.handler),
        retrying=no_retry,
        max_workers=6,
        per_data_node=1,
    )

    assert watcher.peak == {"a.example.org": 1, "b.example.org": 1}


def test_a_widely_replicated_file_falls_through_a_dead_node_under_a_cap(
    requests_over, body, client_for, no_retry
):
    """
    The CMIP5 shape: four replicas, the preferred one gone, and a cap of one

    CMIP5 and CMIP6 files are mirrored across far more data nodes than CMIP7 ones, and
    those nodes are likelier to have gone away. So the case worth pinning is a file
    published in several places whose best node answers 404: the cap has to hold while
    the mirrors are worked through, and the node order the selector asked for has to
    survive being capped.
    """
    nodes = (
        "gone.example.org",
        "second.example.org",
        "third.example.org",
        "fourth.example.org",
    )
    watcher = PerHostWatcher(body, answers={"gone.example.org": httpx.Response(404)})

    results = download_files(
        requests_over(*nodes, n_files=4),
        client=client_for(watcher.handler),
        retrying=no_retry,
        selector=build_data_node_list_selector(nodes),
        max_workers=4,
        per_data_node=1,
    )

    assert all(result.succeeded for result in results)
    # The preferred node was tried first for every file and the next one served them.
    assert all(
        result.fetch.route.data_node == "second.example.org" for result in results
    )
    assert all(peak == 1 for peak in watcher.peak.values())
    # The nodes below the one that worked were never needed.
    assert "third.example.org" not in watcher.peak


def test_no_cap_means_no_cap(requests_over, body, client_for, no_retry):
    """
    `per_data_node=None` really does let every worker at one node

    Here so that the cap tests above cannot pass for the wrong reason: this is the same
    setup with the cap removed, and it reaches the number of workers.
    """
    watcher = PerHostWatcher(body)

    download_files(
        requests_over("a.example.org", n_files=4),
        client=client_for(watcher.handler),
        retrying=no_retry,
        max_workers=4,
        per_data_node=None,
    )

    assert watcher.peak["a.example.org"] == 4


def test_the_cap_is_not_paid_for_when_fetching_one_file_at_a_time(
    requests_over, body, client_for, no_retry
):
    """A sequential run already asks a node for one transfer at a time"""
    watcher = PerHostWatcher(body)

    download_files(
        requests_over("a.example.org", n_files=3),
        client=client_for(watcher.handler),
        retrying=no_retry,
        per_data_node=1,
    )

    assert watcher.peak["a.example.org"] == 1


def test_a_file_which_failed_for_now_is_tried_again_before_the_run_ends(
    requests_over, body, client_for, no_retry
):
    """
    A node that was briefly refusing us costs a retry, not the file

    The first request is refused and every later one is served, so the file can only
    succeed if it was tried a second time within this run.
    """
    answered = {"n": 0}
    lock = threading.Lock()

    def handler(request):
        with lock:
            answered["n"] += 1
            refuse = answered["n"] == 1

        return httpx.Response(503) if refuse else httpx.Response(200, content=body)

    results = download_files(
        requests_over("a.example.org", n_files=2),
        client=client_for(handler),
        retrying=no_retry,
        requeue_passes=1,
    )

    assert all(result.succeeded for result in results)


def test_a_file_nothing_can_serve_is_not_tried_again(
    requests_over, client_for, no_retry
):
    """
    Every route gone means nothing changes by asking again, however many passes

    The cost of getting this wrong is a run that spends all its passes re-proving that
    a retracted file is retracted.
    """
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(404)

    results = download_files(
        requests_over("a.example.org", n_files=1),
        client=client_for(handler),
        retrying=no_retry,
        requeue_passes=3,
    )

    assert attempts["n"] == 1
    assert results[0].give_up


def test_no_extra_passes_means_one_attempt_each(requests_over, client_for, no_retry):
    """`requeue_passes=0` makes a single pass, leaving failures to the next run"""
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(503)

    results = download_files(
        requests_over("a.example.org", n_files=2),
        client=client_for(handler),
        retrying=no_retry,
        requeue_passes=0,
    )

    assert attempts["n"] == 2
    assert not any(result.succeeded for result in results)
    assert not any(result.give_up for result in results)


def test_a_requeued_file_is_reported_once_with_what_became_of_it(
    requests_over, body, client_for, no_retry
):
    """
    One result per request, even for a file which took two passes

    Anything else would double-count: a run of two files that re-queued one of them
    would otherwise add up to three.
    """
    answered = {"n": 0}

    def handler(request):
        answered["n"] += 1

        return (
            httpx.Response(503)
            if answered["n"] == 1
            else httpx.Response(200, content=body)
        )

    results = download_files(
        requests_over("a.example.org", n_files=2),
        client=client_for(handler),
        retrying=no_retry,
        requeue_passes=1,
    )

    assert len(results) == 2
    assert [result.request.dest.name for result in results] == ["tas0.nc", "tas1.nc"]

    run = totals(results)
    assert run.n_files == 2
    assert run.n_fetched == 2
    assert run.n_failed == 0
    assert run.bytes_fetched == 2 * len(body)


def test_each_attempt_is_announced_before_it_starts(
    requests_over, body, client_for, no_retry
):
    """
    `on_start` fires per attempt, including a re-queued one

    This is what lets the database say a file is being worked on right now, and a
    re-queued file really is being worked on again.
    """
    answered = {"n": 0}
    started = []
    finished = []

    def handler(request):
        answered["n"] += 1

        return (
            httpx.Response(503)
            if answered["n"] == 1
            else httpx.Response(200, content=body)
        )

    download_files(
        requests_over("a.example.org", n_files=1),
        client=client_for(handler),
        retrying=no_retry,
        requeue_passes=1,
        on_start=lambda request: started.append(request.dest.name),
        on_result=lambda result: finished.append(result.succeeded),
    )

    assert started == ["tas0.nc", "tas0.nc"]
    assert finished == [False, True]


def test_two_files_writing_the_same_path_is_refused_before_anything_is_fetched(
    tmp_path, client_for, no_retry
):
    """
    One path, two files, so the plan is wrong and downloading either would hide it
    """
    asked = []

    def handler(request):
        asked.append(request.url)
        return httpx.Response(200, content=b"bytes")

    clashing = [
        DownloadRequest(
            routes=(route_on("a.example.org"),), dest=tmp_path / "tas.nc", file_id=1
        ),
        DownloadRequest(
            routes=(route_on("b.example.org"),), dest=tmp_path / "tas.nc", file_id=2
        ),
    ]

    with pytest.raises(DuplicateDestinationError, match="file ids 1 and 2") as excinfo:
        download_files(
            clashing, client=client_for(handler), retrying=no_retry, max_workers=2
        )

    # Carried as well as said, so a caller can see which two files clashed without
    # reading the prose -- and so that the path named is the one they share.
    assert excinfo.value.dest == tmp_path / "tas.nc"
    assert (excinfo.value.first, excinfo.value.second) == (clashing[0], clashing[1])

    assert asked == []


def test_one_file_blowing_up_does_not_lose_the_others_already_recorded(
    tmp_path, body, client_for, no_retry
):
    """
    A file with nothing to try stops the run, but what finished stays finished

    The results handed to `on_result` have already been acted on by the time the error
    surfaces, which for the database layer means their rows are already committed.
    """
    recorded = []
    lock = threading.Lock()

    def record(result):
        with lock:
            recorded.append(result.request.dest.name)

    good = DownloadRequest(
        routes=(route_on("a.example.org"),), dest=tmp_path / "good.nc", file_id=1
    )
    # Published only over OPeNDAP, so there is no route to offer and no fetch to make.
    hopeless = DownloadRequest(
        routes=(
            Route(
                url="https://a.example.org/bad.nc.html",
                data_node="a.example.org",
                service=AccessService.OPENDAP,
                container_format=ContainerFormat.HTML,
            ),
        ),
        dest=tmp_path / "bad.nc",
        file_id=2,
    )

    with pytest.raises(SelectorOfferedNoRouteError, match="No route was offered"):
        download_files(
            [good, hopeless],
            client=client_for(lambda request: httpx.Response(200, content=body)),
            retrying=no_retry,
            max_workers=2,
            on_result=record,
        )

    assert recorded == ["good.nc"]
    assert (tmp_path / "good.nc").read_bytes() == body


def test_a_limit_of_less_than_one_is_refused():
    """
    A cap of zero would stop every download rather than pace it

    Loud, because the caller who wrote it meant something, and silently downloading
    nothing is the worst reading of it.
    """
    with pytest.raises(ValueError, match="at least 1"):
        build_data_node_limiter(0)


def test_a_negative_number_of_passes_is_refused(requests_over, client_for, no_retry):
    """Nonsense in, error out: there is no sensible reading of minus one pass"""
    with pytest.raises(ValueError, match="zero or more"):
        download_files(
            requests_over("a.example.org", n_files=1),
            client=client_for(lambda request: httpx.Response(200, content=b"bytes")),
            retrying=no_retry,
            requeue_passes=-1,
        )
