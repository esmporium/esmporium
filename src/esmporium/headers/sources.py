"""
Where a file's bytes come from: a data node, a local file, or bytes already in hand

Every reader in this package takes a [ByteSource][(m).ByteSource] and asks it for
the bytes at an offset. It is not told where they come from, so one parser serves a
remote file, a local copy and a test fixture alike.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Protocol

from esmporium.headers.errors import TruncatedHeaderError, UnreadableHeaderError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import TracebackType

DEFAULT_BLOCK_BYTES = 64 * 1024
"""
The least a source fetches whenever a read asks for bytes it does not hold yet

Sized from measurement: the header of every CMIP5 and CMIP6 file sampled, global and
variable attributes included, ends within 42,231 bytes, so one block from the start
of a file holds all of it. Readers then ask for many small pieces, and a block turns
those into one fetch.
"""

DEFAULT_MAX_BYTES = 1024 * 1024
"""The most a source fetches from one file, by default"""

DEFAULT_MAX_REQUESTS = 8
"""
The most separate fetches a source makes of one file, by default

Reading a header takes one. Reading the first and last time bounds as well takes a
few more where the time values are stored apart from the header: one for each end,
and one for an index of where the values are, if that lies past the first block.
"""


class ByteSource(Protocol):
    """
    Something which hands out a file's bytes by offset

    Implementations: [HeldBytesSource][(m).HeldBytesSource] for bytes already in
    hand, [LocalFileSource][(m).LocalFileSource] for a file on disk, and
    [RangeSource][esmporium.headers.RangeSource] for a file on a data node.
    """

    @property
    def size(self) -> int:
        """
        How long the whole file is, in bytes

        Returns
        -------
        :
            The length
        """

    @property
    def bytes_read(self) -> int:
        """
        How many bytes have been fetched or read from the file so far

        Returns
        -------
        :
            The count
        """

    @property
    def n_requests(self) -> int:
        """
        How many separate fetches or reads of the file there have been so far

        Returns
        -------
        :
            The count
        """

    def read(self, offset: int, count: int) -> bytes:
        """
        Get `count` bytes of the file, starting at `offset`

        Parameters
        ----------
        offset
            Where to start, counted from the start of the file

        count
            How many bytes to get

        Returns
        -------
        :
            Exactly `count` bytes

        Raises
        ------
        esmporium.headers.TruncatedHeaderError
            The bytes are not held, and getting them is not allowed: they are beyond
            what the source holds, or fetching them would exceed its budget.

        esmporium.headers.UnreadableHeaderError
            The bytes asked for run past the end of the file, so whatever pointed
            there is wrong.
        """


def check_within_file(offset: int, count: int, *, size: int) -> None:
    """
    Check that a read lies inside the file

    Parameters
    ----------
    offset
        Where the read starts

    count
        How many bytes it wants

    size
        How long the file is

    Raises
    ------
    UnreadableHeaderError
        The read starts before the file or runs past its end.
    """
    if offset < 0 or count < 0 or offset + count > size:
        msg = (
            f"something in this file points at {count} bytes from offset {offset}, "
            f"which is outside the file's {size} bytes"
        )
        raise UnreadableHeaderError(msg)


class HeldBytesSource:
    r"""
    A file's bytes which are already in hand, in one or more pieces

    Serves only what it holds, and raises for anything else rather than making it
    up. Used for test fixtures, and for reading bytes fetched some other way.

    Examples
    --------
    >>> source = HeldBytesSource(b"CDF\x01rest of the header", size=1000)
    >>> source.read(0, 4)
    b'CDF\x01'

    >>> try:
    ...     source.read(500, 4)
    ... except TruncatedHeaderError as error:
    ...     error.bytes_needed
    504
    """

    def __init__(self, held: bytes | Mapping[int, bytes], *, size: int) -> None:
        """
        Initialise

        Parameters
        ----------
        held
            The bytes held: either the start of the file, or a mapping from the
            offset each piece starts at to the piece

        size
            How long the **whole** file is, which is more than what is held
            whenever only part of the file is
        """
        pieces = {0: held} if isinstance(held, bytes) else dict(held)
        for start, piece in pieces.items():
            check_within_file(start, len(piece), size=size)

        self._pieces = pieces
        self._size = size
        self._bytes_read = sum(len(piece) for piece in pieces.values())
        self._n_requests = len(pieces)

    @property
    def size(self) -> int:
        """
        How long the whole file is

        Returns
        -------
        :
            The length
        """
        return self._size

    @property
    def bytes_read(self) -> int:
        """
        How many bytes are held

        Returns
        -------
        :
            The count
        """
        return self._bytes_read

    @property
    def n_requests(self) -> int:
        """
        How many pieces the bytes are in

        Returns
        -------
        :
            The count
        """
        return self._n_requests

    def read(self, offset: int, count: int) -> bytes:
        """
        Get `count` held bytes, starting at `offset`

        Parameters
        ----------
        offset
            Where to start

        count
            How many bytes to get

        Returns
        -------
        :
            Exactly `count` bytes

        Raises
        ------
        TruncatedHeaderError
            Those bytes are not all held in one piece.
        """
        check_within_file(offset, count, size=self._size)
        for start, piece in self._pieces.items():
            if start <= offset and offset + count <= start + len(piece):
                return piece[offset - start : offset - start + count]

        raise TruncatedHeaderError(
            offset + count,
            reason=(
                f"only {self._bytes_read} bytes of it are held, in "
                f"{self._n_requests} piece(s), and these are not among them"
            ),
        )


class BlockCache:
    """
    The bytes of a file fetched so far, and the rule for fetching more

    Shared by every source which fetches: a read of bytes it does not hold fetches at
    least `block_bytes` from where the read starts, and refuses to go past a budget of
    bytes and of fetches. Subclasses say how one fetch is done.
    """

    def __init__(
        self,
        *,
        block_bytes: int = DEFAULT_BLOCK_BYTES,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_requests: int = DEFAULT_MAX_REQUESTS,
    ) -> None:
        """
        Initialise

        Parameters
        ----------
        block_bytes
            The least to fetch whenever a read misses what is held

        max_bytes
            The most to fetch from the file in total

        max_requests
            The most separate fetches to make of the file
        """
        self.block_bytes = block_bytes
        self.max_bytes = max_bytes
        self.max_requests = max_requests
        self._pieces: dict[int, bytes] = {}
        self._bytes_read = 0
        self._n_requests = 0

    @property
    def bytes_read(self) -> int:
        """
        How many bytes have been fetched so far

        Returns
        -------
        :
            The count
        """
        return self._bytes_read

    @property
    def n_requests(self) -> int:
        """
        How many fetches there have been so far

        Returns
        -------
        :
            The count
        """
        return self._n_requests

    def _held(self, offset: int, count: int) -> bytes | None:
        """
        Get bytes from what is already held, if one piece holds all of them

        Parameters
        ----------
        offset
            Where to start

        count
            How many bytes

        Returns
        -------
        :
            The bytes, or `None` if no single piece holds them
        """
        for start, piece in self._pieces.items():
            if start <= offset and offset + count <= start + len(piece):
                return piece[offset - start : offset - start + count]

        return None

    def _fetch(self, start: int, end: int) -> bytes:
        """
        Fetch the bytes from `start` up to, not including, `end`

        Parameters
        ----------
        start
            The first byte to fetch

        end
            One past the last byte to fetch

        Returns
        -------
        :
            Exactly `end - start` bytes
        """
        raise NotImplementedError

    def _file_size(self) -> int:
        """
        How long the file is

        Returns
        -------
        :
            The length
        """
        raise NotImplementedError

    def _read_through_cache(self, offset: int, count: int) -> bytes:
        """
        Get bytes from what is held, fetching a block from `offset` if they are not

        Parameters
        ----------
        offset
            Where to start

        count
            How many bytes to get

        Returns
        -------
        :
            Exactly `count` bytes

        Raises
        ------
        TruncatedHeaderError
            Fetching them would go past the budget.
        """
        held = self._held(offset, count)
        if held is not None:
            return held

        size = self._file_size()
        check_within_file(offset, count, size=size)
        end = min(size, offset + max(count, self.block_bytes))

        if self._n_requests + 1 > self.max_requests:
            raise TruncatedHeaderError(
                offset + count,
                reason=(
                    f"getting them would take a fetch beyond the {self.max_requests} "
                    "this read may make"
                ),
            )

        if self._bytes_read + (end - offset) > self.max_bytes:
            # Shrunk to only what was asked for before giving up, so a read near the
            # budget is not refused for the sake of a block it did not need.
            end = offset + count

        if self._bytes_read + (end - offset) > self.max_bytes:
            raise TruncatedHeaderError(
                offset + count,
                reason=(
                    f"getting them would take this read past the {self.max_bytes} "
                    f"bytes it may fetch, of which it has already fetched "
                    f"{self._bytes_read}"
                ),
            )

        fetched = self._fetch(offset, end)
        self._pieces[offset] = fetched
        self._bytes_read += len(fetched)
        self._n_requests += 1

        return fetched[:count]


class LocalFileSource(BlockCache):
    """
    A file on disk

    Reads in blocks, and counts what it reads, as a remote source does, so a local
    read can be compared with a remote one on the same terms.

    Use as a context manager, or call [close][(c).close], to close the file.
    """

    def __init__(
        self,
        path: os.PathLike[str] | str,
        *,
        block_bytes: int = DEFAULT_BLOCK_BYTES,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_requests: int = DEFAULT_MAX_REQUESTS,
    ) -> None:
        """
        Initialise, opening the file

        Parameters
        ----------
        path
            The file to read

        block_bytes
            The least to read whenever a read misses what is held

        max_bytes
            The most to read from the file in total

        max_requests
            The most separate reads to make of the file
        """
        super().__init__(
            block_bytes=block_bytes, max_bytes=max_bytes, max_requests=max_requests
        )
        self.path = path
        self._handle = open(path, "rb")
        self._size = os.fstat(self._handle.fileno()).st_size

    @property
    def size(self) -> int:
        """
        How long the file is

        Returns
        -------
        :
            The length
        """
        return self._size

    def _file_size(self) -> int:
        return self._size

    def _fetch(self, start: int, end: int) -> bytes:
        self._handle.seek(start)
        fetched = self._handle.read(end - start)
        if len(fetched) != end - start:
            msg = (
                f"{self.path} gave {len(fetched)} bytes from offset {start} where it "
                f"is {self._size} bytes long and {end - start} were asked for"
            )
            raise UnreadableHeaderError(msg)

        return fetched

    def read(self, offset: int, count: int) -> bytes:
        """
        Get `count` bytes of the file, starting at `offset`

        Parameters
        ----------
        offset
            Where to start

        count
            How many bytes to get

        Returns
        -------
        :
            Exactly `count` bytes
        """
        return self._read_through_cache(offset, count)

    def close(self) -> None:
        """Close the file"""
        self._handle.close()

    def __enter__(self) -> LocalFileSource:
        """
        Use as a context manager

        Returns
        -------
        :
            This source
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the file, however the block ended"""
        self.close()
