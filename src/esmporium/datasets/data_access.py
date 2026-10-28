"""
The vocabulary of a route to a dataset's data

A route has two independent properties, and they are deliberately named separately:
[AccessService][(m).AccessService] is *how the bytes move* and
[ContainerFormat][(m).ContainerFormat] is *what the bytes are*. They vary
independently -- most visibly for a kerchunk reference set, which is fetched over
plain HTTPS but opened as a zarr store -- so collapsing them into one "access type"
would make the commonest non-file route ESGF publishes today unrepresentable.
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

    Solr calls this `HTTPServer` and ESGF-NG STAC calls it `https`; both mean the
    same thing, so both are recorded as this.
    """

    OPENDAP = "opendap"
    """
    OPeNDAP/DAP

    Note that Solr publishes the *landing page* for this, whose URL ends
    `.nc.html`, rather than the DAP endpoint itself.
    """

    GLOBUS = "globus"
    """
    Globus transfer

    ESGF-NG publishes a Globus *web app* page for a human to click
    (`https://app.globus.org/file-manager?origin_id=...`). The Solr-backed indexes
    publish a `globus:<endpoint-uuid>/<path>` URL instead -- one colon and no
    authority, so it is not a URL any ordinary parser will take apart. Live examples,
    from `esgf.nci.org.au` and from the 1.5 bridge respectively:

    ```
    globus:2058c7d6-a79f-11e6-9ad6-22000a1e3b52/replica/CMIP6/AerChemMIP/...
    globus:8896f38e-68d1-4708-bce4-b1b3a3405809/css03_data/CMIP6/CMIP/...
    ```
    """

    GRIDFTP = "gridftp"
    """
    GridFTP (`gsiftp://`)

    Only seen from Solr, and only from the nodes which run a GridFTP server: live
    `esgf.nci.org.au` and the 1.5 bridge both publish these, where live
    `esgf.ceda.ac.uk` does not.
    """


class ContainerFormat(str, Enum):
    """
    What the bytes on a route are
    """

    # A note for developers:
    # **This is a routing hint, not a complete instruction**, and `netcdf` is where that
    # shows. It is two on-disk formats: classic netCDF and netCDF-4/HDF5. They need
    # different readers -- `scipy` and `h5netcdf` respectively, for `xarray` -- and ESGF
    # serves both, sometimes within one dataset. So whatever opens one of these routes
    # has to read the file's first four bytes to find out which it is; knowing the
    # `container_format` narrows the question without answering it. That is the same
    # argument as `service` and `container_format` being two columns, one level further
    # down, and the download step will meet it.

    # Routes recorded as this were checked against the bytes they lead to (2026-10-26):
    # a `HEAD` returned exactly the `size` we had recorded, the magic bytes were
    # netCDF's own, and the file opened over HTTPS without being downloaded first. So
    # for netCDF, what we record is what is really there.
    NETCDF = "netcdf"
    """
    A netCDF file, either classic or netCDF-4/HDF5
    """

    KERCHUNK = "kerchunk"
    """
    A kerchunk reference set: chunk pointers into netCDF files, opened as zarr

    In ESGF-NG these are parquet (the href ends `.kerchunk.parq`), not the JSON that
    kerchunk is more commonly written as.
    """

    # Future TODO note:
    # Nothing publishes one to us yet. Declared so that the day something does, it is a
    # new row rather than a schema change.

    # The media type to expect is `application/vnd.zarr`, which [STAC's Zarr best
    # practices](https://github.com/radiantearth/stac-best-practices/blob/main/best-practices-zarr.md)
    # qualify with parameters: `application/vnd.zarr; version=3`, and
    # `; profile=multiscales` for a pyramided store. That is why
    # [stac_data_access][esmporium.search.stac_data_access] matches on the media type
    # with its parameters stripped -- an exact-string match would miss every real
    # store.
    ZARR = "zarr"
    """
    A zarr store
    """

    # Future TODO note:
    # As for [ZARR][(c).ZARR]: declared, not yet seen. Note that opening one needs more
    # than a URL (a storage backend, credentials, and a snapshot, branch or tag), so
    # whatever records one will need more than
    # [DataAccess.url][esmporium.db.schema.DataAccess.url] to go on.

    # **Where that extra information will land is already decided, and it is not a new
    # column.** Whatever an API says about a store it says *in the asset*, and the
    # asset is kept verbatim in
    # [DataAccess.raw_json][esmporium.db.schema.DataAccess.raw_json], so a snapshot,
    # branch or tag published tomorrow is recorded tomorrow without a migration.

    # A column would have to be named after a convention, and as of 2026-10-27 there is
    # no convention to name it after: ESGF's own STAC item schema
    # (`esgf.github.io/stac-transaction-api/cmip6/v2.0.2/schema.json`) does not mention
    # zarr, icechunk, snapshots, branches or tags, nor does the ESGF-NG roadmap; the
    # [xarray-assets](https://github.com/stac-extensions/xarray-assets) extension that
    # would carry opening options is deprecated; and the
    # [zarr-conventions STAC convention](https://github.com/zarr-conventions/stac)
    # explicitly defers asset-level detail. What catalogues do in the meantime is
    # invent their own prefixed properties. Guessing one of those into a column now
    # would bake in a decision ESGF has not made.

    # Icechunk has no registered media type at all, so nothing maps onto this yet and
    # an icechunk asset would raise
    # [UnknownContainerFormatError][esmporium.search.UnknownContainerFormatError] --
    # which is the behaviour we want, because a route we silently dropped is one we
    # would never know we had been offered.
    ICECHUNK = "icechunk"
    """
    An icechunk store
    """

    HTML = "html"
    """
    A web page *about* the data rather than the data

    Both of the non-file routes ESGF publishes today land here: an OPeNDAP `.nc.html`
    form page, and a Globus web-app link. Recording them as this is how a reader is
    told "this is not bytes you can open", rather than us dropping them and losing
    the fact that they were offered at all.
    """
