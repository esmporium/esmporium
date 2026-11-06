r"""
Working out what a file is, and reading its global attributes accordingly

A route's [ContainerFormat][esmporium.datasets.ContainerFormat] says `netcdf`, and that
is **a routing hint rather than a complete instruction**: `netcdf` is two unrelated
on-disk formats. Classic netCDF (`CDF\x01`, `CDF\x02`) is a sequential structure read
here with no dependencies at all; netCDF-4 (`\x89HDF\r\n\x1a\n`) is HDF5 and needs
`h5py`. ESGF serves both, so the only way to know which a file is, is to look at its
first bytes.

**It is not safe to guess from the project either**, which is the trap worth naming
because the guess looks so reasonable. Sampled live on 2026-11-04: CMIP6 was netCDF-4
in 18 files out of 18, and CMIP5 was classic netCDF in 44 models out of 47 -- but
`EC-EARTH`'s rcp85 output is netCDF-4. So a reader that assumed "CMIP5 means classic"
would work for most of an archive and then fail, and the thing it failed on would look
like a corrupt file rather than a mis-identified one.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Protocol

from esmporium.headers.errors import UnsupportedHeaderFormatError
from esmporium.headers.hdf5 import HDF5_MAGIC, read_hdf5_global_attributes
from esmporium.headers.netcdf3 import (
    NETCDF3_MAGIC_CLASSIC,
    NETCDF3_MAGICS,
    read_netcdf3_global_attributes,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

MAGIC_BYTES_NEEDED = max(len(HDF5_MAGIC), *(len(each) for each in NETCDF3_MAGICS))
"""
How many of a file's first bytes to quote when its layout is not one we read

The longest magic number this recognises, so an unrecognised file start is reported in
full rather than cut off part way through. Derived rather than stated, so adding a
layout cannot leave it behind.
"""


class HeaderLayout(str, Enum):
    """
    The on-disk layout of a file we can read a header out of

    `str`-valued for the same reason as every other enum stored or logged here: a
    record of one reads as its name rather than an integer nobody can interpret.

    Deliberately *not* the same thing as
    [ContainerFormat][esmporium.datasets.ContainerFormat]. That says what a route
    claims to serve, which is what the search index published; this says what the
    bytes turned out to be. The two can disagree, and when they do it is this one that
    is right.
    """

    NETCDF3 = "netcdf3"
    r"""
    Classic netCDF or its 64-bit-offset variant: `CDF\x01` or `CDF\x02`

    Read by [esmporium.headers.netcdf3][], which needs nothing installed.
    """

    NETCDF4_HDF5 = "netcdf4_hdf5"
    r"""
    netCDF-4, which is HDF5: `\x89HDF\r\n\x1a\n`

    Read by [esmporium.headers.hdf5][], which needs the optional `headers` extra.
    """


class HeaderParser(Protocol):
    """
    Something which reads global attributes out of the start of a file

    One per [HeaderLayout][(m).HeaderLayout], collected in
    [PARSERS_BY_LAYOUT][(m).PARSERS_BY_LAYOUT].

    Takes bytes and nothing else -- no URL, no route, no client. A parser cannot reach
    the network and is not told where its bytes came from, which is what makes every
    one of them testable from a file on disk, and what keeps "this node is broken"
    and "these bytes are wrong" from being decided in the same place.
    """

    def __call__(self, prefix: bytes, *, file_size: int) -> Mapping[str, object]:
        """
        Read every global attribute from the start of a file

        Parameters
        ----------
        prefix
            The first bytes of the file, starting at byte zero

        file_size
            How long the whole file is

            Needed by HDF5, which checks it against the superblock and refuses to
            open a file where the two disagree. Accepted by every parser so they are
            interchangeable, and ignored by those that do not need it.

        Returns
        -------
        :
            Every global attribute, with values as the file stores them

        Raises
        ------
        esmporium.headers.TruncatedHeaderError
            `prefix` is too short. Fetch more; another mirror will not help.

        esmporium.headers.UnreadableHeaderError
            The bytes claim this layout and do not parse as it.
        """


def _parse_netcdf3(prefix: bytes, *, file_size: int) -> Mapping[str, object]:
    """
    Read a classic netCDF header, ignoring the file's total length

    An adapter, so that [read_netcdf3_global_attributes][esmporium.headers.read_netcdf3_global_attributes]
    does not have to take an argument it has no use for. The classic header is
    self-describing from byte zero and says nothing about where the file ends, so
    unlike HDF5 there is nothing to check a total length against.

    Parameters
    ----------
    prefix
        The first bytes of the file

    file_size
        How long the whole file is. Ignored.

    Returns
    -------
    :
        Every global attribute
    """  # noqa: E501
    return read_netcdf3_global_attributes(prefix)


PARSERS_BY_LAYOUT: Mapping[HeaderLayout, HeaderParser] = {
    HeaderLayout.NETCDF3: _parse_netcdf3,
    HeaderLayout.NETCDF4_HDF5: read_hdf5_global_attributes,
}
"""
Which [HeaderParser][(m).HeaderParser] reads each [HeaderLayout][(m).HeaderLayout]

Exhaustive by construction: [sniff_layout][(m).sniff_layout] can only return a layout
that is a key here, because recognising a layout and reading it are the same list.
"""


def sniff_layout(prefix: bytes) -> HeaderLayout:
    r"""
    Work out a file's on-disk layout from its first bytes

    Parameters
    ----------
    prefix
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
    if prefix[: len(HDF5_MAGIC)] == HDF5_MAGIC:
        return HeaderLayout.NETCDF4_HDF5

    # Both classic magics are the same four bytes long, so either one's length
    # will do to slice what the membership test needs.
    if prefix[: len(NETCDF3_MAGIC_CLASSIC)] in NETCDF3_MAGICS:
        return HeaderLayout.NETCDF3

    raise UnsupportedHeaderFormatError(prefix[:MAGIC_BYTES_NEEDED])


def read_global_attributes(prefix: bytes, *, file_size: int) -> Mapping[str, object]:
    r"""
    Read every global attribute from the start of a file, whatever layout it is in

    The entry point to this package's reading half: sniff the layout, then hand the
    bytes to the parser for it.

    Parameters
    ----------
    prefix
        The first bytes of the file, starting at byte zero

    file_size
        How long the whole file is

        Best taken from the `Content-Range` of the response that fetched `prefix`,
        rather than from the search index: the index and the served file are known to
        disagree, and HDF5 refuses to open a file whose apparent length contradicts
        its superblock.

    Returns
    -------
    :
        Every global attribute, with values as the file stores them

        Names and values are passed through untouched. Deciding that
        `parent_experiment_rip` is what CMIP5 calls a variant label is a question
        about CMIP5, not about netCDF, and is answered in
        [esmporium.db.parent_headers][].

    Raises
    ------
    UnsupportedHeaderFormatError
        The bytes are not a layout this package reads, or they are netCDF-4 and the
        optional `headers` extra is not installed (a
        [HeaderParserUnavailableError][esmporium.headers.HeaderParserUnavailableError],
        which is one of these).

    esmporium.headers.TruncatedHeaderError
        `prefix` is too short for this layout. Fetch more and try again.

    esmporium.headers.UnreadableHeaderError
        The bytes announce a layout and then do not parse as it.

    Examples
    --------
    A classic netCDF file is recognised and read with nothing installed:

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
    ... )
    >>> dict(read_global_attributes(header, file_size=len(header)))
    {'parent_experiment_id': 'piControl'}
    """
    return PARSERS_BY_LAYOUT[sniff_layout(prefix)](prefix, file_size=file_size)
