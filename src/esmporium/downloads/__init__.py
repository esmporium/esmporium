"""
Downloading data from the ESGF data nodes
"""

# Note for developers:
# "This package does not open the files it downloads"
# It writes bytes and checks digests. It does not read netCDF headers, does not
# sniff the magic bytes that tell classic netCDF from netCDF-4/HDF5, and imports no
# netCDF library. Reading what is inside a file is a separate step's work.
from esmporium.downloads.errors import (
    ChecksumMismatchError,
    DataNodeBlockedError,
    DataNodeFetchError,
    DataNodeHostFaultError,
    DataNodeTimeoutError,
    DataNodeTransportError,
    NeedsCredentialsError,
    RouteMissingError,
    ShortReadError,
    SizeMismatchError,
    UnfetchableRouteError,
)
from esmporium.downloads.fetching import (
    DEFAULT_CHUNK_BYTES,
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_READ_TIMEOUT_SECONDS,
    FetchOutcome,
    Hasher,
    ProgressCallback,
    fetch_to_path,
    part_path,
)
from esmporium.downloads.health import (
    DataNodeCall,
    DataNodeCallObserver,
    fan_out,
)
from esmporium.downloads.limits import (
    DEFAULT_PER_DATA_NODE,
    DataNodeLimiter,
    build_data_node_limiter,
    no_data_node_limit,
)
from esmporium.downloads.paths import (
    FileTarget,
    PathBuilder,
    UnsafeDownloadPathError,
    esmporium_layout,
    local_path,
)
from esmporium.downloads.retry import (
    DEFAULT_ATTEMPTS,
    build_download_retrying,
    is_transient,
)
from esmporium.downloads.routes import Route, https_twin, is_fetchable
from esmporium.downloads.run import (
    DownloadRequest,
    DownloadResult,
    DuplicateDestinationError,
    ResultCallback,
    RouteFailure,
    RunTotals,
    StartCallback,
    download_file,
    download_files,
    totals,
)
from esmporium.downloads.selectors import (
    DEFAULT_DATA_NODE_SELECTOR,
    DataNodeSelector,
    SelectorOfferedNoRouteError,
    build_data_node_list_selector,
    build_list_selector,
    prepare_routes,
    select_as_given,
)

__all__ = [
    "DEFAULT_ATTEMPTS",
    "DEFAULT_CHUNK_BYTES",
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_DATA_NODE_SELECTOR",
    "DEFAULT_PER_DATA_NODE",
    "DEFAULT_READ_TIMEOUT_SECONDS",
    "ChecksumMismatchError",
    "DataNodeBlockedError",
    "DataNodeCall",
    "DataNodeCallObserver",
    "DataNodeFetchError",
    "DataNodeHostFaultError",
    "DataNodeLimiter",
    "DataNodeSelector",
    "DataNodeTimeoutError",
    "DataNodeTransportError",
    "DownloadRequest",
    "DownloadResult",
    "DuplicateDestinationError",
    "FetchOutcome",
    "FileTarget",
    "Hasher",
    "NeedsCredentialsError",
    "PathBuilder",
    "ProgressCallback",
    "ResultCallback",
    "Route",
    "RouteFailure",
    "RouteMissingError",
    "RunTotals",
    "SelectorOfferedNoRouteError",
    "ShortReadError",
    "SizeMismatchError",
    "StartCallback",
    "UnfetchableRouteError",
    "UnsafeDownloadPathError",
    "build_data_node_limiter",
    "build_data_node_list_selector",
    "build_download_retrying",
    "build_list_selector",
    "download_file",
    "download_files",
    "esmporium_layout",
    "fan_out",
    "fetch_to_path",
    "https_twin",
    "is_fetchable",
    "is_transient",
    "local_path",
    "no_data_node_limit",
    "part_path",
    "prepare_routes",
    "select_as_given",
    "totals",
]
