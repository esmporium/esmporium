"""
Reading a file's header from a data node, without downloading the file

ESGF's search index publishes only the facets a dataset is catalogued by, so anything
else a file knows about itself has to be read out of the file. The thing we want is its
global attributes -- specifically the `parent_*` ones, which say what simulation a run
branched from and which CMIP5 and CMIP6 publish nowhere else.

A header is a few kilobytes at the start of a file that can be hundreds of megabytes,
so this fetches a prefix with one HTTP range request and reads the attributes out of it.

**This is its own package, beside [esmporium.downloads][] rather than inside it**, and
that is a deliberate boundary rather than tidiness. The two want opposite policies from
the same-looking failures. A download retries a timeout, because finishing a large
transfer is worth another go; a header read does not, because a node that cannot send
four kilobytes will not manage it on the second ask, and the next mirror is the better
bet. A download is ranked by throughput; a 64 KiB read measures latency and would drag
a working replica's measured speed to nothing if the two were pooled. So they share
their vocabulary -- [Route][esmporium.downloads.Route], the data node selectors, the
per-node limiter -- and keep their own retry policies, outcomes and health.

The package is also deliberately **not about parents**. It reads *all* of a file's
global attributes into a plain mapping and says nothing about what they mean; deciding
that CMIP5's `parent_experiment_rip` is what CMIP6 calls `parent_variant_label` is a
question about those projects, answered in [esmporium.db.parent_headers][]. Reading a
header is a general capability, and a local archive walked from disk will want the same
one.
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
