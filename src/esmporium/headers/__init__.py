"""
Reading a file's header from a data node, without downloading the file

ESGF's search index publishes only the facets a dataset is catalogued by, so anything
else a file knows about itself has to be read out of the file. The thing we want is its
global attributes -- specifically the `parent_*` ones, which say what simulation a run
branched from and which CMIP5 and CMIP6 publish nowhere else.

A header is a few kilobytes at the start of a file that can be hundreds of megabytes,
so this fetches a prefix with one HTTP range request and reads the attributes out of it.
"""

from esmporium.headers.errors import (
    HeaderNeedsCredentialsError,
    HeaderNodeBlockedError,
    HeaderNodeHostFaultError,
    HeaderParseError,
    HeaderParserUnavailableError,
    HeaderReadError,
    HeaderReadTimeoutError,
    HeaderRouteMissingError,
    HeaderTransportError,
    TruncatedHeaderError,
    UnreadableHeaderError,
    UnsupportedHeaderFormatError,
)
from esmporium.headers.fetching import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_MAX_WANT_BYTES,
    DEFAULT_READ_TIMEOUT_SECONDS,
    DEFAULT_WANT_BYTES,
    FileHeader,
    HeaderBytes,
    read_header,
)
from esmporium.headers.hdf5 import HDF5_MAGIC, read_hdf5_global_attributes
from esmporium.headers.health import (
    HeaderReadCall,
    HeaderReadCallObserver,
    fan_out,
)
from esmporium.headers.netcdf3 import (
    NETCDF3_MAGIC_64BIT_OFFSET,
    NETCDF3_MAGIC_CLASSIC,
    NETCDF3_MAGICS,
    read_netcdf3_global_attributes,
)
from esmporium.headers.parsing import (
    MAGIC_BYTES_NEEDED,
    PARSERS_BY_LAYOUT,
    HeaderLayout,
    HeaderParser,
    read_global_attributes,
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

__all__ = [
    "DEFAULT_ATTEMPTS",
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_MAX_WANT_BYTES",
    "DEFAULT_PER_DATA_NODE",
    "DEFAULT_READ_TIMEOUT_SECONDS",
    "DEFAULT_WANT_BYTES",
    "HDF5_MAGIC",
    "MAGIC_BYTES_NEEDED",
    "NETCDF3_MAGICS",
    "NETCDF3_MAGIC_64BIT_OFFSET",
    "NETCDF3_MAGIC_CLASSIC",
    "PARSERS_BY_LAYOUT",
    "FileHeader",
    "HeaderBytes",
    "HeaderLayout",
    "HeaderNeedsCredentialsError",
    "HeaderNodeBlockedError",
    "HeaderNodeHostFaultError",
    "HeaderParseError",
    "HeaderParser",
    "HeaderParserUnavailableError",
    "HeaderReadCall",
    "HeaderReadCallObserver",
    "HeaderReadError",
    "HeaderReadResult",
    "HeaderReadTimeoutError",
    "HeaderRequest",
    "HeaderRouteMissingError",
    "HeaderTransportError",
    "ResultCallback",
    "RouteFailure",
    "TruncatedHeaderError",
    "UnreadableHeaderError",
    "UnsupportedHeaderFormatError",
    "build_header_retrying",
    "fan_out",
    "is_worth_retrying",
    "read_global_attributes",
    "read_hdf5_global_attributes",
    "read_header",
    "read_headers",
    "read_netcdf3_global_attributes",
    "read_one_header",
    "sniff_layout",
]
