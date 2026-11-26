"""
Tests of reading a file's header from a data node, one range request at a time
"""

from __future__ import annotations

import socket
import ssl

import httpx
import pytest
from tests.conftest import CMIP5_FILE_SIZE, CMIP6_FILE_SIZE, recorded_pieces

from esmporium.datasets import HeaderReadOutcome
from esmporium.headers import (
    DEFAULT_BLOCK_BYTES,
    HeaderNeedsCredentialsError,
    HeaderNodeBlockedError,
    HeaderNodeHostFaultError,
    HeaderReadTimeoutError,
    HeaderRouteMissingError,
    HeaderTransportError,
    RangesNotHonouredError,
    TruncatedHeaderError,
    read_header,
)

from .conftest import NODE, URL, serving

WHOLE_FILE_BYTES = 368702896


def partial(prefix: bytes, *, file_size: int = WHOLE_FILE_BYTES) -> httpx.Response:
    """
    Build the `206` a well-behaved data node answers a range from byte zero with

    Parameters
    ----------
    prefix
        The bytes to serve

    file_size
        How long the whole file is, reported in `Content-Range`

    Returns
    -------
    :
        The response
    """
    return httpx.Response(
        206,
        content=prefix,
        headers={"Content-Range": f"bytes 0-{len(prefix) - 1}/{file_size}"},
    )


# A valid classic header declaring nothing: magic, `numrecs`, then absent dimension,
# attribute and variable lists.
EMPTY_CLASSIC_HEADER = b"CDF\x01" + bytes(28)


def test_a_range_request_from_the_start_is_what_gets_sent(route, client_for, no_retry):
    """
    One request, for a block from the start of the file, is all a header needs

    Worth pinning because the alternative -- letting a netCDF library open the URL and
    seek around inside it -- is what made this hard the first time, and it is not
    visible from the outside which one is happening.
    """
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Range"))

        return partial(EMPTY_CLASSIC_HEADER)

    with client_for(handler) as client:
        header = read_header(client, route, block_bytes=4096, retrying=no_retry)

    assert seen == ["bytes=0-4095"]
    assert dict(header.global_attributes) == {}
    assert (header.n_requests, header.bytes_read) == (1, len(EMPTY_CLASSIC_HEADER))


def test_a_real_cmip5_header_is_read_over_a_route(
    route, client_for, no_retry, cmip5_netcdf3_header
):
    """
    The happy path, end to end, with a real file's bytes behind a fake node
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return partial(cmip5_netcdf3_header, file_size=CMIP5_FILE_SIZE)

    with client_for(handler) as client:
        header = read_header(client, route, retrying=no_retry)

    assert header.global_attributes["parent_experiment_id"] == "piControl"
    assert header.global_attributes["parent_experiment_rip"] == "r1i1p1"
    assert header.variable_attributes["time"]["calendar"] == "noleap"
    assert header.route is route
    assert header.file_size == CMIP5_FILE_SIZE
    assert header.time_bounds is None


def test_the_file_size_comes_from_the_node_not_the_index(
    route, client_for, no_retry, cmip5_netcdf3_header
):
    """
    The node's `Content-Range` wins over what the search index published

    They are known to disagree, and the one that matters is what was actually served:
    HDF5 checks the length it is told against the superblock of the bytes in front of
    it, so believing the index would make a correctly-served file unreadable.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return partial(cmip5_netcdf3_header, file_size=CMIP5_FILE_SIZE)

    with client_for(handler) as client:
        header = read_header(
            client, route, retrying=no_retry, published_size=CMIP5_FILE_SIZE + 999
        )

    assert header.file_size == CMIP5_FILE_SIZE


def test_the_published_size_is_the_fallback_when_the_node_is_silent(
    needs_h5netcdf, route, client_for, no_retry, cmip6_netcdf4_prefix
):
    """
    A netCDF-4 file is unreadable without a size, so the index is better than nothing
    """

    def handler(request: httpx.Request) -> httpx.Response:
        # A `206` with no `Content-Range` at all, which is malformed but happens.
        return httpx.Response(206, content=cmip6_netcdf4_prefix)

    with client_for(handler) as client:
        header = read_header(
            client,
            route,
            retrying=no_retry,
            block_bytes=len(cmip6_netcdf4_prefix),
            published_size=CMIP6_FILE_SIZE,
        )

    assert header.global_attributes["parent_experiment_id"] == "piControl"
    assert header.file_size == CMIP6_FILE_SIZE


class TestReadingTimeBounds:
    """
    First and last time values, fetched as a few more ranges of the same file
    """

    def test_a_classic_file_costs_one_more_request(
        self, route, client_for, no_retry, collect_calls
    ):
        """
        The first record is inside the first block; the last is at the far end
        """
        observer, calls = collect_calls
        pieces, size = recorded_pieces("cmip5-netcdf3-time-bounds")
        asked: list[str] = []

        with client_for(serving(pieces, size=size, asked=asked)) as client:
            header = read_header(
                client, route, time_bounds=True, retrying=no_retry, observer=observer
            )

        assert header.time_bounds is not None
        assert header.time_bounds.last_bounds == (54719.0, 54750.0)
        assert header.n_requests == 2
        assert asked[0] == f"bytes=0-{DEFAULT_BLOCK_BYTES - 1}"
        assert [call.outcome for call in calls] == [HeaderReadOutcome.SUCCESS] * 2
        assert [call.attempt_number for call in calls] == [1, 2]

    def test_a_cmip6_file_fits_the_default_budget(
        self, needs_h5netcdf, route, client_for, no_retry
    ):
        """
        CMOR's one-value chunks put each end of the time coordinate far into the file
        """
        pieces, size = recorded_pieces("cmip6-netcdf4-time-bounds")

        with client_for(serving(pieces, size=size)) as client:
            header = read_header(client, route, time_bounds=True, retrying=no_retry)

        assert header.time_bounds is not None
        assert header.time_bounds.first_bounds == (0.0, 31.0)
        assert header.n_requests == 6

    def test_a_cmip7_file_costs_two_requests(
        self, needs_h5netcdf, route, client_for, no_retry
    ):
        """
        Measured: its time values sit just past the first block, not inside it
        """
        pieces, size = recorded_pieces("cmip7-netcdf4-time-bounds")

        with client_for(serving(pieces, size=size)) as client:
            header = read_header(client, route, time_bounds=True, retrying=no_retry)

        assert header.time_bounds is not None
        assert header.time_bounds.last_bounds == (4014.0, 4015.0)
        assert header.n_requests <= 2
        assert header.bytes_read <= 2 * DEFAULT_BLOCK_BYTES

    def test_going_past_the_budget_is_a_truncation_on_the_last_call(
        self, needs_h5netcdf, route, client_for, no_retry, collect_calls
    ):
        """
        A bigger budget is the fix, so the read says so rather than blaming the node
        """
        observer, calls = collect_calls
        pieces, size = recorded_pieces("cmip6-netcdf4-time-bounds")

        with client_for(serving(pieces, size=size)) as client:
            with pytest.raises(TruncatedHeaderError, match="beyond the 3"):
                read_header(
                    client,
                    route,
                    time_bounds=True,
                    max_requests=3,
                    retrying=no_retry,
                    observer=observer,
                )

        assert [call.outcome for call in calls] == [
            HeaderReadOutcome.SUCCESS,
            HeaderReadOutcome.SUCCESS,
            HeaderReadOutcome.TRUNCATED_HEADER,
        ]
        assert not calls[-1].outcome.counts_against_data_node

    def test_a_short_answer_to_a_later_range_is_a_transport_error(
        self, route, client_for, no_retry
    ):
        pieces, size = recorded_pieces("cmip5-netcdf3-time-bounds")
        whole = serving(pieces, size=size)

        def handler(request: httpx.Request) -> httpx.Response:
            answer = whole(request)
            if request.headers["Range"].startswith("bytes=0-"):
                return answer

            return httpx.Response(
                206,
                content=answer.content[:3],
                headers={"Content-Range": answer.headers["Content-Range"]},
            )

        with client_for(handler) as client:
            with pytest.raises(HeaderTransportError, match="served 3"):
                read_header(client, route, time_bounds=True, retrying=no_retry)


class TestANodeThatIgnoresTheRange:
    """
    A node which answers `200` with the whole body instead of `206` with a range

    The trap this mechanism introduces and the download layer already warns about. A
    CMIP5 file can be most of a gigabyte, so reading the body to its end would turn a
    header read into an unwanted download.
    """

    @staticmethod
    def _whole_file_handler(
        prefix: bytes, chunks_pulled: list[int]
    ) -> httpx.MockTransport:
        def body():
            # Counted *before* each yield, so a chunk this records is one the reader
            # actually asked for. Counting after would make an abandoned response
            # indistinguishable from a drained one, because the generator would never
            # be resumed to do the counting.
            chunks_pulled.append(1)
            yield prefix  # the real header
            for _ in range(1000):
                # Far more than anyone asked for: a CMIP5 file can be most of a
                # gigabyte, and this is what draining one would cost.
                chunks_pulled.append(1)
                yield bytes(1024 * 1024)

        return httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=body(), headers={"Content-Length": str(WHOLE_FILE_BYTES)}
            )
        )

    def test_the_header_is_still_read(self, route, no_retry, cmip5_netcdf3_header):
        pulled: list[int] = []
        transport = self._whole_file_handler(cmip5_netcdf3_header, pulled)

        with httpx.Client(transport=transport) as client:
            header = read_header(
                client,
                route,
                block_bytes=len(cmip5_netcdf3_header),
                retrying=no_retry,
            )

        assert header.global_attributes["parent_experiment_id"] == "piControl"

    def test_the_rest_of_the_body_is_not_read(
        self, route, no_retry, cmip5_netcdf3_header
    ):
        """
        We stop pulling as soon as we have enough, rather than draining the response
        """
        pulled: list[int] = []
        transport = self._whole_file_handler(cmip5_netcdf3_header, pulled)

        with httpx.Client(transport=transport) as client:
            read_header(
                client,
                route,
                block_bytes=len(cmip5_netcdf3_header),
                retrying=no_retry,
            )

        # One chunk carried everything we asked for, so nothing past it was pulled.
        assert pulled == [1]

    def test_it_is_recorded_so_the_node_can_be_told_apart(
        self, route, no_retry, cmip5_netcdf3_header, collect_calls
    ):
        """
        A usable answer, but one that cost us a connection the node flooded
        """
        observer, calls = collect_calls
        transport = self._whole_file_handler(cmip5_netcdf3_header, [])

        with httpx.Client(transport=transport) as client:
            read_header(
                client,
                route,
                block_bytes=len(cmip5_netcdf3_header),
                retrying=no_retry,
                observer=observer,
            )

        assert [call.range_honoured for call in calls] == [False]
        assert [call.outcome for call in calls] == [HeaderReadOutcome.SUCCESS]

    def test_the_whole_file_length_is_read_from_content_length(
        self, route, no_retry, cmip5_netcdf3_header
    ):
        """
        On a `200` the body *is* the file, so `Content-Length` is the file's length
        """
        transport = self._whole_file_handler(cmip5_netcdf3_header, [])

        with httpx.Client(transport=transport) as client:
            header = read_header(
                client,
                route,
                block_bytes=len(cmip5_netcdf3_header),
                retrying=no_retry,
            )

        assert header.file_size == WHOLE_FILE_BYTES

    def test_reading_past_the_first_block_is_refused(
        self, route, no_retry, cmip5_netcdf3_header, collect_calls
    ):
        """
        The last time value is near the end of the file, and the node would make us
        stream everything before it
        """
        observer, calls = collect_calls
        transport = self._whole_file_handler(cmip5_netcdf3_header, [])

        with httpx.Client(transport=transport) as client:
            with pytest.raises(RangesNotHonouredError) as caught:
                read_header(
                    client,
                    route,
                    time_bounds=True,
                    block_bytes=len(cmip5_netcdf3_header),
                    retrying=no_retry,
                    observer=observer,
                )

        assert caught.value.outcome.stops_this_data_node
        assert not caught.value.outcome.is_worth_retrying
        assert [call.outcome for call in calls] == [
            HeaderReadOutcome.RANGES_NOT_HONOURED
        ]

    def test_a_later_range_answered_whole_is_refused_and_recorded(
        self, route, client_for, no_retry, collect_calls
    ):
        observer, calls = collect_calls
        pieces, size = recorded_pieces("cmip5-netcdf3-time-bounds")
        well_behaved = serving(pieces, size=size)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.headers["Range"].startswith("bytes=0-"):
                return well_behaved(request)

            return httpx.Response(200, content=b"", headers={"Content-Length": "0"})

        with client_for(handler) as client:
            with pytest.raises(RangesNotHonouredError, match="whole file"):
                read_header(
                    client,
                    route,
                    time_bounds=True,
                    retrying=no_retry,
                    observer=observer,
                )

        assert [call.outcome for call in calls] == [
            HeaderReadOutcome.SUCCESS,
            HeaderReadOutcome.RANGES_NOT_HONOURED,
        ]
        assert calls[-1].response_code == 200
        assert not calls[-1].range_honoured


@pytest.mark.parametrize(
    ("status", "expected_error", "expected_outcome"),
    [
        (404, HeaderRouteMissingError, HeaderReadOutcome.MISSING),
        (410, HeaderRouteMissingError, HeaderReadOutcome.MISSING),
        (416, HeaderRouteMissingError, HeaderReadOutcome.MISSING),
        (401, HeaderNeedsCredentialsError, HeaderReadOutcome.NEEDS_CREDENTIALS),
        (403, HeaderNodeBlockedError, HeaderReadOutcome.BLOCKED),
        (429, HeaderNodeBlockedError, HeaderReadOutcome.BLOCKED),
        (503, HeaderNodeBlockedError, HeaderReadOutcome.BLOCKED),
        (500, HeaderTransportError, HeaderReadOutcome.TRANSPORT_ERROR),
        (502, HeaderTransportError, HeaderReadOutcome.TRANSPORT_ERROR),
    ],
)
def test_a_status_code_is_classified_by_its_number(  # noqa: PLR0913 - three fixtures plus the parametrised triple
    route, client_for, no_retry, status, expected_error, expected_outcome
):
    with client_for(lambda request: httpx.Response(status)) as client:
        with pytest.raises(expected_error) as caught:
            read_header(client, route, retrying=no_retry)

    assert caught.value.outcome == expected_outcome
    assert caught.value.response_code == status


def test_a_416_is_reported_as_an_empty_file(route, client_for, no_retry):
    """
    Any range from byte zero is satisfiable unless the file has no bytes
    """
    with client_for(lambda request: httpx.Response(416)) as client:
        with pytest.raises(HeaderRouteMissingError, match="empty file") as caught:
            read_header(client, route, retrying=no_retry)

    assert not caught.value.outcome.counts_against_data_node
    assert not caught.value.outcome.is_worth_retrying


def test_a_retry_after_is_carried_off_a_block(route, client_for, no_retry):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "12"})

    with client_for(handler) as client:
        with pytest.raises(HeaderNodeBlockedError) as caught:
            read_header(client, route, retrying=no_retry)

    assert caught.value.retry_after_seconds == 12.0


def test_connecting_timing_out_is_a_host_fault(route, client_for, no_retry):
    """
    A connection that never completes is what a black-holed node looks like

    Told apart from a stall *after* connecting, because only this one means the node
    is unreachable -- and so only this one is a reason to give up on its other routes.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout(  # noqa: TRY003 - simulating a library error
            "timed out", request=request
        )

    with client_for(handler) as client:
        with pytest.raises(HeaderNodeHostFaultError, match="black-holed") as caught:
            read_header(client, route, retrying=no_retry)

    assert caught.value.outcome == HeaderReadOutcome.HOST_FAULT
    assert caught.value.response_code is None
    assert caught.value.outcome.stops_this_data_node


def test_stalling_after_connecting_is_a_timeout(route, client_for, no_retry):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("stalled", request=request)

    with client_for(handler) as client:
        with pytest.raises(HeaderReadTimeoutError, match="connected and stalled"):
            read_header(client, route, retrying=no_retry)


class TestTellingConnectionFailuresApart:
    """
    Refusal, DNS failure and TLS failure all arrive as `httpx.ConnectError`
    """

    @staticmethod
    def _client_raising(client_for, cause: BaseException):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(  # noqa: TRY003 - simulating a library error
                "could not connect", request=request
            ) from cause

        return client_for(handler)

    def test_a_refusal_keeps_its_retry(self, route, client_for, no_retry):
        cause = ConnectionRefusedError(61, "Connection refused")

        with self._client_raising(client_for, cause) as client:
            with pytest.raises(HeaderTransportError, match="refused") as caught:
                read_header(client, route, retrying=no_retry)

        assert caught.value.outcome.is_worth_retrying
        assert not caught.value.outcome.stops_this_data_node

    def test_a_reset_during_connect_keeps_its_retry(self, route, client_for, no_retry):
        with self._client_raising(client_for, ConnectionResetError()) as client:
            with pytest.raises(HeaderTransportError) as caught:
                read_header(client, route, retrying=no_retry)

        assert caught.value.outcome.is_worth_retrying

    @pytest.mark.parametrize(
        "cause",
        [
            pytest.param(
                socket.gaierror(8, "nodename nor servname provided"), id="dns"
            ),
            pytest.param(
                ssl.SSLCertVerificationError("certificate verify failed"), id="tls"
            ),
            pytest.param(RuntimeError("something we have never seen"), id="unknown"),
        ],
    )
    def test_an_unreachable_node_does_not(self, route, client_for, no_retry, cause):
        """
        Including a cause we have never seen, which is treated as unreachable
        """
        with self._client_raising(client_for, cause) as client:
            with pytest.raises(HeaderNodeHostFaultError) as caught:
                read_header(client, route, retrying=no_retry)

        assert not caught.value.outcome.is_worth_retrying
        assert caught.value.outcome.stops_this_data_node

    def test_a_tls_failure_is_not_mistaken_for_a_refusal(
        self, route, client_for, no_retry
    ):
        """
        `ssl.SSLError` and `ConnectionRefusedError` are both `OSError` subclasses

        So the order the two lists are checked in is load-bearing, not incidental.
        """
        with self._client_raising(
            client_for, ssl.SSLError("protocol mismatch")
        ) as client:
            with pytest.raises(HeaderNodeHostFaultError):
                read_header(client, route, retrying=no_retry)


def test_a_block_is_retried_on_the_same_route(
    route, client_for, fast_retrying, cmip5_netcdf3_header
):
    """
    The one node-level failure a header read tries again
    """
    answers = [
        httpx.Response(503),
        partial(cmip5_netcdf3_header, file_size=CMIP5_FILE_SIZE),
    ]

    with client_for(lambda request: answers.pop(0)) as client:
        header = read_header(client, route, retrying=fast_retrying(2))

    assert header.global_attributes["parent_experiment_id"] == "piControl"
    assert answers == []


def test_a_host_fault_is_not_retried_on_the_same_route(
    route, client_for, fast_retrying
):
    """
    The whole point of a separate retry policy

    The download layer would try this three times. A header read tries once and moves
    on, because a node we could not reach will not become reachable and the next
    mirror is where the answer is.
    """
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.ConnectError(  # noqa: TRY003 - simulating a library error
            "no route", request=request
        ) from socket.gaierror(8)

    with client_for(handler) as client:
        with pytest.raises(HeaderNodeHostFaultError):
            read_header(client, route, retrying=fast_retrying(3))

    assert len(attempts) == 1


def test_every_attempt_is_observed_exactly_once(
    route, client_for, fast_retrying, collect_calls, cmip5_netcdf3_header
):
    observer, calls = collect_calls
    answers = [
        httpx.Response(503),
        partial(cmip5_netcdf3_header, file_size=CMIP5_FILE_SIZE),
    ]

    with client_for(lambda request: answers.pop(0)) as client:
        read_header(
            client,
            route,
            retrying=fast_retrying(2),
            observer=observer,
            file_id=42,
        )

    assert [call.outcome for call in calls] == [
        HeaderReadOutcome.BLOCKED,
        HeaderReadOutcome.SUCCESS,
    ]
    assert [call.attempt_number for call in calls] == [1, 2]
    assert {call.data_node for call in calls} == {NODE}
    assert {call.url for call in calls} == {URL}
    assert {call.file_id for call in calls} == {42}
    assert {call.data_access_id for call in calls} == {7}
    assert {call.http_method for call in calls} == {"GET"}
    assert calls[0].bytes_transferred == 0
    assert calls[1].bytes_transferred == len(cmip5_netcdf3_header)
    assert calls[1].bytes_requested == DEFAULT_BLOCK_BYTES


def test_a_header_that_does_not_parse_is_observed_as_a_failed_call(
    route, client_for, no_retry, collect_calls
):
    """
    Bytes that arrive and do not parse are not a successful read

    Fetching and parsing are one read on purpose: the question an observer is
    answering is "did asking this node for this header work".
    """
    observer, calls = collect_calls

    with client_for(lambda request: partial(b"GRIB" + bytes(512))) as client:
        with pytest.raises(Exception, match="neither classic netCDF"):
            read_header(client, route, retrying=no_retry, observer=observer)

    assert [call.outcome for call in calls] == [HeaderReadOutcome.UNSUPPORTED_FORMAT]
    assert calls[0].response_code == 206
    assert not calls[0].outcome.counts_against_data_node
