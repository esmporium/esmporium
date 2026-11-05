"""
Tests of recording and ranking data node health for header reads

Three of these pin where header health deliberately differs from download health:
ranking on **latency** rather than throughput, writing a node off on a **rate over a
fair sample** rather than a streak, and counting the nodes that ignore a range request.
"""

from __future__ import annotations

import datetime

import pytest
from sqlmodel import Session, select

from esmporium.datasets import AccessService, ContainerFormat, HeaderReadOutcome
from esmporium.db import (
    HeaderReadCallRecord,
    HeaderReadHealth,
    aggregate_header_read_health,
    build_header_read_health_selector,
    rank_by_latency,
    rank_by_reliability_then_latency,
    record_header_read_calls,
    unreliable_data_nodes,
)
from esmporium.downloads import Route
from esmporium.headers import HeaderReadCall

QUICK = "quick.example.org"
SLOW = "slow.example.org"
DEAD = "dead.example.org"
UNKNOWN = "unknown.example.org"


def make_call(  # noqa: PLR0913 - a factory mirroring every field we vary
    data_node=QUICK,
    outcome=HeaderReadOutcome.SUCCESS,
    *,
    seconds=0.5,
    bytes_requested=65536,
    bytes_transferred=6364,
    response_code=206,
    attempt_number=1,
    range_honoured=True,
):
    """Build one observation, with the parts a test does not care about filled in"""
    return HeaderReadCall(
        data_node=data_node,
        url=f"https://{data_node}/tas.nc",
        http_method="GET",
        response_code=response_code,
        outcome=outcome,
        error=None if outcome == HeaderReadOutcome.SUCCESS else RuntimeError("nope"),
        bytes_requested=bytes_requested,
        bytes_transferred=bytes_transferred,
        seconds=seconds,
        attempt_number=attempt_number,
        range_honoured=range_honoured,
    )


def route_on(data_node, *, data_access_id=None):
    """Build an ordinary fetchable route served by a given data node"""
    return Route(
        url=f"https://{data_node}/tas.nc",
        data_node=data_node,
        service=AccessService.HTTPS,
        container_format=ContainerFormat.NETCDF,
        data_access_id=data_access_id,
    )


def health_of(  # noqa: PLR0913 - a factory mirroring every field we vary
    data_node,
    *,
    failure_rate=0.0,
    median_seconds=1.0,
    n_calls=10,
    success_rate=1.0,
    n_range_ignored=0,
):
    """Build a `HeaderReadHealth` directly, for the pure ranking tests"""
    return HeaderReadHealth(
        data_node=data_node,
        n_calls=n_calls,
        n_success=int(n_calls * success_rate),
        n_judged=n_calls,
        failure_rate=failure_rate,
        success_rate=success_rate,
        median_seconds=median_seconds,
        n_blocked=0,
        n_timeout=0,
        n_host_fault=0,
        n_unreadable=0,
        n_missing=0,
        n_range_ignored=n_range_ignored,
    )


def test_each_attempt_is_recorded_as_its_own_row(engine):
    observer = record_header_read_calls(engine)

    observer(make_call(attempt_number=1, outcome=HeaderReadOutcome.BLOCKED))
    observer(make_call(attempt_number=2))

    with Session(engine) as session:
        rows = session.exec(
            select(HeaderReadCallRecord).order_by(HeaderReadCallRecord.attempt_number)
        ).all()

    assert [row.attempt_number for row in rows] == [1, 2]
    assert [row.outcome for row in rows] == [
        HeaderReadOutcome.BLOCKED,
        HeaderReadOutcome.SUCCESS,
    ]
    assert rows[0].error is not None
    assert rows[1].error is None


def test_a_node_we_have_never_seen_gets_a_row(engine):
    """
    The same `DataNode` row the download recorder would make, not a second one

    Which is the one thing the two kinds of health must share: "does this node answer"
    is the same question either way, so they have to point at the same node.
    """
    record_header_read_calls(engine)(make_call(data_node="brand-new.example.org"))

    health = aggregate_header_read_health(engine)

    assert set(health) == {"brand-new.example.org"}


def test_latency_is_what_gets_summarised(engine):
    """
    The median time a successful read took, not its throughput

    A 64 KiB read is almost entirely connection setup plus one round trip, so
    throughput here measures a handshake. This is the number that says whether a node
    is a good one to ask.
    """
    observer = record_header_read_calls(engine)
    for seconds in (0.2, 0.4, 0.6):
        observer(make_call(seconds=seconds))

    health = aggregate_header_read_health(engine)[QUICK]

    assert health.median_seconds == 0.4
    assert health.n_success == 3
    assert health.failure_rate == 0.0


def test_a_file_level_answer_is_not_held_against_the_node(engine):
    """
    A 404, a truncated header and an unreadable format are all facts about the file

    Every mirror of a file answers the same way, so charging any of them to the node
    would retire it for behaving correctly.
    """
    observer = record_header_read_calls(engine)
    observer(make_call(outcome=HeaderReadOutcome.SUCCESS))
    observer(make_call(outcome=HeaderReadOutcome.MISSING, response_code=404))
    observer(make_call(outcome=HeaderReadOutcome.TRUNCATED_HEADER))
    observer(make_call(outcome=HeaderReadOutcome.UNSUPPORTED_FORMAT))

    health = aggregate_header_read_health(engine)[QUICK]

    assert health.n_calls == 4
    # Only the success is evidence about this node; the other three are facts about
    # the file.
    assert health.n_judged == 1
    assert health.failure_rate == 0.0
    assert health.n_missing == 1


def test_bytes_that_do_not_parse_do_count_against_the_node(engine):
    """
    The one parse failure that is the node's fault

    Corrupt bytes are bad data rather than an unreadable format, and another mirror of
    the same file may be fine -- the argument a failed checksum makes for downloads.
    """
    observer = record_header_read_calls(engine)
    observer(make_call(outcome=HeaderReadOutcome.SUCCESS))
    observer(make_call(outcome=HeaderReadOutcome.UNREADABLE_BYTES))

    health = aggregate_header_read_health(engine)[QUICK]

    assert health.n_judged == 2
    assert health.failure_rate == 0.5
    assert health.n_unreadable == 1


def test_a_failure_rate_is_a_rate_not_a_flag(engine):
    """
    A node that mostly works must not report a failure rate of 1.0

    The same mistake `esmporium.db.download_health` had: a success belongs in the
    denominator, and `counts_against_data_node` excludes it because it answers the
    narrower question of whose fault a failure is.
    """
    observer = record_header_read_calls(engine)
    for _ in range(9):
        observer(make_call(outcome=HeaderReadOutcome.SUCCESS))
    observer(make_call(outcome=HeaderReadOutcome.TIMEOUT))

    health = aggregate_header_read_health(engine)[QUICK]

    assert health.n_judged == 10
    assert health.failure_rate == pytest.approx(0.1)
    assert health.success_rate == pytest.approx(0.9)


def test_a_node_that_ignores_the_range_is_counted(engine):
    """
    The one number here with no counterpart in download health

    Such a node still gives a usable header, so it is not a failure -- but it made us
    open a connection it began pouring a file down.
    """
    observer = record_header_read_calls(engine)
    observer(make_call(range_honoured=True))
    observer(make_call(range_honoured=False, response_code=200))

    health = aggregate_header_read_health(engine)[QUICK]

    assert health.n_success == 2
    assert health.n_range_ignored == 1


def test_only_attempts_inside_the_window_are_counted(engine):
    observer = record_header_read_calls(engine)
    observer(make_call())

    with Session(engine) as session:
        row = session.exec(select(HeaderReadCallRecord)).one()
        row.created_at = datetime.datetime.now(
            datetime.timezone.utc
        ) - datetime.timedelta(days=30)
        session.add(row)
        session.commit()

    assert aggregate_header_read_health(engine) == {}
    assert aggregate_header_read_health(engine, since=None) != {}


def test_an_empty_pool_returns_nothing_not_everything(engine):
    record_header_read_calls(engine)(make_call())

    assert aggregate_header_read_health(engine, []) == {}
    assert set(aggregate_header_read_health(engine, [QUICK])) == {QUICK}


def test_a_node_with_no_attempts_is_absent_rather_than_empty(engine):
    """
    "We have nothing to go on" has to stay distinguishable from "we tried and it failed"
    """
    record_header_read_calls(engine)(make_call())

    assert UNKNOWN not in aggregate_header_read_health(engine, [QUICK, UNKNOWN])


class TestRanking:
    def test_reliability_comes_before_speed(self):
        """
        A node answering quickly one time in five is worse than a slow reliable one

        The cost of a failure is not the failed request; it is the wait before another
        mirror is tried.
        """
        flaky_but_quick = health_of(QUICK, failure_rate=0.8, median_seconds=0.1)
        slow_but_sure = health_of(SLOW, failure_rate=0.0, median_seconds=3.0)

        assert rank_by_reliability_then_latency(slow_but_sure) < (
            rank_by_reliability_then_latency(flaky_but_quick)
        )

    def test_speed_breaks_a_tie(self):
        quick = health_of(QUICK, failure_rate=0.0, median_seconds=0.3)
        slow = health_of(SLOW, failure_rate=0.0, median_seconds=2.0)

        assert rank_by_reliability_then_latency(quick) < (
            rank_by_reliability_then_latency(slow)
        )

    def test_a_node_that_has_never_answered_sorts_last(self):
        never = health_of(DEAD, failure_rate=0.0, median_seconds=None)
        answered = health_of(SLOW, failure_rate=0.0, median_seconds=9.0)

        assert rank_by_latency(answered) < rank_by_latency(never)
        assert rank_by_latency(never) == float("inf")


class TestWritingANodeOff:
    """
    Judged on a rate over a fair sample, never on a streak

    The prototype used three consecutive failures and so dropped a node succeeding 228
    times out of 244 -- 93% healthy -- which stranded 214 simulations. Reads are
    grouped by model, so one model with a gap on one mirror makes its failures
    consecutive there.
    """

    def test_a_node_that_never_works_is_written_off(self):
        dead = health_of(DEAD, n_calls=12, success_rate=0.0, median_seconds=None)

        assert unreliable_data_nodes({DEAD: dead}) == frozenset({DEAD})

    def test_a_busy_but_healthy_node_is_not(self):
        """
        The exact regression that made the prototype rate-based
        """
        busy = health_of(QUICK, n_calls=244, success_rate=228 / 244)

        assert unreliable_data_nodes({QUICK: busy}) == frozenset()

    def test_a_node_with_too_small_a_sample_is_never_written_off(self):
        """
        With no sample there is no evidence

        A node excluded on two bad reads is a node never given the chance to prove
        otherwise.
        """
        unlucky = health_of(DEAD, n_calls=2, success_rate=0.0, median_seconds=None)

        assert unreliable_data_nodes({DEAD: unlucky}) == frozenset()
        assert unreliable_data_nodes({DEAD: unlucky}, min_calls=2) == frozenset({DEAD})


class TestTheSelector:
    def test_routes_are_ordered_by_their_nodes_health(self, engine):
        observer = record_header_read_calls(engine)
        for _ in range(3):
            observer(make_call(data_node=QUICK, seconds=0.2))
            observer(make_call(data_node=SLOW, seconds=3.0))

        selector = build_header_read_health_selector(engine)
        routes = (route_on(SLOW), route_on(QUICK))

        assert selector(routes, 0).data_node == QUICK
        assert selector(routes, 1).data_node == SLOW
        assert selector(routes, 2) is None

    def test_a_node_we_know_nothing_about_follows_the_ones_we_do(self, engine):
        record_header_read_calls(engine)(make_call(data_node=QUICK))

        selector = build_header_read_health_selector(engine)
        routes = (route_on(UNKNOWN), route_on(QUICK))

        assert selector(routes, 0).data_node == QUICK
        assert selector(routes, 1).data_node == UNKNOWN

    def test_with_no_health_at_all_it_falls_back(self, engine):
        """
        Rather than inventing a ranking from nothing
        """
        selector = build_header_read_health_selector(engine)
        routes = (route_on(UNKNOWN), route_on(QUICK))

        assert selector(routes, 0).data_node == UNKNOWN

    def test_an_unreliable_node_is_put_last_not_removed(self, engine):
        """
        A node that fails four reads in five still holds files nothing healthier does

        Dropping it outright would turn "slow to get" into "impossible to get". The
        ordering already means it is only reached when nothing better has the file.
        """
        observer = record_header_read_calls(engine)
        for _ in range(8):
            observer(
                make_call(
                    data_node=DEAD, outcome=HeaderReadOutcome.TIMEOUT, seconds=30.0
                )
            )
        observer(make_call(data_node=SLOW, seconds=5.0))

        selector = build_header_read_health_selector(engine)
        routes = (route_on(DEAD), route_on(SLOW))

        assert selector(routes, 0).data_node == SLOW
        # Still offered, just last.
        assert selector(routes, 1).data_node == DEAD

    def test_skipping_unreliable_nodes_can_be_turned_off(self, engine):
        observer = record_header_read_calls(engine)
        for _ in range(8):
            observer(
                make_call(
                    data_node=DEAD, outcome=HeaderReadOutcome.TIMEOUT, seconds=0.1
                )
            )
        observer(make_call(data_node=SLOW, seconds=5.0))

        selector = build_header_read_health_selector(engine, skip_unreliable=False)
        routes = (route_on(DEAD), route_on(SLOW))

        # Without the skip, the dead node's low latency is all that is compared after
        # its failure rate -- which is why the skip exists.
        assert {selector(routes, index).data_node for index in (0, 1)} == {DEAD, SLOW}
