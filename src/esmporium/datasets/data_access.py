"""
The vocabulary of a route to a dataset's data

A route has two independent properties, and they are deliberately named separately:
[AccessService][(m).AccessService] is *how the bytes move* and
[ContainerFormat][(m).ContainerFormat] is *what the bytes are*. They vary
independently -- most visibly for a kerchunk reference set, which is fetched over
plain HTTPS but opened as a zarr store -- so collapsing them into one "access type"
would make the commonest non-file route ESGF publishes today unrepresentable.

This vocabulary lives in [esmporium.datasets][] rather than in
[esmporium.db.schema][] because both sides need it: `search` reads these values out
of an API response and `db` stores them in a column. `db` may import `search`, but
never the reverse, so anything they must agree on belongs here.
"""

from enum import Enum


class AccessService(str, Enum):
    """
    How the bytes move over a route

    `str`-valued so the stored column reads as the name rather than an integer
    nobody can interpret, matching
    [DatasetChangeType][esmporium.db.schema.DatasetChangeType].
    """

    HTTPS = "https"
    """
    An ordinary HTTP(S) `GET`, e.g. a THREDDS `fileServer` URL

    ESGF-1 Solr calls this `HTTPServer` and ESGF-NG STAC calls it `https`; both mean
    the same thing, so both are recorded as this.
    """

    OPENDAP = "opendap"
    """
    OPeNDAP/DAP

    Note that ESGF-1 publishes the *landing page* for this, whose URL ends
    `.nc.html`, rather than the DAP endpoint itself.
    """

    GLOBUS = "globus"
    """
    Globus transfer

    ESGF-NG publishes a Globus *web app* page for a human to click; ESGF-1's
    Globus-backed indexes publish a `globus://<node>/<endpoint-uuid>/<path>` URL.
    """

    GRIDFTP = "gridftp"
    """
    GridFTP (`gsiftp://`)

    Only seen from ESGF-1, and only from the Globus-backed indexes.
    """


class ContainerFormat(str, Enum):
    """
    What the bytes on a route are

    Orthogonal to [AccessService][(m).AccessService]; see this module's docstring for
    why the two are separate.
    """

    NETCDF = "netcdf"
    """A netCDF file, either classic or netCDF-4/HDF5."""

    KERCHUNK = "kerchunk"
    """
    A kerchunk reference set: chunk pointers into netCDF files, opened as zarr

    In ESGF-NG these are parquet (the href ends `.kerchunk.parq`), not the JSON that
    kerchunk is more commonly written as.
    """

    ZARR = "zarr"
    """
    A zarr store

    Nothing publishes one to us yet. Declared so that the day something does, it is a
    new row rather than a schema change.
    """

    ICECHUNK = "icechunk"
    """
    An icechunk store

    As for [ZARR][(c).ZARR]: declared, not yet seen. Note that opening one needs more
    than a URL (a storage backend, credentials, and a snapshot, branch or tag), so
    whatever records one will need more than
    [DataAccess.url][esmporium.db.schema.DataAccess.url] to go on.
    """

    HTML = "html"
    """
    A web page *about* the data rather than the data

    Both of the non-file routes ESGF publishes today land here: an OPeNDAP `.nc.html`
    form page, and a Globus web-app link. Recording them as this is how a reader is
    told "this is not bytes you can open", rather than us dropping them and losing
    the fact that they were offered at all.
    """
