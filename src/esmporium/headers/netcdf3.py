r"""
Reading global attributes out of a classic netCDF file's header

"Classic" means the original on-disk format and its 64-bit-offset variant, the ones
whose files begin `CDF\x01` and `CDF\x02`. Nothing is imported to do this: the format
is a short, fully specified, sequentially laid out structure at the very start of the
file, so reading the global attributes out of it is a few hundred bytes of
`struct.unpack` rather than a dependency.

**The two versions are parsed identically here**, which is worth knowing because it
looks as though they should not be. They differ only in the width of a *variable's*
data offset -- four bytes in `CDF\x01`, eight in `CDF\x02` -- and the global
attributes come before the variable list, so this stops reading before the difference
can matter.

The layout, as far as this reads it (the netCDF classic format specification, with the
parts after `gatt_list` left out):

```
header    = magic  numrecs  dim_list  gatt_list  ...
magic     = 'C' 'D' 'F'  version
numrecs   = uint32
dim_list  = ABSENT | NC_DIMENSION  nelems  [dim ...]
gatt_list = ABSENT | NC_ATTRIBUTE  nelems  [attr ...]
ABSENT    = uint32(0)  uint32(0)
dim       = name  uint32
attr      = name  nc_type  nelems  [values ...]
name      = uint32  chars     -- padded to a four-byte boundary
```

Dimensions are read and discarded rather than skipped over, because their lengths are
not needed but their *names* are variable-length, so there is no way to step past them
without parsing them.
"""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING

from esmporium.headers.errors import TruncatedHeaderError, UnreadableHeaderError

if TYPE_CHECKING:
    from collections.abc import Mapping

NETCDF3_MAGIC_CLASSIC = b"CDF\x01"
"""The first four bytes of a classic netCDF file"""

NETCDF3_MAGIC_64BIT_OFFSET = b"CDF\x02"
"""The first four bytes of a 64-bit-offset netCDF file"""

NETCDF3_MAGICS = frozenset({NETCDF3_MAGIC_CLASSIC, NETCDF3_MAGIC_64BIT_OFFSET})
"""
Every classic-netCDF file start this module can read

`CDF\x05` (CDF-5) is deliberately absent. It widens the element counts this parser
reads as four bytes, so treating it as either of the above would not fail -- it would
quietly return nonsense, which is worse. No CMIP file has been seen in it.
"""

_NC_DIMENSION = 0x0A
_NC_ATTRIBUTE = 0x0C

_NC_CHAR = 2

# nc_type -> (struct code, width in bytes). `NC_CHAR` is handled separately, as text.
_NUMERIC_TYPES: dict[int, tuple[str, int]] = {
    1: ("b", 1),  # NC_BYTE, signed
    3: ("h", 2),  # NC_SHORT
    4: ("i", 4),  # NC_INT
    5: ("f", 4),  # NC_FLOAT
    6: ("d", 8),  # NC_DOUBLE
}


class _Reader:
    """
    A cursor over a prefix of a file, which refuses to read past what it was given

    Every read goes through [take][(c).take], so running off the end of a short prefix
    raises [TruncatedHeaderError][esmporium.headers.TruncatedHeaderError] from one
    place rather than being a possible `IndexError` at each of a dozen call sites.
    Slicing `bytes` past its end returns something shorter instead of failing, so
    without this a truncated header would parse as mangled rather than as incomplete,
    and the caller would try another mirror instead of asking for more bytes.
    """

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.at = 0

    def take(self, count: int) -> bytes:
        """
        Read the next `count` bytes

        Parameters
        ----------
        count
            How many bytes to read

        Returns
        -------
        :
            Exactly `count` bytes

        Raises
        ------
        TruncatedHeaderError
            There are fewer than `count` bytes left.
        """
        end = self.at + count
        if end > len(self.data):
            raise TruncatedHeaderError(end, bytes_given=len(self.data))

        chunk = self.data[self.at : end]
        self.at = end

        return chunk

    def uint32(self) -> int:
        """
        Read a four-byte big-endian unsigned integer

        Returns
        -------
        :
            The value read
        """
        (value,) = struct.unpack(">I", self.take(4))

        return int(value)

    def pad_to_four(self) -> None:
        """
        Skip to the next four-byte boundary, if not already on one

        The format pads names and attribute values this way. Measured from the start
        of the file, which is also the start of this buffer, so the absolute position
        is the right thing to round up.
        """
        over = self.at % 4
        if over:
            self.take(4 - over)

    def name(self) -> str:
        """
        Read a padded, length-prefixed name

        Returns
        -------
        :
            The name, decoded as UTF-8
        """
        raw = self.take(self.uint32())
        self.pad_to_four()

        return raw.decode("utf-8", errors="replace")


def _count_of_list(reader: _Reader, expected_tag: int, what: str) -> int:
    """
    Read a `dim_list` or `gatt_list` header and return how many entries it has

    Parameters
    ----------
    reader
        The cursor to read from

    expected_tag
        The tag this list must carry, e.g. `NC_ATTRIBUTE`

    what
        What the list holds, for the error message

    Returns
    -------
    :
        How many entries follow, which is zero for an absent list

    Raises
    ------
    UnreadableHeaderError
        The list carries neither the expected tag nor the absent marker.
    """
    tag = reader.uint32()
    count = reader.uint32()

    # `ABSENT` is two zero words, so a zero tag with a zero count is an empty list
    # rather than a corrupt one. A zero tag with a non-zero count is corrupt.
    if tag == 0 and count == 0:
        return 0

    if tag != expected_tag:
        msg = (
            f"the {what} list should start with the tag {expected_tag:#04x} or be "
            f"absent (two zero words), and starts {tag:#x} with {count} entries"
        )
        raise UnreadableHeaderError(msg)

    return count


def _attribute_value(reader: _Reader, nc_type: int, element_count: int) -> object:
    """
    Read one attribute's value

    Parameters
    ----------
    reader
        The cursor to read from

    nc_type
        The attribute's netCDF type code

    element_count
        How many elements of that type the value has

    Returns
    -------
    :
        A `str` for a character attribute. For a numeric one, the value itself when
        there is a single element and a `tuple` when there are several -- so that a
        scalar does not have to be unwrapped by every caller, which is how a
        one-element tuple ends up being read as a number by accident.

    Raises
    ------
    UnreadableHeaderError
        `nc_type` is not a type the classic format defines.
    """
    if nc_type == _NC_CHAR:
        raw = reader.take(element_count)
        reader.pad_to_four()

        # Trailing NULs are padding within the declared length, not content: the
        # format counts characters, and writers routinely declare a round number.
        return raw.decode("utf-8", errors="replace").rstrip("\x00")

    if nc_type not in _NUMERIC_TYPES:
        msg = (
            f"the attribute has type {nc_type}, which the classic netCDF format does "
            "not define (it defines 1 to 6)"
        )
        raise UnreadableHeaderError(msg)

    code, width = _NUMERIC_TYPES[nc_type]
    values = struct.unpack(
        f">{element_count}{code}", reader.take(element_count * width)
    )
    reader.pad_to_four()

    if element_count == 1:
        return values[0]

    return tuple(values)


def read_netcdf3_global_attributes(prefix: bytes) -> Mapping[str, object]:
    r"""
    Read every global attribute from the start of a classic netCDF file

    Parameters
    ----------
    prefix
        The first bytes of the file, starting at byte zero

        Needs to reach the end of the global attribute section and no further. A few
        kilobytes is plenty: across twelve live CMIP5 files the section ended by 6.3 kB.

    Returns
    -------
    :
        Every global attribute, in the order the file declares them

        Values are `str` for character attributes and numbers (or tuples of them) for
        numeric ones, exactly as stored -- nothing is coerced, because what an
        attribute *means* is a question about the project that wrote it and not about
        the format.

    Raises
    ------
    TruncatedHeaderError
        `prefix` ends before the global attributes do. Fetch more and try again.

    UnreadableHeaderError
        `prefix` does not begin like a classic netCDF file, or its header does not
        parse as one.

    Examples
    --------
    A minimal header: no dimensions, and one character attribute. Note the padding of
    both the name and the value out to a four-byte boundary.

    >>> header = (
    ...     b"CDF\x01"
    ...     + b"\x00\x00\x00\x00"  # numrecs
    ...     + b"\x00\x00\x00\x00" * 2  # dim_list: absent
    ...     + b"\x00\x00\x00\x0c"  # gatt_list: NC_ATTRIBUTE
    ...     + b"\x00\x00\x00\x01"  # ... carrying one entry
    ...     + b"\x00\x00\x00\x05"
    ...     + b"title\x00\x00\x00"  # the name, padded to four bytes
    ...     + b"\x00\x00\x00\x02"  # NC_CHAR
    ...     + b"\x00\x00\x00\x02"
    ...     + b"hi\x00\x00"  # two characters, padded to four bytes
    ... )
    >>> dict(read_netcdf3_global_attributes(header))
    {'title': 'hi'}

    Too few bytes is told apart from bad bytes, because the two want different things
    done about them -- fetch more, versus try another mirror. The bound is how far
    this got, not how much it will eventually need:

    >>> try:
    ...     read_netcdf3_global_attributes(header[:10])
    ... except TruncatedHeaderError as error:
    ...     error.bytes_needed
    12
    """
    reader = _Reader(prefix)

    magic = reader.take(4)
    if magic not in NETCDF3_MAGICS:
        msg = (
            f"this is not a classic netCDF file: it starts with {magic!r}, not "
            r"`CDF\x01` or `CDF\x02`"
        )
        raise UnreadableHeaderError(msg)

    # `numrecs`, which this does not need. Read rather than skipped so the cursor
    # stays the single account of where we are.
    reader.uint32()

    for _ in range(_count_of_list(reader, _NC_DIMENSION, "dimension")):
        # The name has to be parsed to get past it, and the length follows it.
        reader.name()
        reader.uint32()

    attributes: dict[str, object] = {}
    for _ in range(_count_of_list(reader, _NC_ATTRIBUTE, "global attribute")):
        name = reader.name()
        nc_type = reader.uint32()
        element_count = reader.uint32()
        attributes[name] = _attribute_value(reader, nc_type, element_count)

    return attributes
