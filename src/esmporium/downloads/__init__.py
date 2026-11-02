"""
Downloading data from the ESGF data nodes

Where [esmporium.search][] finds out what exists and
[esmporium.db][] records it, this package fetches the bytes.

**This package never imports [esmporium.db][]**, deliberately and for the same reason
[esmporium.search][] does not: the dependency runs one way, so everything here can be
tested without a database, and the database layer stays the only thing that knows
about the database. The engine-taking entry point therefore lives in `db`, as
[download][esmporium.db.download], exactly as
[search_and_track][esmporium.db.search_and_track] does for searching.

The pieces, from the bottom up:

| module | what it does |
| --- | --- |
| [routes][esmporium.downloads.routes] | what a fetchable route is, and what is not |
| [paths][esmporium.downloads.paths] | where a file goes on our disk |
| [health][esmporium.downloads.health] | what we observe about each attempt |
| [errors][esmporium.downloads.errors] | how an attempt can fail, classified |
| [retry][esmporium.downloads.retry] | when to try the same data node again |
| [fetching][esmporium.downloads.fetching] | one route, one data node, bytes to a path |
| [selectors][esmporium.downloads.selectors] | which route to try next |
| [run][esmporium.downloads.run] | work through many files, falling back on mirrors |

!!! note "This package does not open the files it downloads"
    It writes bytes and checks digests. It does not read netCDF headers, does not
    sniff the magic bytes that tell classic netCDF from netCDF-4/HDF5, and imports no
    netCDF library. Reading what is inside a file is a separate step's work.
"""

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
    ResultCallback,
    RouteFailure,
    RunTotals,
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
    "DEFAULT_READ_TIMEOUT_SECONDS",
    "ChecksumMismatchError",
    "DataNodeBlockedError",
    "DataNodeCall",
    "DataNodeCallObserver",
    "DataNodeFetchError",
    "DataNodeHostFaultError",
    "DataNodeSelector",
    "DataNodeTimeoutError",
    "DataNodeTransportError",
    "DownloadRequest",
    "DownloadResult",
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
    "UnfetchableRouteError",
    "UnsafeDownloadPathError",
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
    "part_path",
    "prepare_routes",
    "select_as_given",
    "totals",
]
