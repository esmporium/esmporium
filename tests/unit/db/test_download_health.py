"""
Tests of recording and ranking ESGF data node health

Three of these pin the places where download health deliberately differs from search
health: the `since` window, reliability-before-speed ranking, and a selector that
re-reads itself as a long download goes on.
"""

from __future__ import annotations

import datetime

import pytest
from sqlmodel import Session, select

from esmporium.datasets import AccessService, ContainerFormat, DownloadOutcome
from esmporium.db import (
    DataNodeCallRecord,
    DataNodeHealth,
    aggregate_data_node_health,
    build_data_node_health_selector,
    build_refreshing_data_node_health_selector,
    rank_by_reliability_then_throughput,
    rank_by_throughput,
    record_data_node_calls,
)
from esmporium.downloads import DataNodeCall, Route

FAST = "fast.example.org"
FLAKY = "flaky.example.org"
UNKNOWN = "unknown.example.org"


def make_call(  # noqa: PLR0913 - a factory mirroring every field we vary
    data_node=FAST,
    outcome=DownloadOutcome.SUCCESS,
    *,
    bytes_transferred=10_000_000,
    seconds=1.0,
    response_code=200,
    attempt_number=1,
    url=None,
):
    """Build one observation, with the parts a test does not care about filled in"""
    return DataNodeCall(
        data_node=data_node,
        url=url if url is not None else f"https://{data_node}/tas.nc",
        http_method="GET",
        response_code=response_code,
        outcome=outcome,
        error=None if outcome == DownloadOutcome.SUCCESS else RuntimeError("nope"),
        bytes_transferred=bytes_transferred,
        seconds=seconds,
        attempt_number=attempt_number,
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


def health_of(data_node, *, failure_rate, throughput):
    """Build a `DataNodeHealth` directly, for the pure ranking tests"""
    return DataNodeHealth(
        data_node=data_node,
        n_calls=10,
        n_success=10,
        n_judged=10,
        failure_rate=failure_rate,
        median_throughput_mb_per_s=throughput,
        total_bytes=10_000_000,
        n_blocked=0,
        n_timeout=0,
        n_host_fault=0,
        n_checksum_failed=0,
        n_missing=0,
        n_needs_credentials=0,
    )


def test_an_observation_is_recorded_as_a_row(engine):
    """The observer writes one row per attempt, resolving the data node to its id"""
    observer = record_data_node_calls(engine)

    observer(make_call())

    with Session(engine) as session:
        (row,) = session.exec(select(DataNodeCallRecord)).all()

    assert row.outcome == DownloadOutcome.SUCCESS
    assert row.bytes_transferred == 10_000_000
    assert row.seconds == 1.0
    assert row.response_code == 200
    assert row.attempt_number == 1
    assert row.error is None


def test_a_failure_records_its_error_type_and_message(engine):
    """Rendered as `TypeName: message`, the way the search twin does it"""
    observer = record_data_node_calls(engine)

    observer(make_call(outcome=DownloadOutcome.TIMEOUT, response_code=None))

    with Session(engine) as session:
        (row,) = session.exec(select(DataNodeCallRecord)).all()

    assert row.error == "RuntimeError: nope"
    # Nothing answered, so nothing to record -- which is how this is told apart from
    # a node that said no.
    assert row.response_code is None


def test_a_data_node_we_have_never_seen_gets_a_row(engine):
    """
    An unseen data node is created rather than dropping the observation

    In practice there is nothing to create, because a fetched route came from a
    `DataAccess` row. But a derived `https://` twin can name a node reached by a path
    we never stored, and losing the health would be worse than inserting a row.
    """
    observer = record_data_node_calls(engine)

    observer(make_call(data_node="brand-new.example.org"))

    health = aggregate_data_node_health(engine, since=None)
    assert "brand-new.example.org" in health


def test_health_aggregates_per_data_node(engine):
    """Counts, failure rate and median throughput, per node"""
    observer = record_data_node_calls(engine)
    # 10 MB in 1s, and 10 MB in 2s: median throughput is 7.5 MB/s.
    observer(make_call(FAST, seconds=1.0))
    observer(make_call(FAST, seconds=2.0))
    observer(make_call(FLAKY, outcome=DownloadOutcome.TIMEOUT, bytes_transferred=0))

    health = aggregate_data_node_health(engine, since=None)

    assert health[FAST].n_calls == 2
    assert health[FAST].n_success == 2
    assert health[FAST].failure_rate == 0.0
    assert health[FAST].median_throughput_mb_per_s == pytest.approx(7.5)
    assert health[FAST].total_bytes == 20_000_000

    assert health[FLAKY].n_success == 0
    assert health[FLAKY].failure_rate == 1.0
    # Nothing succeeded, so there is no measurement -- reported as `None` rather than
    # `0.0`, so "we do not know" is not confused with "it was slow".
    assert health[FLAKY].median_throughput_mb_per_s is None


@pytest.mark.parametrize(
    "outcome, field",
    (
        (DownloadOutcome.MISSING, "n_missing"),
        (DownloadOutcome.NEEDS_CREDENTIALS, "n_needs_credentials"),
    ),
)
def test_a_404_or_a_401_is_counted_but_not_held_against_the_node(
    engine, outcome, field
):
    """
    The node answered correctly, so its failure rate must not move

    Counting these would retire a data node for telling us the truth: a route that has
    gone away is not the node misbehaving, and neither is being asked to authenticate.
    """
    observer = record_data_node_calls(engine)
    observer(make_call(FAST, outcome=outcome, bytes_transferred=0))

    health = aggregate_data_node_health(engine, since=None)[FAST]

    assert health.n_calls == 1
    # Not judged at all, so there is nothing to compute a rate over.
    assert health.n_judged == 0
    assert health.failure_rate == 0.0
    assert getattr(health, field) == 1


def test_blocked_does_count_against_the_node(engine):
    """
    A node that keeps refusing us is one we should stop trying first

    Which is arguably unfair -- a rate limit is correct behaviour -- but it is exactly
    what ranking is for.
    """
    observer = record_data_node_calls(engine)
    observer(make_call(FAST, outcome=DownloadOutcome.BLOCKED, bytes_transferred=0))

    health = aggregate_data_node_health(engine, since=None)[FAST]

    assert health.n_judged == 1
    assert health.failure_rate == 1.0
    assert health.n_blocked == 1


def test_the_since_window_excludes_older_attempts(engine):
    """
    Health is read over a window, which is the main divergence from search health

    Without it, lifetime totals would let one fast afternoon outrank a node that is
    fast today -- which is what the prototype did.
    """
    observer = record_data_node_calls(engine)
    observer(make_call(FAST))

    # Backdate the row so it falls outside a short window.
    with Session(engine) as session:
        row = session.exec(select(DataNodeCallRecord)).one()
        row.created_at = datetime.datetime.now(
            datetime.timezone.utc
        ) - datetime.timedelta(days=30)
        session.add(row)
        session.commit()

    assert FAST in aggregate_data_node_health(engine, since=None)
    assert FAST not in aggregate_data_node_health(
        engine, since=datetime.timedelta(days=7)
    )


def test_the_since_window_accepts_an_absolute_datetime(engine):
    """
    A `datetime` is an absolute cut-off, a `timedelta` is "this long ago"

    Worth pinning because the filter runs in SQL: `created_at` is stored
    timezone-aware but SQLite hands it back naive, so comparing in Python would raise.
    """
    observer = record_data_node_calls(engine)
    observer(make_call(FAST))

    now = datetime.datetime.now(datetime.timezone.utc)

    assert FAST in aggregate_data_node_health(
        engine, since=now - datetime.timedelta(minutes=1)
    )
    assert FAST not in aggregate_data_node_health(
        engine, since=now + datetime.timedelta(minutes=1)
    )


def test_asking_about_no_data_nodes_returns_nothing(engine):
    """An empty iterable means nothing, not everything"""
    observer = record_data_node_calls(engine)
    observer(make_call(FAST))

    assert aggregate_data_node_health(engine, [], since=None) == {}


def test_asking_about_some_data_nodes_leaves_the_others_out(engine):
    """
    The pool the caller cares about, not every node we have ever recorded

    The other half of the rule above: an empty list narrows to nothing, and a
    non-empty one narrows to itself.
    """
    observer = record_data_node_calls(engine)
    observer(make_call(FAST))
    observer(make_call(FLAKY))

    health = aggregate_data_node_health(engine, [FAST], since=None)

    assert set(health) == {FAST}


def test_reliability_is_ranked_before_speed():
    """
    A node that works beats a node that is quick

    Pure logic, no database: this is the preferencing rule on its own.
    """
    quick_but_flaky = health_of(FLAKY, failure_rate=0.5, throughput=80.0)
    slow_but_solid = health_of(FAST, failure_rate=0.0, throughput=20.0)

    ranked = sorted(
        [quick_but_flaky, slow_but_solid], key=rank_by_reliability_then_throughput
    )

    assert [each.data_node for each in ranked] == [FAST, FLAKY]


def test_speed_breaks_ties_on_reliability():
    """Among equally reliable nodes, the faster one wins"""
    slow = health_of("slow.example.org", failure_rate=0.0, throughput=5.0)
    fast = health_of(FAST, failure_rate=0.0, throughput=50.0)

    ranked = sorted([slow, fast], key=rank_by_reliability_then_throughput)

    assert [each.data_node for each in ranked] == [FAST, "slow.example.org"]


def test_ranking_by_throughput_puts_unmeasured_nodes_last():
    """
    `math.inf` rather than `0.0`, so an unmeasured node is not infinitely fast
    """
    measured = health_of(FAST, failure_rate=0.0, throughput=10.0)
    unmeasured = health_of(UNKNOWN, failure_rate=0.0, throughput=None)

    ranked = sorted([unmeasured, measured], key=rank_by_throughput)

    assert [each.data_node for each in ranked] == [FAST, UNKNOWN]


def test_the_health_selector_orders_a_files_routes(engine):
    """Routes on the healthier node are offered first"""
    observer = record_data_node_calls(engine)
    observer(make_call(FAST, seconds=1.0))
    observer(make_call(FLAKY, outcome=DownloadOutcome.TIMEOUT, bytes_transferred=0))

    selector = build_data_node_health_selector(engine, since=None)
    routes = [route_on(FLAKY), route_on(FAST)]

    assert selector(routes, 0).data_node == FAST
    assert selector(routes, 1).data_node == FLAKY
    # Exhausted, which is how the caller knows to stop.
    assert selector(routes, 2) is None


def test_a_node_with_no_health_is_still_tried_just_not_first(engine):
    """
    A data node we have never fetched from is unknown, not bad

    The same rule the search side's `_rank_pool` applies.
    """
    observer = record_data_node_calls(engine)
    observer(make_call(FAST))

    selector = build_data_node_health_selector(engine, since=None)
    routes = [route_on(UNKNOWN), route_on(FAST)]

    assert selector(routes, 0).data_node == FAST
    assert selector(routes, 1).data_node == UNKNOWN


def test_the_health_selector_falls_back_when_it_knows_nothing(engine):
    """
    With no health for any of a file's nodes, defer rather than invent a ranking

    `None` from `_rank_pool` is the signal, and the fallback decides.
    """
    selector = build_data_node_health_selector(engine, since=None)
    routes = [route_on(UNKNOWN), route_on("other.example.org")]

    # The default fallback keeps the given order.
    assert selector(routes, 0).data_node == UNKNOWN
    assert selector(routes, 1).data_node == "other.example.org"


def test_the_plain_health_selector_does_not_notice_new_health(engine):
    """
    A snapshot, by design: this is the behaviour the refreshing one exists to change

    Pinned so that the difference between the two selectors is a tested fact rather
    than a claim in a docstring.
    """
    observer = record_data_node_calls(engine)
    observer(make_call(FAST))

    selector = build_data_node_health_selector(engine, since=None)
    routes = [route_on(FLAKY), route_on(FAST)]
    assert selector(routes, 0).data_node == FAST

    # FAST goes bad, loudly.
    for _ in range(20):
        observer(make_call(FAST, outcome=DownloadOutcome.TIMEOUT, bytes_transferred=0))
    observer(make_call(FLAKY, seconds=1.0))

    # Still the old opinion, because the snapshot was taken at build time.
    assert selector(routes, 0).data_node == FAST


def test_the_refreshing_selector_re_reads_once_its_snapshot_expires(engine):
    """
    A multi-day download learns as it goes

    A selector built on Monday's health and still using it on Thursday sends every
    remaining file to a node that stopped being the best one days ago. The clock is
    injected so this takes milliseconds instead of three hours.
    """
    observer = record_data_node_calls(engine)
    observer(make_call(FAST, seconds=1.0))

    now = datetime.datetime(2026, 11, 2, 9, 0, tzinfo=datetime.timezone.utc)

    def clock():
        return now

    selector = build_refreshing_data_node_health_selector(
        engine,
        refresh_after=datetime.timedelta(hours=3),
        since=None,
        clock=clock,
    )
    routes = [route_on(FLAKY), route_on(FAST)]

    assert selector(routes, 0).data_node == FAST

    # FAST starts failing while the download is running.
    for _ in range(20):
        observer(make_call(FAST, outcome=DownloadOutcome.TIMEOUT, bytes_transferred=0))
    observer(make_call(FLAKY, seconds=1.0))

    # Before the refresh interval, the snapshot still stands.
    now += datetime.timedelta(hours=2)
    assert selector(routes, 0).data_node == FAST

    # After it, the selector has noticed.
    now += datetime.timedelta(hours=2)
    assert selector(routes, 0).data_node == FLAKY


def test_the_refreshing_selector_also_falls_back_when_it_knows_nothing(engine):
    """
    The refresh does not change what "no evidence at all" means

    The plain selector's fallback is pinned above. This is its twin: re-reading an
    empty table still produces no ranking, and the fallback still decides.
    """
    selector = build_refreshing_data_node_health_selector(engine, since=None)
    routes = [route_on(UNKNOWN), route_on("other.example.org")]

    # The default fallback keeps the given order.
    assert selector(routes, 0).data_node == UNKNOWN
    assert selector(routes, 1).data_node == "other.example.org"


def test_the_refreshing_selector_does_not_query_until_it_needs_to(engine):
    """
    Lazy: a download that stops asking stops querying

    Checked by counting reads through a counting clock, because the alternative is
    a background timer that keeps running after a download has finished.
    """
    observer = record_data_node_calls(engine)
    observer(make_call(FAST))

    reads = {"n": 0}
    now = datetime.datetime(2026, 11, 2, 9, 0, tzinfo=datetime.timezone.utc)

    def clock():
        reads["n"] += 1
        return now

    selector = build_refreshing_data_node_health_selector(
        engine, refresh_after=datetime.timedelta(hours=3), since=None, clock=clock
    )

    # Building it reads nothing: the first snapshot is taken on first use.
    assert reads["n"] == 0

    routes = [route_on(FAST)]
    selector(routes, 0)
    selector(routes, 0)

    # The clock is consulted per selection, but only to decide whether to re-read.
    assert reads["n"] == 2


def test_a_failure_rate_is_a_rate_over_every_attempt_that_is_evidence(engine):
    """
    A regression: a node that mostly works must not report a failure rate of 1.0
    """
    observer = record_data_node_calls(engine)
    for _ in range(9):
        observer(make_call(outcome=DownloadOutcome.SUCCESS))
    observer(make_call(outcome=DownloadOutcome.TIMEOUT))

    health = aggregate_data_node_health(engine)[FAST]

    assert health.n_calls == 10
    assert health.n_success == 9
    assert health.n_judged == 10
    assert health.failure_rate == pytest.approx(0.1)


def test_outcomes_that_say_nothing_about_the_node_stay_out_of_the_rate(engine):
    """
    The exclusions the rate is supposed to have, still excluded after the fix above
    """
    observer = record_data_node_calls(engine)
    observer(make_call(outcome=DownloadOutcome.SUCCESS))
    observer(make_call(outcome=DownloadOutcome.MISSING, response_code=404))
    observer(make_call(outcome=DownloadOutcome.NEEDS_CREDENTIALS, response_code=401))
    observer(make_call(outcome=DownloadOutcome.SIZE_MISMATCH))

    health = aggregate_data_node_health(engine)[FAST]

    assert health.n_calls == 4
    # Only the success is evidence about this node; the other three are not its fault.
    assert health.n_judged == 1
    assert health.failure_rate == 0.0
