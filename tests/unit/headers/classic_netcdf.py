r"""
Building small classic netCDF files, for testing the reader

Written to the same grammar the reader cites (netCDF Users Guide, Appendix B):

```text
header    = magic numrecs dim_list gatt_list var_list
var       = name nelems [dimid ...] vatt_list nc_type vsize begin
data      = non_recs recs
record    = [varslab ...]
```

Non-record variables' data comes first, one after another, each padded to four
bytes. Then the records, each holding one slab per record variable in `var_list`
order, padded to four bytes unless there is exactly one record variable.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from math import prod

from esmporium.headers import HeldBytesSource

NC_DIMENSION = 0x0A
NC_VARIABLE = 0x0B
NC_ATTRIBUTE = 0x0C
STREAMING = 0xFFFFFFFF

TYPES: dict[str, tuple[int, str, int]] = {
    "byte": (1, "b", 1),
    "char": (2, "s", 1),
    "short": (3, "h", 2),
    "int": (4, "i", 4),
    "float": (5, "f", 4),
    "double": (6, "d", 8),
}
"""Type name -> (nc_type, struct code, width)"""


def word(value: int) -> bytes:
    """Render a `NON_NEG`: four bytes, big-endian"""
    return value.to_bytes(4, "big")


def padded(raw: bytes) -> bytes:
    """Pad bytes to a four-byte boundary with NULs"""
    return raw + bytes(-len(raw) % 4)


def name(text: str) -> bytes:
    """Render a `name`: `nelems namestring`, padded"""
    encoded = text.encode()

    return word(len(encoded)) + padded(encoded)


def attribute(attribute_name: str, value: object) -> bytes:
    """
    Render one `attr`

    `str` is `NC_CHAR`, `float` is `NC_DOUBLE`, `int` is `NC_INT`, and a tuple of
    either is several values of that type.
    """
    if isinstance(value, str):
        encoded = value.encode()

        return name(attribute_name) + word(2) + word(len(encoded)) + padded(encoded)

    values = value if isinstance(value, tuple) else (value,)
    type_name = "double" if isinstance(values[0], float) else "int"
    nc_type, code, _ = TYPES[type_name]

    return (
        name(attribute_name)
        + word(nc_type)
        + word(len(values))
        + padded(struct.pack(f">{len(values)}{code}", *values))
    )


def att_list(attributes: dict[str, object]) -> bytes:
    """Render an `att_list`, `ABSENT` when empty"""
    if not attributes:
        return word(0) + word(0)

    return (
        word(NC_ATTRIBUTE)
        + word(len(attributes))
        + b"".join(attribute(key, value) for key, value in attributes.items())
    )


@dataclass
class Variable:
    """One variable to write"""

    name: str
    dimensions: tuple[str, ...]
    type_name: str
    values: list[float | int]
    """Every value, flattened, last dimension fastest, records included"""

    attributes: dict[str, object] = field(default_factory=dict)


@dataclass
class ClassicFile:
    """A built file: its bytes, possibly in pieces with gaps, and its size"""

    pieces: dict[int, bytes]
    size: int

    def source(self) -> HeldBytesSource:
        """Hold every piece"""
        return HeldBytesSource(self.pieces, size=self.size)

    def whole(self) -> bytes:
        """The file's bytes, which have to be in one piece"""
        assert list(self.pieces) == [0], "this file has gaps"

        return self.pieces[0]


def build(  # noqa: PLR0913 - one argument per thing a test varies
    dimensions: dict[str, int],
    variables: list[Variable],
    *,
    global_attributes: dict[str, object] | None = None,
    magic: bytes = b"CDF\x01",
    streaming: bool = False,
    data_starts_at: int | None = None,
) -> ClassicFile:
    """
    Build a classic netCDF file

    Parameters
    ----------
    dimensions
        Each dimension's name and length; length zero makes it the record dimension

    variables
        The variables, in `var_list` order

    global_attributes
        The global attributes

    magic
        `CDF\\x01` or `CDF\\x02`

    streaming
        Write `numrecs` as `STREAMING` rather than the count

    data_starts_at
        Where the data starts, if not straight after the header. Past the header,
        this leaves a gap the file does not hold, which is how a file larger than
        four gigabytes is built without writing it.

    Returns
    -------
    :
        The file
    """
    names = list(dimensions)
    record_dimension = next(
        (each for each, length in dimensions.items() if length == 0), None
    )

    def is_record(variable: Variable) -> bool:
        return bool(variable.dimensions) and variable.dimensions[0] == record_dimension

    def slab_values(variable: Variable) -> int:
        return prod(
            dimensions[each] for each in variable.dimensions if each != record_dimension
        )

    def slab_bytes(variable: Variable) -> int:
        return slab_values(variable) * TYPES[variable.type_name][2]

    record_variables = [each for each in variables if is_record(each)]
    numrecs = (
        len(record_variables[0].values) // slab_values(record_variables[0])
        if record_variables
        else 0
    )
    single = len(record_variables) == 1

    def vsize(variable: Variable) -> int:
        size = slab_bytes(variable)
        if is_record(variable) and single:
            return size

        return size + (-size % 4)

    def header(begins: dict[str, int]) -> bytes:
        dim_list = (
            word(NC_DIMENSION)
            + word(len(dimensions))
            + b"".join(name(each) + word(length) for each, length in dimensions.items())
            if dimensions
            else word(0) + word(0)
        )
        if variables:
            var_list = word(NC_VARIABLE) + word(len(variables))
            for variable in variables:
                begin = begins.get(variable.name, 0)
                var_list += (
                    name(variable.name)
                    + word(len(variable.dimensions))
                    + b"".join(word(names.index(each)) for each in variable.dimensions)
                    + att_list(variable.attributes)
                    + word(TYPES[variable.type_name][0])
                    + word(vsize(variable))
                    + (
                        begin.to_bytes(4, "big")
                        if magic == b"CDF\x01"
                        else begin.to_bytes(8, "big")
                    )
                )
        else:
            var_list = word(0) + word(0)

        return (
            magic
            + word(STREAMING if streaming else numrecs)
            + dim_list
            + att_list(global_attributes or {})
            + var_list
        )

    header_length = len(header({}))
    at = header_length if data_starts_at is None else data_starts_at

    begins: dict[str, int] = {}
    for variable in variables:
        if not is_record(variable):
            begins[variable.name] = at
            at += vsize(variable)

    for variable in record_variables:
        begins[variable.name] = at
        at += vsize(variable)

    def packed(variable: Variable, values: list[float | int]) -> bytes:
        _, code, _ = TYPES[variable.type_name]

        return struct.pack(f">{len(values)}{code}", *values)

    data = b""
    for variable in variables:
        if not is_record(variable):
            data += padded(packed(variable, variable.values))

    for record in range(numrecs):
        for variable in record_variables:
            per = slab_values(variable)
            slab = packed(variable, variable.values[record * per : (record + 1) * per])
            data += slab if single else padded(slab)

    rendered = header(begins)
    assert len(rendered) == header_length
    data_start = min(begins.values()) if begins else at

    if data_starts_at is None or data_starts_at == header_length:
        whole = rendered + data

        return ClassicFile(pieces={0: whole}, size=len(whole))

    return ClassicFile(
        pieces={0: rendered, data_start: data}, size=data_start + len(data)
    )
