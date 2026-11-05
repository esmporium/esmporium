r"""
Reading global attributes out of a netCDF-4/HDF5 file's header

netCDF-4 files are HDF5 files -- they begin `\x89HDF\r\n\x1a\n` -- and a netCDF global
attribute is an HDF5 attribute on the root group. Unlike the classic format, this one
is not a sequential structure that can be walked with `struct.unpack`: the files ESGF
serves keep 45 to 52 global attributes, well past HDF5's threshold of eight for
storing them inline, so they live in a *fractal heap* indexed by a *version 2 B-tree*.
Reading that by hand is several hundred lines of format-version-specific work, so this
module uses `h5py` instead.

`h5py` is an **optional extra** (`pip install esmporium[headers]`), imported inside the
function that needs it. Classic netCDF needs nothing installed, so a CMIP5 archive
reads with no extra at all; CMIP6 is entirely netCDF-4 and does need it.

**`h5py`, not `netCDF4`**, although `netCDF4` can also do this and the prototype which
came before us used it. `netCDF4` opens a *URL* and does its own HTTP inside the C
library, which is where every one of that prototype's failure modes came from: a stall
that no Python timeout could interrupt, so each read needed its own killable
subprocess; HTTP statuses reaching Python only as text inside an `OSError`, so failures
had to be classified by searching their messages; and a child wedged in an
uninterruptible kernel read surviving `SIGKILL`, which deadlocked every worker at once.
`h5py` opens a *file-like object*, so the bytes are fetched by
[esmporium.headers.fetching][] with `httpx` and handed over already in memory. Nothing
here touches the network.

The one thing that makes reading a fetched prefix work at all: HDF5 needs to be told
how long the **whole** file is, because the superblock records an end-of-file address
and HDF5 checks it. Told the prefix's own length it refuses to open the file at all
(`truncated file: eof = 65536, sblock->base_addr = 0`). So
[read_hdf5_global_attributes][(m).read_hdf5_global_attributes] takes the real size and
serves reads from the prefix underneath it.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

from esmporium.headers.errors import (
    HeaderParserUnavailableError,
    TruncatedHeaderError,
    UnreadableHeaderError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    # Available to type checkers only, and the type `io.RawIOBase.readinto` really
    # takes -- annotating it as `memoryview` narrows the override and breaks
    # substitutability.
    from _typeshed import WriteableBuffer

HDF5_MAGIC = b"\x89HDF\r\n\x1a\n"
r"""The first eight bytes of an HDF5 file, and so of a netCDF-4 one"""


class _NeedMoreBytes(Exception):
    """
    Raised inside the file-like object when HDF5 reads past the fetched prefix

    Never escapes this module: it is how a read beyond what we hold travels back out
    through HDF5's C layer to become a
    [TruncatedHeaderError][esmporium.headers.TruncatedHeaderError].
    """

    def __init__(self, wanted_up_to: int) -> None:
        self.wanted_up_to = wanted_up_to
        super().__init__(f"HDF5 asked for bytes up to {wanted_up_to}")


class _PrefixFile(io.RawIOBase):
    """
    A read-only file-like object over the first bytes of a much longer file

    Claims to be `file_size` bytes long, because HDF5 will not open a file whose
    apparent length disagrees with the end-of-file address in its superblock. Reads
    inside the prefix are served from it, and a read beyond it **raises**.

    Raising rather than returning zeros is the whole point. Zero-filling would let
    HDF5 make decisions about bytes we never fetched and hand back attributes read out
    of padding, which is a wrong answer presented as a right one. Raising turns the
    same situation into "fetch more and try again".
    """

    def __init__(self, prefix: bytes, file_size: int) -> None:
        self._prefix = prefix
        self._file_size = file_size
        self._position = 0

        self.highest_offset_read = 0
        """How far into the file HDF5 got, for reporting what a read actually cost"""

    def readable(self) -> bool:
        """
        Whether this can be read from

        Returns
        -------
        :
            Always `True`
        """
        return True

    def seekable(self) -> bool:
        """
        Whether this can be seeked in

        Returns
        -------
        :
            Always `True`. HDF5 seeks constantly, so answering `False` would stop it
            opening the file.
        """
        return True

    def seek(self, offset: int, whence: int = io.SEEK_SET, /) -> int:
        """
        Move the cursor, which may legitimately go beyond the prefix

        Seeking past what we hold is allowed; only *reading* there is not. HDF5 seeks
        before it knows whether it will read, so refusing here would reject files that
        are perfectly readable.

        Parameters
        ----------
        offset
            Where to move to, interpreted according to `whence`

        whence
            One of `io.SEEK_SET`, `io.SEEK_CUR` or `io.SEEK_END`

        Returns
        -------
        :
            The new position
        """
        if whence == io.SEEK_SET:
            self._position = offset
        elif whence == io.SEEK_CUR:
            self._position += offset
        else:
            self._position = self._file_size + offset

        return self._position

    def tell(self) -> int:
        """
        Where the cursor is

        Returns
        -------
        :
            The current position
        """
        return self._position

    def readinto(self, buffer: WriteableBuffer, /) -> int:
        """
        Fill `buffer` from the prefix

        Parameters
        ----------
        buffer
            Where to put the bytes

        Returns
        -------
        :
            How many bytes were written, which is zero at or past the end of the file

        Raises
        ------
        _NeedMoreBytes
            The read reaches past the prefix we hold.
        """
        # Normalised to a byte-wide memoryview: the type the stdlib declares is any
        # writable buffer, which has no length and no slice assignment in general.
        # `memoryview` gives both, for every buffer HDF5 actually passes.
        view = memoryview(buffer).cast("B")

        available = max(0, self._file_size - self._position)
        count = min(len(view), available)
        end = self._position + count
        if count == 0:
            return 0

        if end > len(self._prefix):
            raise _NeedMoreBytes(end)

        view[:count] = self._prefix[self._position : end]
        self._position = end
        self.highest_offset_read = max(self.highest_offset_read, end)

        return count


def _plain_python(value: object) -> object:
    """
    Turn one `h5py` attribute value into something that is not a numpy object

    `h5py` hands back `numpy` scalars, arrays and fixed-length byte strings. Those
    carry a dependency into every caller and compare in surprising ways, so they are
    reduced here, at the boundary -- the only place in this package that sees them.

    Done by duck typing rather than by importing `numpy`, so that this module's only
    import is the one optional extra it is named for.

    Parameters
    ----------
    value
        Whatever `h5py` returned

    Returns
    -------
    :
        A `str` for text, a number for a scalar, or a `tuple` for several values

        Text is decoded as UTF-8 with trailing NULs stripped, since HDF5 stores a
        fixed-width string padded out to its declared length.
    """
    # A one-element array is a scalar as far as an attribute is concerned, so unwrap
    # it. `item()` exists on numpy scalars too, where it is already the value.
    item = getattr(value, "item", None)
    to_list = getattr(value, "tolist", None)
    if callable(item) and getattr(value, "size", 1) == 1:
        value = item()
    elif callable(to_list):
        listed = to_list()
        if isinstance(listed, list):
            return tuple(_plain_python(each) for each in listed)

        return _plain_python(listed)

    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").rstrip("\x00")

    if isinstance(value, str):
        return value.rstrip("\x00")

    return value


def read_hdf5_global_attributes(
    prefix: bytes, *, file_size: int
) -> Mapping[str, object]:
    """
    Read every global attribute from the start of a netCDF-4/HDF5 file

    Parameters
    ----------
    prefix
        The first bytes of the file, starting at byte zero

        Has to reach the attribute *values*, not just their names, and those are
        further in than one might expect: on a sampled CMIP6 file the names were
        readable within 1.9 kB but the values needed 42.3 kB. 64 kB is the default ask
        for that reason.

    file_size
        How long the **whole** file is, in bytes

        Not optional and not guessable. HDF5 compares this against the end-of-file
        address in the superblock and refuses to open a file where they disagree, so
        passing `len(prefix)` fails outright. Take it from the `Content-Range` of the
        response that fetched `prefix` rather than from the search index, because the
        index and the served file are known to disagree.

    Returns
    -------
    :
        Every global attribute, with `numpy` types reduced to plain Python ones

        Nothing is renamed or reinterpreted: what an attribute means is a question
        about the project that wrote the file, not about the format.

    Raises
    ------
    HeaderParserUnavailableError
        `h5py` is not installed, so netCDF-4 files cannot be read at all.

    TruncatedHeaderError
        HDF5 needed bytes beyond `prefix`. Fetch more and try again; another mirror
        will not help, because every copy of the file has this layout.

    UnreadableHeaderError
        HDF5 would not open these bytes, or would not read their attributes.
    """
    try:
        import h5py  # noqa: PLC0415 - the optional `headers` extra, imported on use
    except ImportError as error:
        raise HeaderParserUnavailableError(prefix[: len(HDF5_MAGIC)]) from error

    opened = _PrefixFile(prefix, file_size)
    try:
        with h5py.File(opened, "r") as handle:
            root = handle["/"]

            return {str(name): _plain_python(root.attrs[name]) for name in root.attrs}
    except _NeedMoreBytes as error:
        raise TruncatedHeaderError(
            error.wanted_up_to, bytes_given=len(prefix)
        ) from error
    except OSError as error:
        # HDF5 reports everything from "this is not an HDF5 file" to a mangled
        # fractal heap as an `OSError`, with no code to tell them apart. They all
        # mean the same thing to us: these bytes did not yield a header.
        msg = f"h5py could not read a header out of these bytes: {error}"
        raise UnreadableHeaderError(msg) from error
