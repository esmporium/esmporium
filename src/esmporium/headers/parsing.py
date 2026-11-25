r"""
Working out what a file is, and reading it accordingly
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from esmporium.headers.contents import DEFAULT_TIME_VARIABLE, HeaderLayout
from esmporium.headers.errors import (
    MissingVariableAttributesError,
    NoSuchVariableError,
    UnsupportedHeaderFormatError,
)
from esmporium.headers.hdf5 import HDF5_MAGIC, read_hdf5_file_header
from esmporium.headers.netcdf3 import (
    NETCDF3_MAGIC_CLASSIC,
    NETCDF3_MAGICS,
    read_netcdf3_file_header,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

    from esmporium.headers.contents import FileHeader, FirstAndLastTimeBounds
    from esmporium.headers.sources import ByteSource

MAGIC_BYTES_NEEDED = max(len(HDF5_MAGIC), *(len(each) for each in NETCDF3_MAGICS))
"""
How many of a file's first bytes it takes to recognise its layout

The longest magic number this recognises, so an unrecognised file start is also
reported in full rather than cut off part way through.
"""


class HeaderParser(Protocol):
    """
    Something which reads a file's header out of a byte source

    One per [HeaderLayout][esmporium.headers.HeaderLayout], collected in
    [PARSERS_BY_LAYOUT][(m).PARSERS_BY_LAYOUT].

    Takes a source and nothing else -- no URL, no route, no client. A parser cannot
    tell where its bytes come from, which is what makes every one of them testable
    from bytes in hand, and what keeps "this node is broken" and "these bytes are
    wrong" from being decided in the same place.
    """

    def __call__(
        self, source: ByteSource, *, time_variable: str | None = None
    ) -> FileHeader:
        """
        Read a file's header, and optionally its first and last time values

        Parameters
        ----------
        source
            The file's bytes

        time_variable
            The time variable to read the first and last values and bounds of, or
            `None` not to read any values

        Returns
        -------
        :
            Everything the header declares, and the time values if asked for and the
            file has that variable

        Raises
        ------
        esmporium.headers.TruncatedHeaderError
            The bytes needed are beyond what `source` may give.

        esmporium.headers.UnreadableHeaderError
            The bytes claim this layout and do not parse as it.
        """


PARSERS_BY_LAYOUT: Mapping[HeaderLayout, HeaderParser] = {
    HeaderLayout.NETCDF3: read_netcdf3_file_header,
    HeaderLayout.NETCDF4_HDF5: read_hdf5_file_header,
}
"""
Which [HeaderParser][(m).HeaderParser] reads each layout

Exhaustive by construction: [sniff_layout][(m).sniff_layout] can only return a layout
that is a key here, because recognising a layout and reading it are the same list.
"""


def sniff_layout(start: bytes) -> HeaderLayout:
    r"""
    Work out a file's on-disk layout from its first bytes

    Parameters
    ----------
    start
        The first bytes of the file, starting at byte zero

        Only the first few are looked at -- at most
        [MAGIC_BYTES_NEEDED][(m).MAGIC_BYTES_NEEDED], the longest magic number here.

    Returns
    -------
    :
        Which layout the bytes announce

    Raises
    ------
    UnsupportedHeaderFormatError
        The bytes announce something this package cannot read. That includes `CDF\x05`
        (CDF-5), which is classic netCDF with wider element counts -- recognising it
        as classic would not fail, it would quietly return nonsense, which is worse.

    Examples
    --------
    >>> sniff_layout(b"CDF\x01and then the rest of the header")
    <HeaderLayout.NETCDF3: 'netcdf3'>

    >>> sniff_layout(b"\x89HDF\r\n\x1a\nand then the superblock")
    <HeaderLayout.NETCDF4_HDF5: 'netcdf4_hdf5'>

    Anything else is refused rather than guessed at:

    >>> try:
    ...     sniff_layout(b"GRIB\x00\x00\x00\x02")
    ... except UnsupportedHeaderFormatError as error:
    ...     error.magic
    b'GRIB\x00\x00\x00\x02'
    """
    if start[: len(HDF5_MAGIC)] == HDF5_MAGIC:
        return HeaderLayout.NETCDF4_HDF5

    # Both classic magics are the same four bytes long, so either one's length
    # will do to slice what the membership test needs.
    if start[: len(NETCDF3_MAGIC_CLASSIC)] in NETCDF3_MAGICS:
        return HeaderLayout.NETCDF3

    raise UnsupportedHeaderFormatError(start[:MAGIC_BYTES_NEEDED])


def read_file_header(
    source: ByteSource,
    *,
    time_bounds: bool = False,
    time_variable: str = DEFAULT_TIME_VARIABLE,
) -> FileHeader:
    r"""
    Read everything a file's header says, whatever layout it is in

    The entry point to this package's reading half: sniff the layout, then hand the
    source to the parser for it. One read gives every global attribute and every
    variable's attributes, because they sit together at the start of the file, so
    whatever is wanted from a header later does not need another read.

    Parameters
    ----------
    source
        The file's bytes

    time_bounds
        Whether to read the first and last values and bounds of `time_variable` too

        These are stored apart from the header, so reading them costs a few more
        small reads of the file. Off unless asked for.

    time_variable
        Which variable is the time coordinate

    Returns
    -------
    :
        Everything the header declares, and the time values if asked for

        Names and values are passed through untouched. Deciding that
        `parent_experiment_rip` is what CMIP5 calls a variant label is a question
        about CMIP5, not about netCDF, and is answered in
        [esmporium.db.parent_headers][].

        [time_bounds][esmporium.headers.FileHeader.time_bounds] is `None` when the file
        has no `time_variable`, as a file of fixed fields does not.
        [read_first_and_last_time_bounds][(m).read_first_and_last_time_bounds] raises
        instead.

    Raises
    ------
    UnsupportedHeaderFormatError
        The bytes are not a layout this package reads, or they are netCDF-4 and the
        optional `headers` extra is not installed (a
        [HeaderParserUnavailableError][esmporium.headers.HeaderParserUnavailableError],
        which is one of these).

    esmporium.headers.TruncatedHeaderError
        The bytes needed are beyond what `source` may give.

    esmporium.headers.UnreadableHeaderError
        The bytes announce a layout and then do not parse as it.

    Examples
    --------
    A classic netCDF file is recognised and read with nothing installed:

    >>> from esmporium.headers import HeldBytesSource
    >>> header = (
    ...     b"CDF\x01"
    ...     + b"\x00\x00\x00\x00"  # numrecs
    ...     + b"\x00\x00\x00\x00" * 2  # no dimensions
    ...     + b"\x00\x00\x00\x0c"  # NC_ATTRIBUTE
    ...     + b"\x00\x00\x00\x01"  # ... carrying one entry
    ...     + b"\x00\x00\x00\x14"  # a 20-character name, so no padding
    ...     + b"parent_experiment_id"
    ...     + b"\x00\x00\x00\x02"  # NC_CHAR
    ...     + b"\x00\x00\x00\x09"  # nine characters
    ...     + b"piControl\x00\x00\x00"  # ... padded to twelve bytes
    ...     + b"\x00\x00\x00\x00" * 2  # no variables
    ... )
    >>> read = read_file_header(HeldBytesSource(header, size=len(header)))
    >>> read.layout, dict(read.global_attributes)
    (<HeaderLayout.NETCDF3: 'netcdf3'>, {'parent_experiment_id': 'piControl'})
    """
    start = source.read(0, min(MAGIC_BYTES_NEEDED, source.size))
    parser = PARSERS_BY_LAYOUT[sniff_layout(start)]

    return parser(source, time_variable=time_variable if time_bounds else None)


def read_global_attributes(source: ByteSource) -> Mapping[str, object]:
    """
    Read every global attribute of a file

    Parameters
    ----------
    source
        The file's bytes

    Returns
    -------
    :
        Every global attribute, exactly as the file stores them
    """
    return read_file_header(source).global_attributes


def read_variable_attributes(
    source: ByteSource, name: str, *, wanted: Collection[str] = ()
) -> Mapping[str, object]:
    """
    Read one variable's attributes

    Parameters
    ----------
    source
        The file's bytes

    name
        The variable, e.g. `"time"`

    wanted
        Attributes the variable has to carry, e.g. `("units", "calendar")`

        Any that are missing are an error which lists every attribute the variable
        does carry, so a file which says something unexpected can be seen for what it
        is rather than guessed about.

    Returns
    -------
    :
        Every attribute of the variable, exactly as the file stores them

    Raises
    ------
    NoSuchVariableError
        The file has no variable called `name`. The error lists the ones it has.

    MissingVariableAttributesError
        The variable lacks some of `wanted`. The error lists what it does carry.
    """
    variables = read_file_header(source).variable_attributes
    if name not in variables:
        raise NoSuchVariableError(name, found=tuple(variables))

    attributes = variables[name]
    missing = tuple(each for each in wanted if each not in attributes)
    if missing:
        raise MissingVariableAttributesError(name, missing=missing, found=attributes)

    return attributes


def read_first_and_last_time_bounds(
    source: ByteSource, *, time_variable: str = DEFAULT_TIME_VARIABLE
) -> FirstAndLastTimeBounds:
    """
    Read the first and last values of a file's time coordinate, and of its bounds

    Only these, never every value: the first lower bound is where the file's data
    starts and the last upper bound is where it ends, which is what checking a
    file's extent needs.

    Parameters
    ----------
    source
        The file's bytes

    time_variable
        Which variable is the time coordinate

    Returns
    -------
    :
        The values, exactly as stored. Its bounds are `None` when the time variable
        names no `bounds`, as is usual for data reported at instants.

    Raises
    ------
    NoSuchVariableError
        The file has no `time_variable`. The error lists the variables it has.

    esmporium.headers.UnreadableHeaderError
        The time variable has no values or is not one-dimensional, or its bounds are
        not one pair of values per step.
    """
    header = read_file_header(source, time_bounds=True, time_variable=time_variable)
    if header.time_bounds is None:
        raise NoSuchVariableError(
            time_variable, found=tuple(header.variable_attributes)
        )

    return header.time_bounds
