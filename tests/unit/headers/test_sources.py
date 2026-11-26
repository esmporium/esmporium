"""
Tests of the byte sources the readers take
"""

from __future__ import annotations

import pytest

from esmporium.headers import (
    HeldBytesSource,
    LocalFileSource,
    TruncatedHeaderError,
    UnreadableHeaderError,
    read_file_header,
)

from .classic_netcdf import Variable, build


class TestHeldBytes:
    def test_held_bytes_are_served(self):
        source = HeldBytesSource({0: b"abcd", 100: b"wxyz"}, size=200)

        assert source.read(0, 4) == b"abcd"
        assert source.read(101, 2) == b"xy"
        assert (source.bytes_read, source.n_requests) == (8, 2)

    def test_bytes_not_held_are_refused_rather_than_made_up(self):
        source = HeldBytesSource({0: b"abcd", 100: b"wxyz"}, size=200)

        with pytest.raises(TruncatedHeaderError) as caught:
            source.read(2, 4)

        assert caught.value.bytes_needed == 6

    def test_a_read_outside_the_file_is_unreadable_not_truncated(self):
        """
        Something in the file pointing past its end is wrong, and more bytes will
        not help
        """
        source = HeldBytesSource(b"abcd", size=4)

        with pytest.raises(UnreadableHeaderError, match="outside the file"):
            source.read(2, 4)

    def test_pieces_reaching_past_the_end_are_refused(self):
        with pytest.raises(UnreadableHeaderError):
            HeldBytesSource({10: b"abcd"}, size=12)


class TestALocalFile:
    def test_a_local_file_reads_like_any_other(self, tmp_path):
        built = build(
            {"time": 0, "bnds": 2},
            [
                Variable(
                    "time",
                    ("time",),
                    "double",
                    [0.5, 1.5, 2.5],
                    {"units": "days since 1850-01-01", "bounds": "time_bnds"},
                ),
                Variable("time_bnds", ("time", "bnds"), "double", [0, 1, 1, 2, 2, 3]),
            ],
        )
        path = tmp_path / "tas.nc"
        path.write_bytes(built.whole())

        with LocalFileSource(path) as source:
            header = read_file_header(source, time_bounds=True)

        assert header.time_bounds is not None
        assert header.time_bounds.last_bounds == (2.0, 3.0)
        assert header.file_size == built.size
        # The whole file is smaller than one block, so it was read once.
        assert (header.n_requests, header.bytes_read) == (1, built.size)

    def test_reads_are_counted_in_blocks(self, tmp_path):
        path = tmp_path / "data.bin"
        path.write_bytes(bytes(range(256)) * 64)

        with LocalFileSource(path, block_bytes=1024) as source:
            assert source.read(0, 4) == bytes(range(4))
            assert source.read(10, 4) == bytes(range(10, 14))
            assert (source.n_requests, source.bytes_read) == (1, 1024)

            assert source.read(5000, 2) == bytes([5000 % 256, 5001 % 256])
            assert (source.n_requests, source.bytes_read) == (2, 2048)


class TestTheBudget:
    """
    The rules every fetching source shares, shown on a local file
    """

    @pytest.fixture
    def path(self, tmp_path):
        path = tmp_path / "data.bin"
        path.write_bytes(bytes(16 * 1024))

        return path

    def test_no_more_fetches_than_allowed(self, path):
        with LocalFileSource(path, block_bytes=1024, max_requests=2) as source:
            source.read(0, 1)
            source.read(4096, 1)

            with pytest.raises(TruncatedHeaderError, match="beyond the 2"):
                source.read(8192, 1)

    def test_no_more_bytes_than_allowed(self, path):
        with LocalFileSource(path, block_bytes=1024, max_bytes=1500) as source:
            source.read(0, 1)

            with pytest.raises(TruncatedHeaderError, match="past the 1500 bytes"):
                source.read(4096, 1000)

    def test_a_block_shrinks_to_what_was_asked_rather_than_be_refused(self, path):
        """
        Near the budget, a read is not refused for the sake of bytes it did not need
        """
        with LocalFileSource(path, block_bytes=1024, max_bytes=1500) as source:
            source.read(0, 1)
            source.read(4096, 100)

            assert source.bytes_read == 1124

    def test_a_block_stops_at_the_end_of_the_file(self, path):
        with LocalFileSource(path, block_bytes=4096) as source:
            source.read(16 * 1024 - 10, 10)

            assert source.bytes_read == 10
