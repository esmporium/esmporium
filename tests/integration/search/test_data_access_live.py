"""
Test data access tracking end to end against the live ESGF search APIs

The unit tests already pin the parsing and the writing, so if those pass and these
fail, the thing that changed is on the other end of the wire.

The matrix is four cells, not six, because the two APIs barely overlap:

|              | CMIP5                 | CMIP6          | CMIP7              |
|--------------|-----------------------|----------------|--------------------|
| ESGF-1 Solr  | yes -- the only route | yes            | no -- holds none   |
| ESGF-NG STAC | no -- no collection   | yes, east+west | yes -- only route  |
"""

from __future__ import annotations

import httpx
import pytest
from sqlmodel import Session, create_engine, select

from esmporium.datasets import AccessService, ContainerFormat
from esmporium.db import (
    DataAccess,
    Dataset,
    DatasetVersion,
    File,
    configure_sqlite_for_concurrency,
    ingest_parsed_documents,
    record_solr_file_access,
)
from esmporium.db.migrate import upgrade_to_head
from esmporium.query import QueryCMIP5, QueryCMIP6, QueryCMIP7
from esmporium.search import (
    INBUILT_SEARCH_API_FACADE_STORE,
    SOLR_FORMAT_TAG,
    NoFacadeAnsweredError,
    SearchAPIRequestError,
    build_list_selector,
    search_single_project,
)

pytestmark = pytest.mark.hits_esgf_search_api

HTTP_NOT_FOUND = 404
"""What an advertised-but-unserved route answers (named, so the assert reads)"""

LIMIT = 100
"""
Page size for the dataset searches

Note this is a *page size*, not a cap on results: a search matching more than this
pages until it has them all. The queries below are therefore narrowed to a single
model and ensemble member, so each cell is a handful of datasets in one page. Asking
for a page of 2 against an unnarrowed query is what made the first version of this
file take half an hour.
"""

MAX_RESULTS = 200
"""
Refuse rather than page forever

A belt-and-braces guard on the narrowing above: if a query stops being narrow --
because a model gains ensemble members, say -- this fails loudly instead of quietly
turning into hundreds of requests.
"""


def _facade(project: str, host: str):
    """The inbuilt facade for one project on one host."""
    return INBUILT_SEARCH_API_FACADE_STORE.get_api_facade_for_project_from_host(
        project, host
    )


CMIP5_QUERY = QueryCMIP5(
    experiment="historical",
    variable="tas",
    time_frequency="mon",
    model="MIROC-ESM",
    ensemble="r1i1p1",
)
"""A CMIP5 query narrow enough to be a handful of datasets"""

CMIP6_QUERY = QueryCMIP6(
    experiment_id="historical",
    variable_id="tas",
    frequency="mon",
    source_id="BCC-CSM2-MR",
    variant_label="r1i1p1f1",
)
"""
A CMIP6 query narrow enough to be a handful of datasets, on *both* APIs
"""

CMIP7_QUERY = QueryCMIP7(
    variable_id="tas",
    frequency="mon",
    source_id="CanESM5-1",
    variant_label="r1i1p2f1",
    experiment_id="1pctCO2",
)
"""
A CMIP7 query narrow enough to be a handful of datasets

Needs the experiment as well as the member: CMIP7 publishes many experiments per
member, so model and member alone is still dozens of datasets.
"""

# Note for developers:
# ESGF-NG West is listed separately from east because it is the only deployment
# publishing whole-version routes -- a kerchunk reference set and a Globus
# link -- so leaving it out would mean never exercising that branch of the
# model against a live answer.
LIVE_CASES = (
    pytest.param(
        _facade("CMIP5", "esgf.ceda.ac.uk"),
        CMIP5_QUERY,
        id="solr-cmip5-ceda",
    ),
    pytest.param(
        _facade("CMIP6", "esgf.nci.org.au"),
        CMIP6_QUERY,
        id="solr-cmip6-nci",
    ),
    pytest.param(
        _facade("CMIP6", "search.east.esgf.io"),
        CMIP6_QUERY,
        id="stac-cmip6-east",
    ),
    pytest.param(
        _facade("CMIP6", "search.west.esgf.io"),
        CMIP6_QUERY,
        id="stac-cmip6-west",
    ),
    pytest.param(
        _facade("CMIP7", "search.east.esgf.io"),
        CMIP7_QUERY,
        id="stac-cmip7-east",
    ),
)
"""
Every live (API, project) pair there is, plus both STAC mirrors for CMIP6
"""


def _record(session, facade, query):
    """Search, ingest, and fill in Solr's file information if this is Solr."""
    outcome = search_single_project(
        query,
        build_list_selector([facade]),
        limit=LIMIT,
        max_results=MAX_RESULTS,
    )
    documents = [document for pile in outcome.parsed_docs.values() for document in pile]
    if not documents:
        pytest.fail(
            f"{facade.search_api.host} answered about no datasets for {query}, "
            "so there is nothing to record file access for. These APIs report a "
            "mis-spelled facet name or value as a successful empty result, so this "
            "is a finding rather than an outage."
        )

    ingested = ingest_parsed_documents(session, documents)
    if facade.search_api.raw_docs_format_tag == SOLR_FORMAT_TAG:
        record_solr_file_access(session, facade, ingested, limit=100)

    session.commit()


@pytest.mark.parametrize("facade, query", LIVE_CASES)
def test_a_live_search_records_files_and_routes(engine, skip_or_fail, facade, query):
    """
    Searching for real datasets leaves files and routes to them in the database

    The key test of this step: every assertion here is about what a live API really
    published, not about what we think it publishes.
    """
    upgrade_to_head(engine)

    try:
        with Session(engine) as session:
            _record(session, facade, query)
    except NoFacadeAnsweredError as error:
        skip_or_fail(
            error.failures,
            skippable=SearchAPIRequestError,
            reason=f"{facade.search_api.host} did not answer",
        )

    with Session(engine) as session:
        files = session.exec(select(File)).all()
        routes = session.exec(select(DataAccess)).all()

        assert files, "a live search recorded no files"
        assert routes, "a live search recorded no routes to bytes"

        for stored in files:
            assert stored.filename
            assert stored.size and stored.size > 0, (
                f"{stored.filename} was published with no usable size"
            )

        # Somewhere in there is the ordinary way of getting a netCDF file.
        #
        # Note `==` rather than `is` throughout: a value read back out of the
        # database is a plain `str`, not an enum member (see
        # `DataAccess.service`), and it compares equal to the member but is not it.
        assert any(
            route.service == AccessService.HTTPS
            and route.container_format == ContainerFormat.NETCDF
            and route.file_id is not None
            for route in routes
        )

        # The invariant on `DataAccess.file_id`, checked against live data: a route
        # which serves one file serves a file of its own dataset version.
        version_of_file = {stored.id: stored.dataset_version_id for stored in files}
        for route in routes:
            if route.file_id is not None:
                assert version_of_file[route.file_id] == route.dataset_version_id


@pytest.mark.parametrize("facade, query", LIVE_CASES)
def test_a_live_search_records_a_checksum_we_can_name(
    engine, skip_or_fail, facade, query
):
    """
    Whatever the API publishes, we end up with a digest and the algorithm it is of

    The two APIs say this very differently -- Solr names the algorithm in its own
    field, STAC encodes it as a multihash prefix -- so this passing for both is what
    makes a checksum from either comparable with the other.
    """
    upgrade_to_head(engine)

    try:
        with Session(engine) as session:
            _record(session, facade, query)
    except NoFacadeAnsweredError as error:
        skip_or_fail(
            error.failures,
            skippable=SearchAPIRequestError,
            reason=f"{facade.search_api.host} did not answer",
        )

    with Session(engine) as session:
        files = session.exec(select(File)).all()

        named = [stored for stored in files if stored.checksum_type is not None]
        assert named, (
            "no file came back with a checksum we could name the algorithm of; "
            "if a publisher has started using a multihash code we do not know, "
            "the digest is still stored but is no longer comparable across APIs"
        )
        for stored in named:
            assert stored.checksum
            assert stored.checksum_type == "SHA256"


def _file_facts(engine, facade, query):
    """Record a query through one facade and read back what it stored about each file"""
    upgrade_to_head(engine)

    with Session(engine) as session:
        _record(session, facade, query)

    with Session(engine) as session:
        rows = session.exec(
            select(File, DatasetVersion).join(
                DatasetVersion, File.dataset_version_id == DatasetVersion.id
            )
        ).all()

        return {(version.version, stored.filename): stored for stored, version in rows}


def test_the_two_apis_agree_about_one_cmip6_dataset(
    engine, database_path, skip_or_fail
):
    """
    Solr and STAC, head to head on the same dataset version: do they say the same thing?

    CMIP6 is the one project both serve, so it is the only place this can be asked at
    all -- and it needs asking, because both answers land in the same
    [File][esmporium.db.schema.File] row. One row per
    `(dataset_version_id, filename)`, whatever described it, so if the two APIs
    disagreed, ingest order would decide what we hold.

    They agree, and that is the finding. On all four facts we keep about a file --
    size, checksum, the algorithm it is of, and the tracking id -- Solr and STAC are
    identical, so a checksum learned from one really is comparable with the other,
    and whichever answered first, we hold the same thing.
    """
    solr_facade = _facade("CMIP6", "esgf.nci.org.au")
    stac_facade = _facade("CMIP6", "search.west.esgf.io")

    stac_engine = configure_sqlite_for_concurrency(
        create_engine(f"sqlite:///{database_path.parent / 'stac.db'}")
    )

    try:
        solr = _file_facts(engine, solr_facade, CMIP6_QUERY)
        stac = _file_facts(stac_engine, stac_facade, CMIP6_QUERY)
    except NoFacadeAnsweredError as error:
        skip_or_fail(
            error.failures,
            skippable=SearchAPIRequestError,
            reason="one of the two APIs did not answer",
        )

    shared = sorted(set(solr) & set(stac))
    assert shared, (
        "the two APIs returned no (version, filename) in common for "
        f"{CMIP6_QUERY}, so there is nothing to compare. Solr offered "
        f"{sorted(solr)[:3]} and STAC offered {sorted(stac)[:3]}. Either the query "
        "has drifted off data one of them holds, or they no longer agree about which "
        "version is published -- both findings rather than outages."
    )

    for key in shared:
        from_solr, from_stac = solr[key], stac[key]
        assert (
            from_solr.size,
            from_solr.checksum,
            from_solr.checksum_type,
            from_solr.tracking_id,
        ) == (
            from_stac.size,
            from_stac.checksum,
            from_stac.checksum_type,
            from_stac.tracking_id,
        ), (
            f"the two APIs describe {key[1]} of version {key[0]} differently. "
            "These facts describe the bytes rather than where they are, so they are "
            "supposed to be the same from either, and a `File` row can only hold one "
            "answer. See esmporium.db.FileFactsChangedWarning."
        )


def test_the_west_cmip6_mirror_offers_whole_version_routes(engine, skip_or_fail):
    """
    West CMIP6 really does publish a virtual store and a Globus link, right now

    This is the live evidence that the non-file branch of the model is needed today
    rather than someday: a kerchunk reference set covering a whole dataset version,
    fetched over plain HTTPS and opened as zarr, in the same `assets` dict as the
    files. East publishes neither, which is why this is one named test rather than a
    matrix row.

    If ESGF stops publishing these, this test fails and the right response is to ask
    what replaced them -- not to delete the whole-version branch, which is also where
    a real zarr or icechunk store would land.
    """
    upgrade_to_head(engine)
    facade = _facade("CMIP6", "search.west.esgf.io")

    try:
        with Session(engine) as session:
            _record(session, facade, CMIP6_QUERY)
    except NoFacadeAnsweredError as error:
        skip_or_fail(
            error.failures,
            skippable=SearchAPIRequestError,
            reason="search.west.esgf.io did not answer",
        )

    with Session(engine) as session:
        whole_version = session.exec(
            select(DataAccess).where(DataAccess.file_id.is_(None))
        ).all()

        assert whole_version, "west published no whole-version routes"
        formats = {route.container_format for route in whole_version}
        assert ContainerFormat.KERCHUNK in formats
        assert ContainerFormat.HTML in formats

        kerchunk = next(
            route
            for route in whole_version
            if route.container_format == ContainerFormat.KERCHUNK
        )
        # Fetched over HTTPS, opened as kerchunk: the reason these are two columns.
        assert kerchunk.service == AccessService.HTTPS
        assert kerchunk.url.endswith(".kerchunk.parq")


def test_a_live_kerchunk_route_is_advertised_and_not_yet_served(engine, skip_or_fail):
    """
    The route west publishes is real; the bytes behind it are not there yet

    One `HEAD`, which is as far as esmporium ever goes towards opening anything. It
    exists because "advertised" and "available" came apart here: every
    `.kerchunk.parq` href sampled answers `404` (twelve of twelve on 2026-10-26), so
    a download step which trusted the advertised `access` list would fail on every
    one of them.

    This is the check that tells us when that changes, in either direction:

    - it starts answering `200` -- virtual access is live, and the download step has
      a second kind of thing to open. Say so in
      [ContainerFormat.KERCHUNK][esmporium.datasets.ContainerFormat.KERCHUNK] and
      decide what reads it.
    - the asset stops being published -- ask what replaced it, rather than deleting
      the whole-version branch of the model, which is also where a real zarr or
      icechunk store would land.

    A node which does not answer at all is a skip, not a failure: that is an outage
    rather than a finding.
    """
    upgrade_to_head(engine)
    facade = _facade("CMIP6", "search.west.esgf.io")

    try:
        with Session(engine) as session:
            _record(session, facade, CMIP6_QUERY)
    except NoFacadeAnsweredError as error:
        skip_or_fail(
            error.failures,
            skippable=SearchAPIRequestError,
            reason="search.west.esgf.io did not answer",
        )

    with Session(engine) as session:
        kerchunk = session.exec(
            select(DataAccess).where(
                DataAccess.container_format == ContainerFormat.KERCHUNK
            )
        ).first()

    assert kerchunk is not None, "west published no kerchunk route to check"

    try:
        with httpx.Client(follow_redirects=True, timeout=30.0) as client:
            answer = client.head(kerchunk.url)
    except httpx.RequestError as error:
        pytest.skip(f"{kerchunk.url} could not be reached: {error}")

    assert answer.status_code == HTTP_NOT_FOUND, (
        f"{kerchunk.url} answered {answer.status_code}, not {HTTP_NOT_FOUND}. "
        "Virtual access through ESGF-NG may now be served, which would be news: see "
        "this test's docstring for what to do about it."
    )


def test_a_live_cmip5_file_is_recorded_against_its_own_variables_dataset(
    engine, skip_or_fail
):
    """
    Every CMIP5 file lands against the dataset version for *its* variable

    One CMIP5 Solr document describes every variable in its table -- 56 of them here
    -- so searching for `tas` legitimately teaches us about 56 datasets and records
    files for all of them. That is existing, correct behaviour, not over-fetching.

    What could go wrong is *attribution*: a CMIP5 `dataset_id` is table-grained, so
    the file request has to narrow by variable, and the answers have to be placed
    against the version for that variable rather than whichever one came first. This
    checks that, which is the thing the per-variable batching in
    `plan_solr_file_batches` exists for.
    """
    upgrade_to_head(engine)
    facade = _facade("CMIP5", "esgf.ceda.ac.uk")

    try:
        with Session(engine) as session:
            _record(session, facade, CMIP5_QUERY)
    except NoFacadeAnsweredError as error:
        skip_or_fail(
            error.failures,
            skippable=SearchAPIRequestError,
            reason="esgf.ceda.ac.uk did not answer",
        )

    with Session(engine) as session:
        rows = session.exec(
            select(File, Dataset)
            .join(DatasetVersion, File.dataset_version_id == DatasetVersion.id)
            .join(Dataset, DatasetVersion.dataset_id == Dataset.id)
        ).all()

        assert rows
        # A CMIP5 filename starts with its variable, so the filename is the
        # independent check on where we filed it.
        misfiled = [
            (stored.filename, dataset.variable)
            for stored, dataset in rows
            if not stored.filename.startswith(f"{dataset.variable}_")
        ]
        assert not misfiled, misfiled

        # And the point of all that: more than one variable really was recorded, so
        # the check above is not vacuously true of a single-variable result.
        assert len({dataset.variable for _, dataset in rows}) > 1
