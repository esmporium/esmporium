"""
A route to bytes, as the download layer sees one
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from urllib.parse import urlsplit, urlunsplit

from esmporium.datasets import AccessService, ContainerFormat

_FETCHABLE_CONTAINER_FORMATS = frozenset(
    {
        ContainerFormat.NETCDF,
        # Declared fetchable because they are files on an HTTP server when they are
        # served at all. Note that every kerchunk route ESGF-NG advertises answered
        # 404 when sampled (12 of 12, 2026-10-27), so in practice fetching one gets
        # `MISSING` -- which is the right answer, and better than refusing to try.
        ContainerFormat.KERCHUNK,
    }
)
"""
Container formats whose routes are actual bytes we can stream to a file

`ZARR` and `ICECHUNK` are *stores*, not files: opening one needs a storage backend and
possibly a snapshot or branch, none of which a destination path can express. `HTML` is
a page about the data rather than the data. See
[ContainerFormat][esmporium.datasets.ContainerFormat].
"""


@dataclass(frozen=True)
class Route:
    """
    One way of getting one file's bytes from one data node
    """

    url: str
    """
    The URL to fetch, exactly as the API published it

    See [DataAccess.url][esmporium.db.schema.DataAccess.url]. Not normalised, which is
    why [is_fetchable][(m).is_fetchable] exists.
    """

    data_node: str
    """
    The data node serving this route, e.g. `esgf.nci.org.au`

    See [DataAccess.data_node_id][esmporium.db.schema.DataAccess.data_node_id]. **Not
    the host in [url][(c).url]**: the two differ, e.g. `ceda.ac.uk` serves over
    `dap.ceda.ac.uk`. Health is tracked against this, because this is the thing that
    is up or down.
    """

    service: AccessService
    """
    How the bytes move. See [AccessService][esmporium.datasets.AccessService].
    """

    container_format: ContainerFormat
    """
    What the bytes are. See [ContainerFormat][esmporium.datasets.ContainerFormat].
    """

    data_access_id: int | None = None
    """
    Which [DataAccess][esmporium.db.schema.DataAccess] row this came from

    `None` for a route we *derived* rather than read, which today means an `https://`
    twin of an `http://`-only route (see
    [https_twin][esmporium.downloads.https_twin]). Recorded so that a download can say
    which stored route served it, and left `None` rather than borrowing the original's
    id, because the URL really fetched was not the URL that row holds.
    """


def is_fetchable(route: Route) -> bool:
    """
    Decide whether a route is bytes we can stream to a file

    `DataAccess` deliberately records *every* route an API published, including ones
    that are not downloadable -- a route we dropped is one we would never know we had
    been offered. So something has to filter them, and this is it.

    Parameters
    ----------
    route
        The route to judge

    Returns
    -------
    :
        Whether [fetch_to_path][esmporium.downloads.fetch_to_path] can handle it

    Examples
    --------
    >>> from esmporium.datasets import AccessService, ContainerFormat
    >>> https_netcdf = Route(
    ...     url="https://esgf.example.org/a.nc",
    ...     data_node="esgf.example.org",
    ...     service=AccessService.HTTPS,
    ...     container_format=ContainerFormat.NETCDF,
    ... )
    >>> is_fetchable(https_netcdf)
    True

    An OPeNDAP route is a landing page, not the data:

    >>> is_fetchable(
    ...     Route(
    ...         url="https://esgf.example.org/a.nc.html",
    ...         data_node="esgf.example.org",
    ...         service=AccessService.OPENDAP,
    ...         container_format=ContainerFormat.HTML,
    ...     )
    ... )
    False
    """
    return (
        route.service == AccessService.HTTPS
        and route.container_format in _FETCHABLE_CONTAINER_FORMATS
    )


def https_twin(route: Route) -> Route | None:
    """
    Get the `https://` version of an `http://`-only route, if there is one to make

    Some replicas are indexed only over plain HTTP but serve HTTPS on the same path.
    Trying the secure twin first costs one request and sometimes turns a data node we
    would have given up on into one that works.

    The twin carries `data_access_id=None`, because the URL it fetches is not the URL
    the stored row holds.

    Parameters
    ----------
    route
        The route to make a twin of

    Returns
    -------
    :
        The `https://` twin, or `None` if `route` is not `http://` (so there is
        nothing to upgrade)

    Examples
    --------
    >>> from esmporium.datasets import AccessService, ContainerFormat
    >>> twin = https_twin(
    ...     Route(
    ...         url="http://esgf.example.org/a.nc",
    ...         data_node="esgf.example.org",
    ...         service=AccessService.HTTPS,
    ...         container_format=ContainerFormat.NETCDF,
    ...         data_access_id=7,
    ...     )
    ... )
    >>> twin.url
    'https://esgf.example.org/a.nc'
    >>> print(twin.data_access_id)
    None
    """
    split = urlsplit(route.url)
    if split.scheme != "http":
        return None

    return replace(
        route,
        url=urlunsplit(split._replace(scheme="https")),
        data_access_id=None,
    )
