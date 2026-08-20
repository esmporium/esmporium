"""
Test the retry policy for search requests

These pin the decision of what counts as worth retrying,
and the shape of the policy we build,
without sending (or retrying) a single real request.
The retry policy running for real is exercised through `fire` in `test_search.py`.
"""

from __future__ import annotations

import httpx
import pytest
from tenacity import wait_exponential

from esmporium.search.retry import _is_transient, build_transient_retrying


def _status_error(status_code: int) -> httpx.HTTPStatusError:
    """Build an HTTPStatusError carrying a response with the given status"""
    request = httpx.Request("GET", "https://example.invalid/search")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


@pytest.mark.parametrize(
    "status_code, transient",
    [
        (500, True),
        (502, True),
        (503, True),
        (400, False),
        (404, False),
        (422, False),
    ],
)
def test_is_transient_reads_status_codes(status_code, transient):
    """A 5xx is the node's problem and transient; a 4xx is a real 'no'"""
    assert _is_transient(_status_error(status_code)) is transient


def test_is_transient_treats_transport_errors_as_transient():
    """A connection that never landed is worth trying again"""
    exc = httpx.ConnectError("could not connect")
    assert _is_transient(exc) is True


def test_is_transient_ignores_unrelated_exceptions():
    """Something that is not an HTTP failure is not ours to retry"""
    assert _is_transient(ValueError("not an http problem")) is False


def test_is_transient_floor_is_configurable():
    """The floor for 'the node's problem' can be moved"""
    assert _is_transient(_status_error(499), transient_status_floor=499) is True
    assert _is_transient(_status_error(498), transient_status_floor=499) is False


def test_build_transient_retrying_stops_after_the_given_attempts():
    """The policy tries at most as many times as asked"""
    retrying = build_transient_retrying(3)

    assert retrying.stop.max_attempt_number == 3


def test_build_transient_retrying_backs_off_and_reraises():
    """The policy waits between tries and lets the last failure through"""
    retrying = build_transient_retrying(2)

    assert isinstance(retrying.wait, wait_exponential)
    assert retrying.reraise is True
