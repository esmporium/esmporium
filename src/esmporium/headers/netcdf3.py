r"""
Reading a classic netCDF file's header, and single values out of its data

"Classic" means the original on-disk format and its 64-bit-offset variant, the ones
whose files begin `CDF\x01` and `CDF\x02`. Nothing is imported to do this: the format
is a short, fully specified, sequentially laid out header at the start of the file,
followed by data whose position is arithmetic on numbers the header gives.

Written from the format specification alone: the grammar in the netCDF Users Guide,
[Appendix B, File Format Specifications](https://docs.unidata.ucar.edu/nug/current/file_format_specifications.html),
also published as the community standard
[ESDS-RFC-011](https://www.earthdata.nasa.gov/s3fs-public/imported/ESDS-RFC-011v2.00.pdf).
Each function names the grammar rule it implements, so it can be checked against the
specification and nothing else. The rules used are:

```text
netcdf_file  = header data
header       = magic numrecs dim_list gatt_list var_list
magic        = 'C' 'D' 'F' VERSION
VERSION      = \x01 | \x02
numrecs      = NON_NEG | STREAMING
dim_list     = ABSENT | NC_DIMENSION nelems [dim ...]
gatt_list    = att_list
att_list     = ABSENT | NC_ATTRIBUTE nelems [attr ...]
var_list     = ABSENT | NC_VARIABLE nelems [var ...]
ABSENT       = ZERO ZERO
dim          = name dim_length
dim_length   = NON_NEG            ; zero for the record dimension
name         = nelems namestring  ; namestring padded to a four-byte boundary
attr         = name nc_type nelems [values ...]
var          = name nelems [dimid ...] vatt_list nc_type vsize begin
vatt_list    = att_list
begin        = OFFSET             ; INT for VERSION 1, INT64 for VERSION 2
data         = non_recs recs
recs         = [record ...]
record       = [varslab ...]      ; one per record variable, in var_list order
values       = bytes | chars | shorts | ints | floats | doubles  ; padded to four
```

Every number in the header, and every value, is big-endian.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from math import prod
from typing import TYPE_CHECKING

from esmporium.headers.contents import (
    FileHeader,
    FirstAndLastTimeBounds,
    HeaderLayout,
)
from esmporium.headers.errors import UnreadableHeaderError

if TYPE_CHECKING:
    from esmporium.headers.sources import ByteSource

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
_NC_VARIABLE = 0x0B
_NC_ATTRIBUTE = 0x0C

_STREAMING = 0xFFFFFFFF
"""`numrecs` when the writer did not record how many records there are"""

_NC_CHAR = 2

# nc_type -> (struct code, width in bytes). `NC_CHAR` is read as text, one byte wide.
_NUMERIC_TYPES: dict[int, tuple[str, int]] = {
    1: ("b", 1),  # NC_BYTE, signed
    3: ("h", 2),  # NC_SHORT
    4: ("i", 4),  # NC_INT
    5: ("f", 4),  # NC_FLOAT
    6: ("d", 8),  # NC_DOUBLE
}


@dataclass(frozen=True)
class _Dimension:
    """One `dim`: a name and a length, which is zero for the record dimension"""

    name: str
    length: int

    @property
    def is_record(self) -> bool:
        """
        Whether this is the record dimension

        Returns
        -------
        :
            `True` if its declared length is zero
        """
        return self.length == 0


@dataclass(frozen=True)
class _Variable:
    """One `var`, less `vsize`, which is not relied on"""

    name: str
    dimension_ids: tuple[int, ...]
    attributes: dict[str, object]
    nc_type: int
    begin: int


@dataclass(frozen=True)
class _Header:
    """Everything `header` declares"""

    numrecs: int | None
    """`None` when the file says `STREAMING`"""

    dimensions: tuple[_Dimension, ...]
    global_attributes: dict[str, object]
    variables: dict[str, _Variable]


class _Reader:
    """
    A cursor over a file's bytes, for reading its header in order
    """

    def __init__(self, source: ByteSource) -> None:
        self.source = source
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
        """
        chunk = self.source.read(self.at, count)
        self.at += count

        return chunk

    def uint32(self) -> int:
        """
        Read a four-byte big-endian unsigned integer, which is how `NON_NEG` is stored

        Returns
        -------
        :
            The value read
        """
        (value,) = struct.unpack(">I", self.take(4))

        return int(value)

    def uint64(self) -> int:
        """
        Read an eight-byte big-endian unsigned integer, which is `begin` in VERSION 2

        Returns
        -------
        :
            The value read
        """
        (value,) = struct.unpack(">Q", self.take(8))

        return int(value)

    def pad_to_four(self) -> None:
        """
        Skip `padding`: to the next four-byte boundary, if not already on one

        The header starts at byte zero, so the absolute position is the right thing
        to round up.
        """
        over = self.at % 4
        if over:
            self.take(4 - over)

    def name(self) -> str:
        """
        Read a `name`: `nelems namestring`, padded to four bytes

        Returns
        -------
        :
            The name, decoded as UTF-8
        """
        raw = self.take(self.uint32())
        self.pad_to_four()

        return raw.decode("utf-8", errors="replace")


def _list_length(reader: _Reader, expected_tag: int, what: str) -> int:
    """
    Read the start of a `dim_list`, `att_list` or `var_list`

    Each is `ABSENT | tag nelems [entry ...]`, where `ABSENT` is two zero words.

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
        The list carries neither the expected tag nor `ABSENT`.
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


def check_nc_type(nc_type: int) -> None:
    """
    Check that an `nc_type` is one the classic format defines

    Parameters
    ----------
    nc_type
        The type code

    Raises
    ------
    UnreadableHeaderError
        It is not: the classic format defines 1 to 6.
    """
    if nc_type != _NC_CHAR and nc_type not in _NUMERIC_TYPES:
        msg = (
            f"the type code {nc_type} is not one the classic netCDF format defines "
            "(it defines 1 to 6)"
        )
        raise UnreadableHeaderError(msg)


def _element_width(nc_type: int) -> int:
    """
    How many bytes one value of an `nc_type` takes

    Parameters
    ----------
    nc_type
        The type code

    Returns
    -------
    :
        The width in bytes
    """
    check_nc_type(nc_type)
    if nc_type == _NC_CHAR:
        return 1

    return _NUMERIC_TYPES[nc_type][1]


def _attribute_value(reader: _Reader, nc_type: int, element_count: int) -> object:
    """
    Read one attribute's `[values ...]`, and the padding after them

    Parameters
    ----------
    reader
        The cursor to read from

    nc_type
        The attribute's `nc_type`

    element_count
        Its `nelems`

    Returns
    -------
    :
        A `str` for a character attribute. For a numeric one, the value itself when
        there is a single element and a `tuple` when there are several -- so that a
        scalar does not have to be unwrapped by every caller.

    Raises
    ------
    UnreadableHeaderError
        `nc_type` is not a type the classic format defines.
    """
    width = _element_width(nc_type)
    raw = reader.take(element_count * width)
    reader.pad_to_four()

    if nc_type == _NC_CHAR:
        # Trailing NULs are padding within the declared length, not content: the
        # format counts characters, and writers routinely declare a round number.
        return raw.decode("utf-8", errors="replace").rstrip("\x00")

    code, _ = _NUMERIC_TYPES[nc_type]
    values = struct.unpack(f">{element_count}{code}", raw)

    if element_count == 1:
        return values[0]

    return tuple(values)


def _att_list(reader: _Reader, what: str) -> dict[str, object]:
    """
    Read an `att_list`: `ABSENT | NC_ATTRIBUTE nelems [attr ...]`

    Each `attr` is `name nc_type nelems [values ...]`.

    Parameters
    ----------
    reader
        The cursor to read from

    what
        Whose attributes these are, for the error message

    Returns
    -------
    :
        The attributes, in the order the file declares them
    """
    attributes: dict[str, object] = {}
    for _ in range(_list_length(reader, _NC_ATTRIBUTE, what)):
        name = reader.name()
        nc_type = reader.uint32()
        element_count = reader.uint32()
        attributes[name] = _attribute_value(reader, nc_type, element_count)

    return attributes


def _read_header(source: ByteSource) -> _Header:
    """
    Read `header`: `magic numrecs dim_list gatt_list var_list`

    Parameters
    ----------
    source
        The file's bytes

    Returns
    -------
    :
        Everything the header declares

    Raises
    ------
    UnreadableHeaderError
        The file does not begin like a classic netCDF file, or its header does not
        parse as one.
    """
    reader = _Reader(source)

    magic = reader.take(4)
    if magic not in NETCDF3_MAGICS:
        msg = (
            f"this is not a classic netCDF file: it starts with {magic!r}, not "
            r"`CDF\x01` or `CDF\x02`"
        )
        raise UnreadableHeaderError(msg)

    raw_numrecs = reader.uint32()
    numrecs = None if raw_numrecs == _STREAMING else raw_numrecs

    # dim = name dim_length
    dimensions = tuple(
        _Dimension(name=reader.name(), length=reader.uint32())
        for _ in range(_list_length(reader, _NC_DIMENSION, "dimension"))
    )

    global_attributes = _att_list(reader, "global attribute")

    # var = name nelems [dimid ...] vatt_list nc_type vsize begin
    variables: dict[str, _Variable] = {}
    for _ in range(_list_length(reader, _NC_VARIABLE, "variable")):
        name = reader.name()
        dimension_ids = tuple(reader.uint32() for _ in range(reader.uint32()))
        for dimension_id in dimension_ids:
            if dimension_id >= len(dimensions):
                msg = (
                    f"the variable {name!r} names dimension {dimension_id}, and the "
                    f"file declares {len(dimensions)}"
                )
                raise UnreadableHeaderError(msg)

        attributes = _att_list(reader, f"{name!r} attribute")
        nc_type = reader.uint32()
        check_nc_type(nc_type)
        # `vsize` is read to get past it and not relied on: the specification makes
        # it redundant with the shape and type, which is where sizes are computed
        # from here, and caps it for variables too large for four bytes.
        reader.uint32()
        begin = reader.uint32() if magic == NETCDF3_MAGIC_CLASSIC else reader.uint64()

        variables[name] = _Variable(
            name=name,
            dimension_ids=dimension_ids,
            attributes=attributes,
            nc_type=nc_type,
            begin=begin,
        )

    return _Header(
        numrecs=numrecs,
        dimensions=dimensions,
        global_attributes=global_attributes,
        variables=variables,
    )


def _is_record_variable(header: _Header, variable: _Variable) -> bool:
    """
    Whether a variable's first dimension is the record dimension

    Parameters
    ----------
    header
        The header declaring it

    variable
        The variable

    Returns
    -------
    :
        `True` if its values are spread across records
    """
    return bool(variable.dimension_ids) and (
        header.dimensions[variable.dimension_ids[0]].is_record
    )


def _slab_bytes(header: _Header, variable: _Variable) -> int:
    """
    How many bytes of values a variable has, per record for a record variable

    The product of its non-record dimension lengths and its element width, unpadded.

    Parameters
    ----------
    header
        The header declaring it

    variable
        The variable

    Returns
    -------
    :
        The size in bytes
    """
    fixed = [
        header.dimensions[dimension_id].length
        for dimension_id in variable.dimension_ids
        if not header.dimensions[dimension_id].is_record
    ]

    return prod(fixed) * _element_width(variable.nc_type)


def _record_bytes(header: _Header) -> int:
    """
    How long one `record` is: one `varslab` per record variable

    Each `varslab` is padded to a four-byte boundary, except where the file has
    exactly one record variable: the specification drops record padding in that case.

    Parameters
    ----------
    header
        The header

    Returns
    -------
    :
        The size of one record in bytes
    """
    slabs = [
        _slab_bytes(header, variable)
        for variable in header.variables.values()
        if _is_record_variable(header, variable)
    ]
    if len(slabs) == 1:
        return slabs[0]

    return sum(slab + (-slab % 4) for slab in slabs)


def _number_of_records(header: _Header, *, file_size: int) -> int:
    """
    How many records the file has

    `numrecs`, unless the file says `STREAMING`, in which case it is however many
    whole records fit between the first record and the end of the file.

    Parameters
    ----------
    header
        The header

    file_size
        How long the whole file is

    Returns
    -------
    :
        The number of records
    """
    if header.numrecs is not None:
        return header.numrecs

    record_variables = [
        variable
        for variable in header.variables.values()
        if _is_record_variable(header, variable)
    ]
    record_bytes = _record_bytes(header)
    if not record_variables or record_bytes == 0:
        return 0

    first_record = min(variable.begin for variable in record_variables)

    return max(0, file_size - first_record) // record_bytes


def _value_at(
    source: ByteSource,
    header: _Header,
    variable: _Variable,
    *,
    record: int,
    within: int,
) -> float | int:
    """
    Read one numeric value of a variable

    A record variable's record `r` starts at `begin + r * (record size)`, because
    every `record` repeats the same `varslab`s in the same order. A non-record
    variable is one `vardata` at `begin`, with `record` counting along its first
    dimension.

    Parameters
    ----------
    source
        The file's bytes

    header
        The header declaring the variable

    variable
        The variable to read from

    record
        The index along the variable's first dimension

    within
        The index of the value within that, counting the remaining dimensions in
        order, last fastest

    Returns
    -------
    :
        The value

    Raises
    ------
    UnreadableHeaderError
        The variable holds characters rather than numbers.
    """
    if variable.nc_type == _NC_CHAR:
        msg = f"the variable {variable.name!r} holds characters, not numbers"
        raise UnreadableHeaderError(msg)

    code, width = _NUMERIC_TYPES[variable.nc_type]
    if _is_record_variable(header, variable):
        step = _record_bytes(header)
    else:
        step = _slab_bytes_after_first(header, variable)

    offset = variable.begin + record * step + within * width

    (value,) = struct.unpack(f">{code}", source.read(offset, width))

    return value  # type: ignore[no-any-return]


def _slab_bytes_after_first(header: _Header, variable: _Variable) -> int:
    """
    How many bytes one step along a non-record variable's first dimension covers

    Parameters
    ----------
    header
        The header declaring it

    variable
        The variable

    Returns
    -------
    :
        The size in bytes
    """
    rest = [header.dimensions[each].length for each in variable.dimension_ids[1:]]

    return prod(rest) * _element_width(variable.nc_type)


def _first_dimension_length(
    header: _Header, variable: _Variable, *, file_size: int
) -> int:
    """
    How long a variable's first dimension is, records included

    Parameters
    ----------
    header
        The header declaring it

    variable
        The variable

    file_size
        How long the whole file is

    Returns
    -------
    :
        The length
    """
    if _is_record_variable(header, variable):
        return _number_of_records(header, file_size=file_size)

    return header.dimensions[variable.dimension_ids[0]].length


def _first_and_last_time_bounds(
    source: ByteSource, header: _Header, time: _Variable
) -> FirstAndLastTimeBounds:
    """
    Read the first and last values of a time variable, and of its bounds

    Parameters
    ----------
    source
        The file's bytes

    header
        The header

    time
        The time variable

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
    if len(time.dimension_ids) != 1:
        msg = (
            f"the time variable {time.name!r} should have one dimension and has "
            f"{len(time.dimension_ids)}"
        )
        raise UnreadableHeaderError(msg)

    n_steps = _first_dimension_length(header, time, file_size=source.size)
    if n_steps == 0:
        msg = f"the time variable {time.name!r} has no values"
        raise UnreadableHeaderError(msg)

    first_value = _value_at(source, header, time, record=0, within=0)
    last_value = _value_at(source, header, time, record=n_steps - 1, within=0)

    bounds_name = time.attributes.get("bounds")
    if bounds_name is None:
        return FirstAndLastTimeBounds(
            time_variable=time.name,
            n_steps=n_steps,
            first_value=first_value,
            last_value=last_value,
            bounds_variable=None,
            first_bounds=None,
            last_bounds=None,
        )

    bounds = header.variables.get(str(bounds_name))
    if bounds is None:
        msg = (
            f"the time variable {time.name!r} names {bounds_name!r} as its bounds, "
            "and the file has no variable of that name"
        )
        raise UnreadableHeaderError(msg)

    shape = tuple(header.dimensions[each].length for each in bounds.dimension_ids)
    if (
        len(bounds.dimension_ids) != 2  # noqa: PLR2004 - a time step, then its two ends
        or bounds.dimension_ids[0] != time.dimension_ids[0]
        or shape[1] != 2  # noqa: PLR2004 - a lower and an upper bound
    ):
        msg = (
            f"the bounds variable {bounds.name!r} should hold two values per step of "
            f"{time.name!r}, and has dimensions {shape}"
        )
        raise UnreadableHeaderError(msg)

    first_bounds = (
        _value_at(source, header, bounds, record=0, within=0),
        _value_at(source, header, bounds, record=0, within=1),
    )
    last_bounds = (
        _value_at(source, header, bounds, record=n_steps - 1, within=0),
        _value_at(source, header, bounds, record=n_steps - 1, within=1),
    )

    return FirstAndLastTimeBounds(
        time_variable=time.name,
        n_steps=n_steps,
        first_value=first_value,
        last_value=last_value,
        bounds_variable=bounds.name,
        first_bounds=first_bounds,
        last_bounds=last_bounds,
    )


def read_netcdf3_file_header(
    source: ByteSource, *, time_variable: str | None = None
) -> FileHeader:
    r"""
    Read a classic netCDF file's header, and optionally its first and last times

    Parameters
    ----------
    source
        The file's bytes

        Reading the header needs only its start: across sampled CMIP5 files, global
        and variable attributes together end well inside the first 64 KiB. Reading
        time values needs the bytes where they are stored, which for a record
        variable means two small reads, one at each end of the file.

    time_variable
        The time variable to read the first and last values and bounds of, or `None`
        not to read any values

    Returns
    -------
    :
        Everything the header declares, and the time values if asked for and the
        file has a variable of that name

        Attribute values are `str` for character attributes and numbers (or tuples of
        them) for numeric ones, exactly as stored -- nothing is coerced, because what
        an attribute *means* is a question about the project that wrote it and not
        about the format.

    Raises
    ------
    TruncatedHeaderError
        The bytes needed are beyond what `source` may give.

    UnreadableHeaderError
        The file does not begin like a classic netCDF file, its header does not parse
        as one, or its time values are not laid out as a time coordinate's should be.

    Examples
    --------
    A minimal file: no dimensions, one character attribute, no variables. Note the
    padding of both the name and the value out to a four-byte boundary.

    >>> from esmporium.headers.sources import HeldBytesSource
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
    ...     + b"\x00\x00\x00\x00" * 2  # var_list: absent
    ... )
    >>> read = read_netcdf3_file_header(HeldBytesSource(header, size=len(header)))
    >>> dict(read.global_attributes), dict(read.variable_attributes)
    ({'title': 'hi'}, {})
    """
    header = _read_header(source)

    time_bounds = None
    if time_variable is not None and time_variable in header.variables:
        time_bounds = _first_and_last_time_bounds(
            source, header, header.variables[time_variable]
        )

    return FileHeader(
        layout=HeaderLayout.NETCDF3,
        global_attributes=header.global_attributes,
        variable_attributes={
            name: variable.attributes for name, variable in header.variables.items()
        },
        time_bounds=time_bounds,
        file_size=source.size,
        bytes_read=source.bytes_read,
        n_requests=source.n_requests,
    )
