"""
Tests of the header-read retry policy

The policy is deliberately far more reluctant than the download layer's, and these
tests are mostly about pinning that difference down so it cannot drift back.
"""

from __future__ import annotations

import pytest

from esmporium.datasets import DownloadOutcome, HeaderReadOutcome
from esmporium.headers import (
    DEFAULT_ATTEMPTS,
    HeaderNeedsCredentialsError,
    HeaderNodeBlockedError,
    HeaderNodeHostFaultError,
    HeaderReadTimeoutError,
    HeaderRouteMissingError,
    HeaderTransportError,
    TruncatedHeaderError,
    UnreadableHeaderError,
    UnsupportedHeaderFormatError,
    is_worth_retrying,
)
from esmporium.headers.retry import DEFAULT_BACKOFF_CAP_SECONDS, _wait

URL = "https://dap.esgf.example.org/thredds/fileServer/tas.nc"


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(HeaderNodeBlockedError(URL, "429"), id="blocked"),
        pytest.param(HeaderTransportError(URL, "refused"), id="transport"),
    ],
)
def test_only_a_busy_node_or_a_broken_connection_is_retried(error):
    assert is_worth_retrying(error)


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(HeaderNodeHostFaultError(URL, "DNS"), id="host-fault"),
        pytest.param(HeaderReadTimeoutError(URL, "stalled"), id="timeout"),
        pytest.param(HeaderRouteMissingError(URL, "404"), id="missing"),
        pytest.param(HeaderNeedsCredentialsError(URL, "401"), id="credentials"),
        pytest.param(TruncatedHeaderError(99, bytes_given=10), id="truncated"),
        pytest.param(UnreadableHeaderError("mangled"), id="unreadable"),
        pytest.param(UnsupportedHeaderFormatError(b"GRIB"), id="unsupported"),
        pytest.param(ValueError("not a header failure at all"), id="unclassified"),
    ],
)
def test_everything_else_is_not(error):
    assert not is_worth_retrying(error)


def test_a_timeout_is_not_retried_unlike_a_download():
    """
    The single most important difference from `esmporium.downloads.retry`

    A download retries a timeout because finishing a large transfer is worth another
    go. A header read is asking for a few kilobytes, so a node that did not manage
    that has something wrong with it, and the next mirror is the better bet. The
    prototype retried these and paid roughly seven and a half minutes per dead node
    per simulation for it.
    """
    assert DownloadOutcome.TIMEOUT.is_worth_retrying
    assert not HeaderReadOutcome.TIMEOUT.is_worth_retrying


def test_one_retry_by_default_not_two():
    """
    Two attempts against the download layer's three

    The cost being saved is the *waiting*, which is paid before reaching a mirror that
    works -- not the cost of the request, which is tiny.
    """
    assert DEFAULT_ATTEMPTS == 2


class TestHowLongToWait:
    @staticmethod
    def _state(error, attempt_number):
        class _Outcome:
            @staticmethod
            def exception():
                return error

        class _State:
            outcome = _Outcome()

        state = _State()
        state.attempt_number = attempt_number

        return state

    def test_a_retry_after_is_believed(self):
        error = HeaderNodeBlockedError(URL, "429", retry_after_seconds=7.0)

        assert _wait(self._state(error, 1)) == 7.0

    def test_a_nonsensical_retry_after_is_capped(self):
        """
        A node asking us to wait a day should not park the run for a day
        """
        error = HeaderNodeBlockedError(URL, "429", retry_after_seconds=86400.0)

        assert _wait(self._state(error, 1)) == DEFAULT_BACKOFF_CAP_SECONDS

    def test_backoff_grows_and_is_capped(self):
        error = HeaderTransportError(URL, "refused")

        first = _wait(self._state(error, 1))
        later = _wait(self._state(error, 2))
        much_later = _wait(self._state(error, 20))

        assert 1.0 <= first < later
        assert much_later <= DEFAULT_BACKOFF_CAP_SECONDS * 1.25
