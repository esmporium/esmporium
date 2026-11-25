r"""
Reading a netCDF-4/HDF5 file's header, and single values out of its data

Read with `h5netcdf`, which presents the netCDF data model over `h5py`, through a
file-like object over a [ByteSource][esmporium.headers.ByteSource]. So the bytes are
fetched by this package rather than by the HDF5 library, and only the ones HDF5 asks
for are fetched at all.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Any

from esmporium.headers.contents import (
    FileHeader,
    FirstAndLastTimeBounds,
    HeaderLayout,
)
from esmporium.headers.errors import (
    HeaderParseError,
    HeaderReadError,
    HeaderReaderUnavailableError,
    UnreadableHeaderError,
)

if TYPE_CHECKING:
    # Available to type checkers only, and the type `io.RawIOBase.readinto` really
    # takes -- annotating it as `memoryview` narrows the override and breaks
    # substitutability.
    from _typeshed import WriteableBuffer

    from esmporium.headers.sources import ByteSource

HDF5_MAGIC = b"\x89HDF\r\n\x1a\n"
"""The first eight bytes of an HDF5 file, and so of a netCDF-4 one"""


class _SourceFile(io.RawIOBase):
    """
    A read-only file-like object over a [ByteSource][esmporium.headers.ByteSource]

    Claims to be as long as the source says the whole file is, because HDF5 will not
    open a file whose apparent length disagrees with the end-of-file address in its
    superblock. Every read is handed to the source, which serves it, fetches it, or
    raises.

    A source raising rather than returning zeros is what keeps this honest.
    Zero-filling would let HDF5 make decisions about bytes we never fetched and hand
    back attributes read out of padding, which is a wrong answer presented as a right
    one. The exception travels back out through HDF5 unchanged.
    """

    def __init__(self, source: ByteSource) -> None:
        self._source = source
        self._size = source.size
        self._position = 0

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
        Move the cursor

        Seeking anywhere is allowed, without fetching anything: HDF5 seeks before it
        knows whether it will read.

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
            self._position = self._size + offset

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
        Fill `buffer` from the source

        Parameters
        ----------
        buffer
            Where to put the bytes

        Returns
        -------
        :
            How many bytes were written, which is zero at or past the end of the file
        """
        # Normalised to a byte-wide memoryview: the type the stdlib declares is any
        # writable buffer, which has no length and no slice assignment in general.
        # `memoryview` gives both, for every buffer HDF5 actually passes.
        view = memoryview(buffer).cast("B")

        count = min(len(view), max(0, self._size - self._position))
        if count == 0:
            return 0

        view[:count] = self._source.read(self._position, count)
        self._position += count

        return count


def _plain_python(value: object) -> object:
    """
    Turn one value from `h5netcdf` into something that is not a numpy object

    `h5netcdf` hands back `numpy` scalars, arrays and fixed-length byte strings. Those
    carry a dependency into every caller and compare in surprising ways, so they are
    reduced here, at the boundary -- the only place in this package that sees them.

    Done by duck typing rather than by importing `numpy`, so that this module's only
    import is the one optional extra it is named for.

    Parameters
    ----------
    value
        Whatever `h5netcdf` returned

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


def _number(value: object, *, variable: str) -> float | int:
    """
    Reduce one value read out of a variable to a plain number

    Parameters
    ----------
    value
        What `h5netcdf` returned for one element

    variable
        The variable it came from, for the error message

    Returns
    -------
    :
        The number

    Raises
    ------
    UnreadableHeaderError
        The value is not a number.
    """
    plain = _plain_python(value)
    if isinstance(plain, bool) or not isinstance(plain, int | float):
        msg = f"the variable {variable!r} holds {plain!r}, which is not a number"
        raise UnreadableHeaderError(msg)

    return plain


def _first_and_last_time_bounds(
    handle: Any, time_variable: str
) -> FirstAndLastTimeBounds:
    """
    Read the first and last values of a time variable, and of its bounds

    Parameters
    ----------
    handle
        The open `h5netcdf.File`, which is untyped

    time_variable
        The time variable's name, which the file has

    Returns
    -------
    :
        The values, as stored

    Raises
    ------
    UnreadableHeaderError
        The time variable is not one-dimensional or has no values, or its bounds
        variable is missing or is not one pair of values per time step.
    """
    variables = handle.variables
    time = variables[time_variable]
    if len(time.shape) != 1:
        msg = (
            f"the time variable {time_variable!r} should have one dimension and has "
            f"{len(time.shape)}"
        )
        raise UnreadableHeaderError(msg)

    n_steps = int(time.shape[0])
    if n_steps == 0:
        msg = f"the time variable {time_variable!r} has no values"
        raise UnreadableHeaderError(msg)

    first_value = _number(time[0], variable=time_variable)
    last_value = _number(time[n_steps - 1], variable=time_variable)

    bounds_name = _plain_python(time.attrs.get("bounds"))
    if bounds_name is None:
        return FirstAndLastTimeBounds(
            time_variable=time_variable,
            n_steps=n_steps,
            first_value=first_value,
            last_value=last_value,
            bounds_variable=None,
            first_bounds=None,
            last_bounds=None,
        )

    if str(bounds_name) not in variables:
        msg = (
            f"the time variable {time_variable!r} names {bounds_name!r} as its "
            "bounds, and the file has no variable of that name"
        )
        raise UnreadableHeaderError(msg)

    bounds = variables[str(bounds_name)]
    if tuple(bounds.shape) != (n_steps, 2):
        msg = (
            f"the bounds variable {bounds_name!r} should hold two values per step of "
            f"{time_variable!r}, and has shape {tuple(bounds.shape)}"
        )
        raise UnreadableHeaderError(msg)

    first_pair = bounds[0]
    last_pair = bounds[n_steps - 1]

    return FirstAndLastTimeBounds(
        time_variable=time_variable,
        n_steps=n_steps,
        first_value=first_value,
        last_value=last_value,
        bounds_variable=str(bounds_name),
        first_bounds=(
            _number(first_pair[0], variable=str(bounds_name)),
            _number(first_pair[1], variable=str(bounds_name)),
        ),
        last_bounds=(
            _number(last_pair[0], variable=str(bounds_name)),
            _number(last_pair[1], variable=str(bounds_name)),
        ),
    )


def read_hdf5_file_header(
    source: ByteSource, *, time_variable: str | None = None
) -> FileHeader:
    """
    Read a netCDF-4/HDF5 file's header, and optionally its first and last times

    Parameters
    ----------
    source
        The file's bytes

        Its `size` has to be the length of the **whole** file: HDF5 compares it with
        the end-of-file address in the superblock and refuses to open a file where
        they disagree. On a sampled CMIP6 file the attribute names were readable
        within 1.9 kB, but the values, global and variable alike, needed 42.3 kB.

    time_variable
        The time variable to read the first and last values and bounds of, or `None`
        not to read any values

    Returns
    -------
    :
        Every global attribute and every variable's attributes, with `numpy` types
        reduced to plain Python ones, and the time values if asked for and the file
        has a variable of that name

        Nothing is renamed or reinterpreted: what an attribute means is a question
        about the project that wrote the file, not about the format.

    Raises
    ------
    HeaderReaderUnavailableError
        `h5netcdf` is not installed, so netCDF-4 files cannot be read at all.

    TruncatedHeaderError
        HDF5 needed bytes beyond what `source` may give. A bigger budget is the fix;
        another mirror will not help, because every copy of the file has this layout.

    UnreadableHeaderError
        HDF5 would not open these bytes or read what was asked of them, or the time
        values are not laid out as a time coordinate's should be.
    """
    try:
        import h5netcdf  # noqa: PLC0415 - the optional `headers` extra, imported on use
    except ImportError as error:
        raise HeaderReaderUnavailableError(HDF5_MAGIC) from error

    try:
        with h5netcdf.File(_SourceFile(source), "r") as handle:
            global_attributes = {
                str(name): _plain_python(value) for name, value in handle.attrs.items()
            }
            variable_attributes = {
                str(name): {
                    str(attribute): _plain_python(value)
                    for attribute, value in variable.attrs.items()
                }
                for name, variable in handle.variables.items()
            }

            time_bounds = None
            if time_variable is not None and time_variable in handle.variables:
                time_bounds = _first_and_last_time_bounds(handle, time_variable)
    except (HeaderReadError, HeaderParseError):
        # The source's own errors, and ours, travel out of HDF5 as themselves.
        # `HeaderReadError` is an `OSError`, so this has to come first.
        raise
    except (OSError, ValueError, KeyError) as error:
        # HDF5 reports everything from "this is not an HDF5 file" to a mangled
        # fractal heap as an `OSError`, and `h5netcdf` reports an HDF5 file that is
        # not netCDF-4 as a `ValueError`. They all mean the same thing to us: these
        # bytes did not yield a header.
        msg = f"h5netcdf could not read a header out of these bytes: {error}"
        raise UnreadableHeaderError(msg) from error

    return FileHeader(
        layout=HeaderLayout.NETCDF4_HDF5,
        global_attributes=global_attributes,
        variable_attributes=variable_attributes,
        time_bounds=time_bounds,
        file_size=source.size,
        bytes_read=source.bytes_read,
        n_requests=source.n_requests,
    )
