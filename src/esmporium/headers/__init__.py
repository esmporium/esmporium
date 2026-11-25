"""
Reading a file's header, and the first and last of its time values, without the file

ESGF's search index publishes only the facets a dataset is catalogued by, so anything
else a file knows about itself has to be read out of the file: its global attributes
(the `parent_*` ones say what simulation a run branched from), its variables'
attributes (the time variable's `units` and `calendar`), and the first and last
values of its time coordinate and bounds (where its data starts and ends).

All of that is a few kilobytes at the start of a file that can be hundreds of
megabytes, plus a few values stored further in. So every reader here takes a
[ByteSource][esmporium.headers.ByteSource] and asks for only the bytes it needs: from
a data node over HTTP range requests
([RangeSource][esmporium.headers.RangeSource]), from a local file
([LocalFileSource][esmporium.headers.LocalFileSource]), or from bytes already in hand
([HeldBytesSource][esmporium.headers.HeldBytesSource]).

Classic netCDF is read by this package's own parser; netCDF-4/HDF5 by `h5netcdf`,
the optional `headers` extra.
"""

from esmporium.headers.contents import (
    DEFAULT_TIME_VARIABLE,
    FileHeader,
    FirstAndLastTimeBounds,
    HeaderLayout,
)
from esmporium.headers.errors import (
    HeaderNeedsCredentialsError,
    HeaderNodeBlockedError,
    HeaderNodeHostFaultError,
    HeaderParseError,
    HeaderReadError,
    HeaderReaderUnavailableError,
    HeaderReadTimeoutError,
    HeaderRouteMissingError,
    HeaderTransportError,
    MissingVariableAttributesError,
    NoSuchVariableError,
    RangesNotHonouredError,
    TruncatedHeaderError,
    UnreadableHeaderError,
    UnsupportedHeaderFormatError,
)
from esmporium.headers.fetching import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_READ_TIMEOUT_SECONDS,
    RangeSource,
    read_header,
)
from esmporium.headers.hdf5 import HDF5_MAGIC, read_hdf5_file_header
from esmporium.headers.health import (
    HeaderReadCall,
    HeaderReadCallObserver,
    fan_out,
)
from esmporium.headers.netcdf3 import (
    NETCDF3_MAGIC_64BIT_OFFSET,
    NETCDF3_MAGIC_CLASSIC,
    NETCDF3_MAGICS,
    read_netcdf3_file_header,
)
from esmporium.headers.reading import (
    MAGIC_BYTES_NEEDED,
    READERS_BY_LAYOUT,
    HeaderReader,
    read_file_header,
    read_first_and_last_time_bounds,
    read_global_attributes,
    read_variable_attributes,
    sniff_layout,
)
from esmporium.headers.retry import (
    DEFAULT_ATTEMPTS,
    build_header_retrying,
    is_worth_retrying,
)
from esmporium.headers.run import (
    DEFAULT_PER_DATA_NODE,
    HeaderReadResult,
    HeaderRequest,
    ResultCallback,
    RouteFailure,
    read_headers,
    read_one_header,
)
from esmporium.headers.sources import (
    DEFAULT_BLOCK_BYTES,
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_REQUESTS,
    ByteSource,
    HeldBytesSource,
    LocalFileSource,
)

__all__ = [
    "DEFAULT_ATTEMPTS",
    "DEFAULT_BLOCK_BYTES",
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_REQUESTS",
    "DEFAULT_PER_DATA_NODE",
    "DEFAULT_READ_TIMEOUT_SECONDS",
    "DEFAULT_TIME_VARIABLE",
    "HDF5_MAGIC",
    "MAGIC_BYTES_NEEDED",
    "NETCDF3_MAGICS",
    "NETCDF3_MAGIC_64BIT_OFFSET",
    "NETCDF3_MAGIC_CLASSIC",
    "READERS_BY_LAYOUT",
    "ByteSource",
    "FileHeader",
    "FirstAndLastTimeBounds",
    "HeaderLayout",
    "HeaderNeedsCredentialsError",
    "HeaderNodeBlockedError",
    "HeaderNodeHostFaultError",
    "HeaderParseError",
    "HeaderReadCall",
    "HeaderReadCallObserver",
    "HeaderReadError",
    "HeaderReadResult",
    "HeaderReadTimeoutError",
    "HeaderReader",
    "HeaderReaderUnavailableError",
    "HeaderRequest",
    "HeaderRouteMissingError",
    "HeaderTransportError",
    "HeldBytesSource",
    "LocalFileSource",
    "MissingVariableAttributesError",
    "NoSuchVariableError",
    "RangeSource",
    "RangesNotHonouredError",
    "ResultCallback",
    "RouteFailure",
    "TruncatedHeaderError",
    "UnreadableHeaderError",
    "UnsupportedHeaderFormatError",
    "build_header_retrying",
    "fan_out",
    "is_worth_retrying",
    "read_file_header",
    "read_first_and_last_time_bounds",
    "read_global_attributes",
    "read_hdf5_file_header",
    "read_header",
    "read_headers",
    "read_netcdf3_file_header",
    "read_one_header",
    "read_variable_attributes",
    "sniff_layout",
]
