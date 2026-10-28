"""
Tests of reading files and routes to them out of a search response

The assets here are cut down from the real recorded responses in
`tests/test-data/search`, keeping whatever quirk each test is about. The quirks are
the point: every one of them is something a live ESGF-NG node really publishes, and
several would be invisible to a test written against the STAC spec instead.
"""

from __future__ import annotations

import json

import pytest

from esmporium.datasets import AccessService, ContainerFormat
from esmporium.search import (
    UnknownAccessServiceError,
    UnknownContainerFormatError,
    stac_asset_is_one_file,
    stac_asset_roles,
    stac_data_access,
)
from esmporium.search.apis.esgf1 import (
    UnreadableURLTripleError,
    solr_file_access,
    solr_url_triple,
)

NETCDF_ASSET_NAME = "tas_Amon_BCC-CSM2-MR_historical_r1i1p1f1_gn_185001-201412.nc"
DRS_DIRECTORY = "CMIP6/CMIP/BCC/BCC-CSM2-MR/historical/r1i1p1f1/Amon/tas/gn/v20181126"

NETCDF_ASSET = {
    "href": f"https://esgf-node.ornl.gov/thredds/fileServer/css03_data/{NETCDF_ASSET_NAME}",
    "type": "application/netcdf",
    "description": "HTTPServer Link",
    "roles": ["data"],
    "protocol": "https",
    "alternate:name": "esgf-node.ornl.gov",
    "file:size": 405577368,
    "file:checksum": (
        "122042ef6eefb38c02b5138a9e967a7b49c572685f87aa383e780a25b46c1bd89d04"
    ),
    "file:local_path": f"{DRS_DIRECTORY}/{NETCDF_ASSET_NAME}",
    "cmip6:tracking_id": "hdl:21.14100/007da5e5-12bb-4fe9-8df9-5cd473280650",
}

# Note `"roles": None` and the misspelled `"role"`. This is verbatim from west.
KERCHUNK_ASSET = {
    "href": "https://esgf-node.ornl.gov/thredds/fileServer/user_pub_work/Kerchunk/CMIP6/tas/historical/Amon/CMIP6.CMIP.BCC.BCC-CSM2-MR.historical.r1i1p1f1.Amon.tas.gn.v20181126.kerchunk.parq",
    "type": "application/kerchunk",
    "description": "Kerchunk reference file for virtual aggregation",
    "roles": None,
    "role": ["data", "virtual"],
    "protocol": "kerchunk",
    "alternate:name": "esgf-node.ornl.gov",
}

# Note the `file:local_path` is a *directory*, and there is no size or checksum.
GLOBUS_ASSET = {
    "href": "https://app.globus.org/file-manager?origin_id=dea29ae8&origin_path=/css03_data/",
    "type": "text/html",
    "description": "Globus Web App Link",
    "roles": ["data"],
    "protocol": "globus",
    "alternate:name": "esgf-node.ornl.gov",
    "file:local_path": DRS_DIRECTORY,
    "cmip6:tracking_id": "hdl:21.14100/d362a7a6-d54e-3924-87c6-a73bf1f0523c",
}


def feature(**assets):
    """A STAC feature carrying just the assets a test cares about."""
    return {
        "id": "CMIP6.CMIP.BCC.BCC-CSM2-MR.historical.r1i1p1f1.Amon.tas.gn.v20181126",
        "assets": dict(assets),
    }


def test_asset_roles_reads_the_misspelled_role_key():
    """
    West's kerchunk asset has `"roles": null` and a misspelled `"role"`

    Reading only `roles` would see nothing, treat the asset as role-less, and drop
    the whole virtual-access route -- which is the one route this PR exists to make
    representable.
    """
    assert stac_asset_roles(KERCHUNK_ASSET) == ("data", "virtual")


def test_asset_roles_prefers_the_correctly_spelled_key():
    """`roles` wins where both are present and populated."""
    assert stac_asset_roles({"roles": ["data"], "role": ["thumbnail"]}) == ("data",)


def test_asset_roles_of_an_asset_claiming_none():
    """No roles at all is empty rather than an error."""
    assert stac_asset_roles({"href": "https://example.invalid/x.nc"}) == ()


@pytest.mark.parametrize(
    ("name", "asset", "expected", "why"),
    [
        pytest.param(
            NETCDF_ASSET_NAME,
            NETCDF_ASSET,
            True,
            "its name is the last segment of its path",
            id="netcdf-file",
        ),
        pytest.param(
            "globus",
            GLOBUS_ASSET,
            False,
            "its path is a directory, ending in a version rather than in 'globus'",
            id="globus-directory",
        ),
        pytest.param(
            "reference_file",
            KERCHUNK_ASSET,
            False,
            "it has no file:local_path at all",
            id="kerchunk-no-local-path",
        ),
    ],
)
def test_whether_an_asset_is_one_file_is_decided_structurally(
    name, asset, expected, why
):
    """
    An asset is one file when its name is the last segment of its `file:local_path`

    Deliberately not a hard-coded list of the non-file asset names we have seen:
    `globus` and `reference_file` are west-only today and the set will grow, so a
    list would quietly mis-classify the next one to appear.
    """
    assert stac_asset_is_one_file(name, asset) is expected, why


def test_a_netcdf_asset_becomes_one_file_and_one_route_to_it():
    """The commonest case: a file, and an HTTPS route serving that file."""
    payload = stac_data_access(feature(**{NETCDF_ASSET_NAME: NETCDF_ASSET}))

    assert len(payload.files) == 1
    stored = payload.files[0]
    assert stored.filename == NETCDF_ASSET_NAME
    assert stored.size == 405577368
    # The multihash prefix is gone and the algorithm is named.
    assert stored.checksum_type == "SHA256"
    assert (
        stored.checksum
        == "42ef6eefb38c02b5138a9e967a7b49c572685f87aa383e780a25b46c1bd89d04"
    )
    assert stored.tracking_id == "hdl:21.14100/007da5e5-12bb-4fe9-8df9-5cd473280650"

    assert len(payload.accesses) == 1
    route = payload.accesses[0]
    assert route.filename == NETCDF_ASSET_NAME
    assert route.service is AccessService.HTTPS
    assert route.container_format is ContainerFormat.NETCDF
    assert route.data_node == "esgf-node.ornl.gov"


def test_the_kerchunk_asset_is_a_whole_version_route_fetched_over_https():
    """
    The route this PR exists for: a virtual store covering the whole dataset version

    `service` and `container_format` disagree here, and that is the whole argument
    for them being two columns -- the sidecar is *fetched* over plain HTTPS, and
    *opened* as a kerchunk reference set. STAC writes the format in `protocol`, so a
    single combined field would have to pick one and lose the other.
    """
    payload = stac_data_access(feature(reference_file=KERCHUNK_ASSET))

    assert payload.files == ()
    route = payload.accesses[0]
    assert route.filename is None
    assert route.service is AccessService.HTTPS
    assert route.container_format is ContainerFormat.KERCHUNK
    assert route.url.endswith(".kerchunk.parq")


def test_the_globus_asset_is_a_whole_version_route_recorded_as_a_web_page():
    """
    Globus's asset is a web-app link to a directory, not a transfer endpoint

    Recording it as `html` is how a reader is told "this is not bytes you can open".
    We keep it rather than dropping it, so that the fact the node offered Globus at
    all is not lost.
    """
    payload = stac_data_access(feature(globus=GLOBUS_ASSET))

    assert payload.files == ()
    route = payload.accesses[0]
    assert route.filename is None
    assert route.service is AccessService.GLOBUS
    assert route.container_format is ContainerFormat.HTML


def test_all_three_kinds_of_asset_together():
    """
    One west CMIP6 item, as it really arrives: one file but three routes

    Two of the three serve the whole version, which is why a route cannot simply be
    a child of a file.
    """
    payload = stac_data_access(
        feature(
            **{NETCDF_ASSET_NAME: NETCDF_ASSET},
            globus=GLOBUS_ASSET,
            reference_file=KERCHUNK_ASSET,
        )
    )

    assert len(payload.files) == 1
    assert len(payload.accesses) == 3
    whole_version = [r for r in payload.accesses if r.filename is None]
    assert {r.container_format for r in whole_version} == {
        ContainerFormat.HTML,
        ContainerFormat.KERCHUNK,
    }


def test_properties_access_is_never_read():
    """
    `properties.access` lies, so only assets that are really there become routes

    Live items advertise `["HTTPServer", "OPENDAP", "GridFTP", "Globus"]` while
    carrying no OPeNDAP or GridFTP asset at all, and east CMIP6 omits the field
    entirely. Believing it would invent two routes that do not exist.
    """
    lying = feature(**{NETCDF_ASSET_NAME: NETCDF_ASSET})
    lying["properties"] = {"access": ["HTTPServer", "OPENDAP", "GridFTP", "Globus"]}

    payload = stac_data_access(lying)

    assert len(payload.accesses) == 1
    assert payload.accesses[0].service is AccessService.HTTPS


def test_an_asset_without_the_data_role_is_skipped():
    """A thumbnail or metadata asset is not a route to the data."""
    payload = stac_data_access(
        feature(thumbnail={**NETCDF_ASSET, "roles": ["thumbnail"]})
    )

    assert payload == stac_data_access(feature())


def test_a_feature_with_no_assets_describes_nothing():
    """Offering no way of getting the data is a fact, not an error."""
    payload = stac_data_access(feature())

    assert payload.files == ()
    assert payload.accesses == ()


def test_an_unknown_protocol_raises_rather_than_being_dropped():
    """
    A route we cannot classify is a route we would never know we had been offered

    So this is loud. The alternative -- skipping it -- would mean a new ESGF access
    protocol silently never reaching the database.
    """
    with pytest.raises(UnknownAccessServiceError, match="s3-but-not-really"):
        stac_data_access(feature(odd={**NETCDF_ASSET, "protocol": "s3-but-not-really"}))


def test_an_unknown_type_raises_rather_than_being_dropped():
    """As for the protocol: a container format we cannot name is not dropped."""
    with pytest.raises(UnknownContainerFormatError, match="application/mystery"):
        stac_data_access(feature(odd={**NETCDF_ASSET, "type": "application/mystery"}))


@pytest.mark.parametrize(
    "media_type",
    [
        pytest.param("application/vnd.zarr", id="bare"),
        pytest.param("application/vnd.zarr; version=3", id="versioned"),
        pytest.param(
            "application/vnd.zarr; version=3; profile=multiscales", id="pyramided"
        ),
        pytest.param("APPLICATION/VND.ZARR", id="upper-case"),
    ],
)
def test_a_zarr_asset_is_classified_whatever_its_media_type_parameters(media_type):
    """
    Nothing publishes a zarr store to us yet, so this is the first thing that will

    STAC's zarr best practices qualify the media type (`; version=3`, and
    `; profile=multiscales` for a pyramided store), so an exact-string match would
    classify none of the real ones -- and because an unclassifiable type raises, it
    would fail the parse of the whole item rather than degrade.
    """
    store = {
        **NETCDF_ASSET,
        "type": media_type,
        "file:local_path": f"{DRS_DIRECTORY}/{NETCDF_ASSET_NAME}",
    }

    payload = stac_data_access(feature(**{NETCDF_ASSET_NAME: store}))

    assert payload.accesses[0].container_format == ContainerFormat.ZARR


def test_an_icechunk_asset_is_refused_until_there_is_a_media_type_to_match():
    """
    Icechunk has no registered media type, so there is nothing to key it by

    [ContainerFormat.ICECHUNK][esmporium.datasets.ContainerFormat.ICECHUNK] exists so
    that the day one is published it is a new row rather than a schema change, but
    classifying an asset needs a name the publisher actually writes. Raising is the
    right answer meanwhile: it is how we find out, rather than dropping a route we
    were offered.
    """
    with pytest.raises(UnknownContainerFormatError, match="icechunk"):
        stac_data_access(
            feature(odd={**NETCDF_ASSET, "type": "application/vnd.icechunk"})
        )


def test_a_cmip7_tracking_id_is_found_under_its_own_prefix():
    """
    STAC prefixes `tracking_id` with the project, so it is matched by suffix

    `cmip6:`, `cmip7:`, `cmip6plus:` and `cordex-cmip6:` all occur. Matching the
    suffix means a project we have not met yet needs no code change.
    """
    cmip7 = {k: v for k, v in NETCDF_ASSET.items() if k != "cmip6:tracking_id"}
    cmip7["cmip7:tracking_id"] = "hdl:21.14107/67513875-6a13-4d94-be61-f4fc967586a4"

    payload = stac_data_access(feature(**{NETCDF_ASSET_NAME: cmip7}))

    assert payload.files[0].tracking_id == (
        "hdl:21.14107/67513875-6a13-4d94-be61-f4fc967586a4"
    )


def test_an_unreadable_checksum_is_kept_and_left_unnamed():
    """A file whose checksum we cannot decode is still a file worth recording."""
    doubled = {**NETCDF_ASSET, "file:checksum": "1220" + "1220" + "ab" * 32}

    payload = stac_data_access(feature(**{NETCDF_ASSET_NAME: doubled}))

    assert payload.files[0].checksum == "1220" + "1220" + "ab" * 32
    assert payload.files[0].checksum_type is None


def test_a_file_with_no_checksum_or_size_is_still_a_file():
    """Not every publisher fills these in, and that is not a reason to drop the file."""
    bare = {
        "href": f"https://example.invalid/{NETCDF_ASSET_NAME}",
        "type": "application/netcdf",
        "roles": ["data"],
        "protocol": "https",
        "alternate:name": "example.invalid",
        "file:local_path": f"CMIP6/x/{NETCDF_ASSET_NAME}",
    }

    payload = stac_data_access(feature(**{NETCDF_ASSET_NAME: bare}))

    stored = payload.files[0]
    assert (stored.size, stored.checksum, stored.checksum_type) == (None, None, None)
    assert stored.filename == NETCDF_ASSET_NAME


def test_the_routes_raw_json_keeps_the_whole_asset():
    """
    Nothing the asset carried is lost to our column choices

    `description`, `created`/`updated` and the Globus `origin_id` are all things we
    do not model but might want later.
    """
    payload = stac_data_access(feature(reference_file=KERCHUNK_ASSET))

    assert json.loads(payload.accesses[0].raw_json) == KERCHUNK_ASSET


# A real CEDA CMIP6 `type=File` document, trimmed to the fields we read.
SOLR_FILE_DOC = {
    "type": "File",
    "title": "ch4_Amon_BCC-ESM1_hist-piNTCF_r1i1p1f1_gn_185001-201412.nc",
    "size": 1232798772,
    "checksum": ["981f71dc16d5e378423d88dbf4ebfc663da28da799702e0bc8168a8115142fb3"],
    "checksum_type": ["SHA256"],
    "tracking_id": ["hdl:21.14100/32ed136f-697a-477c-947f-f3431fb47394"],
    "data_node": ["esgf.ceda.ac.uk"],
    "dataset_id": (
        "CMIP6.AerChemMIP.BCC.BCC-ESM1.hist-piNTCF.r1i1p1f1.Amon.ch4.gn"
        ".v20190621|esgf.ceda.ac.uk"
    ),
    "url": [
        "https://esgf.ceda.ac.uk/thredds/fileServer/x/ch4.nc|application/netcdf|HTTPServer",
        "https://esgf.ceda.ac.uk/thredds/dodsC/x/ch4.nc.html|application/opendap-html|OPENDAP",
    ],
}


@pytest.mark.parametrize(
    ("triple", "expected"),
    [
        pytest.param(
            "https://x.invalid/t.nc|application/netcdf|HTTPServer",
            ("https://x.invalid/t.nc", "application/netcdf", "HTTPServer"),
            id="httpserver",
        ),
        pytest.param(
            "https://x.invalid/t.nc.html|application/opendap-html|OPENDAP",
            ("https://x.invalid/t.nc.html", "application/opendap-html", "OPENDAP"),
            id="opendap",
        ),
        pytest.param(
            # A port and a doubled slash, as live `esgf.nci.org.au` publishes it.
            "gsiftp://esgfdtn.nci.org.au:2811//replica/CMIP6/t.nc"
            "|application/gridftp|GridFTP",
            (
                "gsiftp://esgfdtn.nci.org.au:2811//replica/CMIP6/t.nc",
                "application/gridftp",
                "GridFTP",
            ),
            id="gridftp",
        ),
        pytest.param(
            # One colon and no authority, which is how Solr really writes a Globus
            # route -- live `esgf.nci.org.au` and the 1.5 bridge both do this. It is
            # not `globus://<node>/...` and it is not a URL anything will parse.
            "globus:2058c7d6-a79f-11e6-9ad6-22000a1e3b52/replica/CMIP6/t.nc"
            "|Globus|Globus",
            (
                "globus:2058c7d6-a79f-11e6-9ad6-22000a1e3b52/replica/CMIP6/t.nc",
                "Globus",
                "Globus",
            ),
            id="globus",
        ),
    ],
)
def test_a_solr_url_triple_splits_into_url_mime_and_service(triple, expected):
    """
    All four shapes Solr really publishes, including the two that are not mime types

    Solr writes the service name in the mime slot for GridFTP and Globus, which is
    why the mime lookup has to know `"Globus"` as well as real mime types.
    """
    assert solr_url_triple(triple) == expected


def test_a_url_entry_that_is_not_a_triple_is_refused():
    """Not three parts means we cannot tell what it is a route to, or how to reach it"""
    with pytest.raises(UnreadableURLTripleError, match="does not split"):
        solr_url_triple("https://x.invalid/t.nc|application/netcdf")


def test_a_solr_file_document_becomes_one_file_and_every_route_to_it():
    """
    Solr's unit is the file, so every route it describes names that file

    Solr cannot express a whole-version route at all: its triple has a service slot
    but no container-format slot, and every service its `access` list names
    (`HTTPServer`, `GridFTP`, `OPENDAP`, `Globus`) moves whole files. Virtual and
    remote access is an ESGF-NG capability, so every route
    from here has a filename.
    """
    payload = solr_file_access(SOLR_FILE_DOC)

    assert len(payload.files) == 1
    stored = payload.files[0]
    assert stored.filename == SOLR_FILE_DOC["title"]
    assert stored.size == 1232798772
    # Solr states the algorithm, so there is no multihash to decode.
    assert stored.checksum_type == "SHA256"
    assert stored.checksum == SOLR_FILE_DOC["checksum"][0]
    assert stored.tracking_id == SOLR_FILE_DOC["tracking_id"][0]

    assert len(payload.accesses) == 2
    assert all(route.filename == stored.filename for route in payload.accesses)
    assert all(route.data_node == "esgf.ceda.ac.uk" for route in payload.accesses)

    by_service = {route.service: route for route in payload.accesses}
    assert by_service[AccessService.HTTPS].container_format is ContainerFormat.NETCDF
    # The OPeNDAP URL is a form page for a human, not the DAP endpoint, so it is
    # recorded as what it is and stored exactly as published -- `.nc.html` and all.
    assert by_service[AccessService.OPENDAP].container_format is ContainerFormat.HTML
    assert by_service[AccessService.OPENDAP].url.endswith(".nc.html")


def test_a_solr_routes_raw_json_is_the_triple_it_came_from():
    """
    Solr says nothing about a route except its triple, so the triple is what we keep

    Unlike STAC, there is no per-route object to store, which is the one place the
    two APIs cannot be recorded identically.
    """
    payload = solr_file_access(SOLR_FILE_DOC)

    assert json.loads(payload.accesses[0].raw_json) == SOLR_FILE_DOC["url"][0]


def test_a_solr_file_document_with_no_checksum_is_still_a_file():
    """Not every publisher fills these in, and that is no reason to drop the file"""
    bare = {
        k: v
        for k, v in SOLR_FILE_DOC.items()
        if k not in ("checksum", "checksum_type", "tracking_id", "size")
    }

    payload = solr_file_access(bare)

    stored = payload.files[0]
    assert (stored.size, stored.checksum, stored.checksum_type) == (None, None, None)
    assert stored.filename == SOLR_FILE_DOC["title"]


def test_an_unknown_solr_service_is_refused():
    """As on the STAC side: a route we cannot classify is not silently dropped"""
    odd = {
        **SOLR_FILE_DOC,
        "url": ["https://x.invalid/t.nc|application/netcdf|TeleportService"],
    }

    with pytest.raises(ValueError, match="TeleportService"):
        solr_file_access(odd)


def test_a_solr_mime_type_is_recorded_but_not_believed():
    """
    Solr's mime slot is not trustworthy, so the container format comes from the service

    This is a real CEDA CMIP5 entry: `text/html` as the mime type of a `.nc4` file on
    an HTTPServer route. Believing it would record a netCDF file as a web page.
    Reading the service instead is also the truer model of ESGF-1, which has no
    container-format axis at all -- its whole access vocabulary is `HTTPServer` and
    `OPENDAP`, with no way to describe a virtual or remote store.

    The mime type is not thrown away, it is just not acted on: it stays in the
    triple, which is what `raw_json` keeps.
    """
    lying = {
        **SOLR_FILE_DOC,
        "url": [
            "https://esgf.ceda.ac.uk/thredds/fileServer/x/evspsbl.nc4"
            "|text/html|HTTPServer"
        ],
    }

    payload = solr_file_access(lying)

    route = payload.accesses[0]
    assert route.service is AccessService.HTTPS
    assert route.container_format is ContainerFormat.NETCDF
    assert "text/html" in route.raw_json
