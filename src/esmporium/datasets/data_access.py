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


class DownloadOutcome(str, Enum):
    """
    How one attempt to fetch bytes over a route turned out

    Lives here, beside [AccessService][(m).AccessService] and
    [ContainerFormat][(m).ContainerFormat], because it is the same shape of problem
    they solve: a term two layers have to agree on.
    [esmporium.downloads][] produces one of these per attempt and
    [DataNodeCallRecord.outcome][esmporium.db.schema.DataNodeCallRecord.outcome]
    stores it, so it can belong to neither.

    `str`-valued so the stored column reads as the name rather than an integer nobody
    can interpret, matching
    [DatasetChangeType][esmporium.db.schema.DatasetChangeType].

    **These are the distinctions that change what we do next**, which is why there are
    nine of them and not a `success` boolean. Three of them are failures that say
    nothing about the data node (`MISSING`, `NEEDS_CREDENTIALS`, and arguably
    `BLOCKED`), so counting them against its health would retire a node for serving us
    correctly.
    """

    SUCCESS = "success"
    """
    The bytes arrived, and matched whatever we could check them against

    "Whatever we could check" is the caveat: a route whose file has no usable checksum
    still reports this, with the verification recorded separately on
    [Download.checksum_verified][esmporium.db.schema.Download.checksum_verified].
    """

    MISSING = "missing"
    """
    The data node answered that there is nothing at this URL (404 or 410)

    **Not the data node's fault, and deliberately excluded from its failure rate.** A
    route we recorded can simply stop existing -- most visibly for the kerchunk
    routes ESGF-NG advertises, every one of which was a 404 when sampled. Try the next
    mirror; do not retry this one, and do not hold it against the node.
    """

    NEEDS_CREDENTIALS = "needs_credentials"
    """
    The data node asked us to authenticate (401)

    Never retried -- retrying an anonymous request that was refused for being
    anonymous will be refused again -- and never counted against the node, which is
    behaving correctly. esmporium fetches anonymously; supplying credentials is done
    by passing an already-authenticated client, so this outcome means "this route
    needs a client we were not given".

    Note the ambiguity with [BLOCKED][(c).BLOCKED]: a data node refusing an
    unauthenticated request *may* answer 403 rather than 401, in which case we cannot
    tell it apart from rate limiting and will record it as `BLOCKED`. Classifying on
    the status code alone is the best we can do without guessing at body text.
    """

    BLOCKED = "blocked"
    """
    The data node is refusing us for now (403, 429 or 503)

    Rate limiting, a concurrency cap, or a node that is up but not serving. Worth
    backing off and preferring another mirror, and worth retrying with a longer wait
    than an ordinary transient failure -- a `Retry-After` header, where one is sent,
    says how long.

    Counted against the node's health, unlike the two above, because a node that keeps
    refusing us is one we should stop going to first. See the note on
    [NEEDS_CREDENTIALS][(c).NEEDS_CREDENTIALS] for what else can land here.
    """

    TIMEOUT = "timeout"
    """
    The data node took too long, either to connect or to send the next chunk

    Measured per read rather than over the whole transfer: a large file legitimately
    takes a long time, so a wall-clock deadline would fail exactly the downloads we
    most want to finish. What is wrong is a transfer that has *stalled*.
    """

    HOST_FAULT = "host_fault"
    """
    We never reached the data node at all: DNS, TLS, or a refused connection

    Distinct from [TIMEOUT][(c).TIMEOUT] because nothing answered, so there is no
    response code to record -- the same distinction
    [SearchAPICallRecord.response_code][esmporium.db.schema.SearchAPICallRecord.response_code]
    draws with its `None`.
    """

    CHECKSUM_FAILED = "checksum_failed"
    """
    The bytes arrived in full and did not match the published digest

    **The one outcome that says the data node served us bad data**, rather than no
    data, so it is worth separating from every other failure even though it is rare.
    Whatever landed is discarded rather than kept: a file that fails its checksum is
    worse than a file we do not have, because it looks like one we do.
    """

    SHORT_READ = "short_read"
    """
    The transfer was cut off part way: fewer bytes arrived than the node promised

    "Promised" means the response's own `Content-Length`, so this is the data node
    failing to deliver what it said it would -- a dropped connection, a proxy giving
    up. Worth retrying, and it counts against the node.

    Checked because it is the failure a missing checksum hides: a file with no digest
    to verify against, truncated in transit, is otherwise indistinguishable from a
    complete one, and would be renamed into place and recorded as a success.
    """

    SIZE_MISMATCH = "size_mismatch"
    """
    The node served a *complete* file, of a different size than the index published

    Told apart from [SHORT_READ][(c).SHORT_READ] by the response's own
    `Content-Length`: the node delivered exactly what it said it would, so the
    transfer was fine and it is the **published metadata that is wrong**. The file on
    the node is not the file the search index describes.

    **This is why the distinction exists, rather than both being "wrong size".**
    Neither is retried, but only one is the data node's fault. Charging this to the
    node would slowly retire a perfectly good replica for a publication error it had
    no part in -- and since `esmporium` ranks nodes by health, that would end up
    steering downloads *away* from working nodes.

    Not hypothetical. Live CMIP7 `areacella` for `CanESM5-1 piControl r1i1p2f1`
    (checked 2026-11-02) is published as 52,597 bytes with one SHA256, and the node
    serves a complete, valid netCDF-4 file of 44,638 bytes with a different one --
    its URL carries an older version than its siblings. Ten other `areacella` files
    for the same model agreed exactly, so this is a per-file publication fault, not a
    node that is broken.
    """

    TRANSPORT_ERROR = "transport_error"
    """
    Any other transient failure, e.g. a 5xx that is not 503, or a reset connection

    The catch-all for "worth trying again". Anything we cannot classify more
    specifically lands here rather than being silently treated as success.
    """

    @property
    def is_worth_retrying(self) -> bool:
        """
        Whether this outcome is worth trying again against the *same* data node

        Lives on the enum rather than in the retry policy so that there is one answer
        to this question, and so that adding a member forces a decision about it here
        rather than leaving it to be inferred somewhere else.

        `False` for [SUCCESS][(c).SUCCESS] too, which reads oddly in isolation but is
        right: the question is only ever asked about something that failed, and
        answering `True` would describe a retry loop that never terminates.

        Returns
        -------
        :
            `True` if retrying the same route might work
        """
        return self in {
            # The node is up and struggling, or the connection broke. All three
            # plausibly work on a second go.
            DownloadOutcome.BLOCKED,
            DownloadOutcome.TIMEOUT,
            DownloadOutcome.TRANSPORT_ERROR,
            # A transfer cut off part way is worth finishing: the node said how many
            # bytes there were, so a second attempt (or a resume) can get the rest.
            DownloadOutcome.SHORT_READ,
        }

    @property
    def counts_against_data_node(self) -> bool:
        """
        Whether this outcome should count against the data node's health

        Four failures say nothing about the node: a 404 means the route is gone, a
        401 means we did not present credentials, a
        [SIZE_MISMATCH][(c).SIZE_MISMATCH] means the index is wrong rather than the
        node, and none is the node behaving badly. Counting them would retire a data
        node for answering us correctly.

        [BLOCKED][(c).BLOCKED] *does* count, even though a rate limit is arguably
        correct behaviour too, because a node which keeps refusing us is one we should
        stop trying first -- which is exactly what health ranking is for.

        Returns
        -------
        :
            `True` if this outcome belongs in the node's failure rate
        """
        return self not in {
            DownloadOutcome.SUCCESS,
            DownloadOutcome.MISSING,
            DownloadOutcome.NEEDS_CREDENTIALS,
            # The node served exactly what it promised; the index is what is wrong.
            # See the note on SIZE_MISMATCH for why this matters so much here.
            DownloadOutcome.SIZE_MISMATCH,
        }
