"""
Test our parsing against responses the live APIs really sent

The other unit tests parse responses we wrote ourselves,
which pins our behaviour but cannot tell us
whether we understood the API's shape correctly in the first place.
These parse recordings of real answers, and so can, without a network connection.

The recordings go stale.
That is the trade: they will not notice an API changing shape until they are
refreshed. Refresh them with `uv run python scripts/record_search_responses.py`
and read the diff.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from esmporium.datasets import AccessService, ContainerFormat
from esmporium.query import facet_spec
from esmporium.search import (
    ESGF1_CMIP5_FACADE_PARAMETERS,
    ESGF1_CMIP6_FACADE_PARAMETERS,
    ESGFNG_CMIP6_FACADE_PARAMETERS,
    ESGFNG_CMIP7_FACADE_PARAMETERS,
    DatasetFacets,
    ESGFNGResultParser,
    SearchAPIESGF1Solr,
    SearchAPIESGF15BridgeSolr,
    SearchAPIESGFNGSTAC,
    SearchAPIFacade,
    SolrSingleRowResultParser,
    SolrVariableBundleResultParser,
    build_transient_retrying,
    get_mapping_to_query_style_facet_names,
)
from esmporium.search.apis.esgf1 import solr_file_access

RECORDED_DIR = Path(__file__).parents[2] / "test-data" / "search"
"""Where the recorded responses live"""

PROBE_FACETS = {"variable", "reporting_interval", "model"}
"""
A few facets every project has, to check the values by eye against

`tas` is the probe because every project publishes it,
and it is what the recorded query asked for.
"""


def facade(
    parameters,
    search_api_cls,
    result_parser,
    host="recorded.example",
) -> SearchAPIFacade:
    """
    Build a facade for parsing a recording

    The host and retry policy are irrelevant here (nothing is sent),
    so any values will do.

    The result parser is not: it is picked for the project and the endpoint,
    so each case below has to name the one the real facade would use.
    """
    return SearchAPIFacade(
        parameters=parameters,
        search_api=search_api_cls(host, build_transient_retrying(1)),
        result_parser=result_parser,
    )


def every_facet(facade: SearchAPIFacade) -> set[str]:
    """
    Get every facet a facade's query style can express

    This has to match `facets_to_list` in `scripts/record_search_responses.py`:
    asking here for something the recording never asked the API about
    would only prove that it is not in the file.

    Parameters
    ----------
    facade
        The facade whose query style to read

    Returns
    -------
    :
        The facets it can express, named the way they are asked for
    """
    return set(facet_spec(facade.parameters.base_query_style).expressible_facets)


RECORDED_CASES = (
    pytest.param(
        "esgf1-solr-cmip5",
        facade(
            ESGF1_CMIP5_FACADE_PARAMETERS,
            SearchAPIESGF1Solr,
            SolrVariableBundleResultParser(),
        ),
        id="esgf1-solr-cmip5",
    ),
    pytest.param(
        "esgf1-solr-cmip6",
        facade(
            ESGF1_CMIP6_FACADE_PARAMETERS,
            SearchAPIESGF1Solr,
            SolrSingleRowResultParser(),
        ),
        id="esgf1-solr-cmip6",
    ),
    pytest.param(
        "esgf15-bridge-cmip6",
        facade(
            ESGF1_CMIP6_FACADE_PARAMETERS,
            SearchAPIESGF15BridgeSolr,
            SolrSingleRowResultParser(),
        ),
        id="esgf15-bridge-cmip6",
    ),
    pytest.param(
        "esgf-ng-stac-cmip6-east",
        facade(
            ESGFNG_CMIP6_FACADE_PARAMETERS,
            SearchAPIESGFNGSTAC,
            ESGFNGResultParser(),
        ),
        id="esgf-ng-stac-cmip6-east",
    ),
    pytest.param(
        "esgf-ng-stac-cmip6-west",
        facade(
            ESGFNG_CMIP6_FACADE_PARAMETERS,
            SearchAPIESGFNGSTAC,
            ESGFNGResultParser(),
        ),
        id="esgf-ng-stac-cmip6-west",
    ),
    pytest.param(
        "esgf-ng-stac-cmip7-east",
        facade(
            ESGFNG_CMIP7_FACADE_PARAMETERS,
            SearchAPIESGFNGSTAC,
            ESGFNGResultParser(),
        ),
        id="esgf-ng-stac-cmip7-east",
    ),
    pytest.param(
        "esgf-ng-stac-cmip7-west",
        facade(
            ESGFNG_CMIP7_FACADE_PARAMETERS,
            SearchAPIESGFNGSTAC,
            ESGFNGResultParser(),
        ),
        id="esgf-ng-stac-cmip7-west",
    ),
)
"""Each recording, with the facade which asked for it"""


def load(name):
    """
    Load a recorded response

    Parameters
    ----------
    name
        The name it was recorded under

    Returns
    -------
    :
        The recorded response
    """
    path = RECORDED_DIR / f"{name}.json"
    assert path.exists(), (
        f"No recording at {path}. "
        "Record it with `uv run python scripts/record_search_responses.py`."
    )

    return json.loads(path.read_text())


@pytest.mark.parametrize("name, facade", RECORDED_CASES)
def test_result_count_of_a_recorded_search(name, facade):
    """Test that we can count the matches in a response an API really sent"""
    raw = load(f"{name}-search")

    assert facade.search_api.get_n_matches(raw) > 0


SOLR_RECORDED_CASES = tuple(c for c in RECORDED_CASES if "stac" not in str(c.id))
"""The recorded cases whose API pages by offset"""


@pytest.mark.parametrize("name, facade", SOLR_RECORDED_CASES)
def test_recorded_solr_next_page_request_advances_the_offset(name, facade):
    """On a real Solr answer with more matches than one page, we ask for the next"""
    raw = load(f"{name}-search")
    api = facade.search_api

    # The recordings hold far more matches than the small page they returned, so
    # there is always a next page to ask for.
    request = api.build_search_request({}, limit=2)
    nxt = api.next_page_request(request, raw)

    assert nxt is not None
    assert nxt.params["offset"] == 2


CMIP5_RECORDED_CASES = tuple(c for c in RECORDED_CASES if "cmip5" in str(c.id))
"""The recorded cases whose project bundles many variables into one document"""

NON_CMIP5_RECORDED_CASES = tuple(c for c in RECORDED_CASES if "cmip5" not in str(c.id))
"""The recorded cases whose documents are already one dataset each"""


@pytest.mark.parametrize("name, facade", RECORDED_CASES)
def test_parse_search_results_are_well_formed(name, facade):
    """Test the shape of the datasets we parse out of a response an API really sent"""
    raw = load(f"{name}-search")

    documents = facade.parse_search_results(raw)

    assert documents, "no documents were parsed from a non-empty search"
    for document in documents:
        assert document.id_project_specific
        assert document.version
        assert document.esgf_doc_id
        assert document.nodes, "a document was parsed with nowhere to fetch it from"
        assert document.datasets, "a document mapped to no dataset rows"
        for row in document.datasets:
            assert isinstance(row, DatasetFacets), (
                "a dataset row is not the typed DatasetFacets we parse into"
            )
            # grid_label is legitimately NULL (CMIP5); every other facet must be set.
            for column, value in row.model_dump().items():
                if column == "grid_label":
                    continue
                assert value, f"{column} was parsed empty"


@pytest.mark.parametrize("name, facade", CMIP5_RECORDED_CASES)
def test_cmip5_document_explodes_into_variables_sharing_one_version(name, facade):
    """A CMIP5 record bundles many variables sharing one project specific id and version"""  # noqa: E501
    raw = load(f"{name}-search")

    documents = facade.parse_search_results(raw)

    for document in documents:
        variables = [row.variable for row in document.datasets]
        assert len(variables) > 1, "a CMIP5 bundle should carry many variables"
        assert len(set(variables)) == len(variables), "a variable was repeated"

        # Everything but the variable is shared across the bundle's rows...
        without_variable = [
            row.model_dump(exclude={"variable"}) for row in document.datasets
        ]
        assert all(row == without_variable[0] for row in without_variable)
        # ...and CMIP5 has no grid label.
        assert without_variable[0]["grid_label"] is None


@pytest.mark.parametrize("name, facade", NON_CMIP5_RECORDED_CASES)
def test_non_cmip5_document_is_a_single_dataset(name, facade):
    """A CMIP6/CMIP7 document maps to exactly one dataset row"""
    raw = load(f"{name}-search")

    documents = facade.parse_search_results(raw)

    for document in documents:
        assert len(document.datasets) == 1


STAC_RECORDED_CASES = tuple(case for case in RECORDED_CASES if "stac" in str(case.id))
"""The recorded cases whose API describes its facet values in a STAC collection"""


@pytest.mark.parametrize("name, facade", RECORDED_CASES)
def test_recorded_rows_carry_the_project_they_were_asked_for(name, facade):
    """Every dataset row says which project it belongs to, and says the right one

    Which field carries the project is the parser's business and it varies
    (a Solr `project` facet, a STAC feature's top-level `collection` key),
    so it is worth pinning the value rather than only its shape.
    """
    raw = load(f"{name}-search")
    (expected,) = [p for p in ("CMIP5", "CMIP6", "CMIP7") if p.lower() in name]

    documents = facade.parse_search_results(raw)

    assert documents
    assert {row.project for doc in documents for row in doc.datasets} == {expected}


@pytest.mark.parametrize("name, facade", STAC_RECORDED_CASES)
def test_recorded_stac_id_project_specific_is_read_from_title(name, facade):
    """Every STAC deployment publishes the project specific id in `properties.title`"""
    raw = load(f"{name}-search")

    documents = facade.parse_search_results(raw)

    assert documents
    assert [doc.id_project_specific for doc in documents] == [
        feature["properties"]["title"] for feature in raw["features"]
    ]
    # The document still remembers which version it came from.
    assert [doc.esgf_doc_id for doc in documents] == [
        feature["id"] for feature in raw["features"]
    ]


@pytest.mark.parametrize("name, facade", STAC_RECORDED_CASES)
def test_recorded_stac_next_page_request_follows_the_recorded_token(name, facade):
    """A real STAC answer's `next` link is resent verbatim to page onwards

    The recordings were captured with a small page, so each carries a `next` link
    with a continuation token: this pins that we resend exactly that request.
    """
    raw = load(f"{name}-search")
    api = facade.search_api

    (next_link,) = [link for link in raw["links"] if link.get("rel") == "next"]

    nxt = api.next_page_request(api.build_search_request({}, limit=2), raw)

    assert nxt is not None
    assert nxt.method == "POST"
    assert nxt.path == "/search"
    # The whole body, continuation token and all, is what we send next.
    assert nxt.json_body == next_link["body"]


@pytest.mark.parametrize("name, facade", RECORDED_CASES)
def test_parse_facet_values_of_a_recorded_response(name, facade):
    """Test that we can read the facet values out of a response an API really sent"""
    raw = load(f"{name}-facets")

    res = facade.parse_facet_values(raw, PROBE_FACETS)

    assert set(res) <= PROBE_FACETS, "we were told about a facet we did not ask about"
    assert "tas" in res["variable"]
    assert res["model"]
    assert res["reporting_interval"]


@pytest.mark.parametrize("name, facade", RECORDED_CASES)
def test_recorded_facet_values_are_well_formed(name, facade):
    """
    Test the shape of what we hand back, on real data

    Every facet the query style can express is asked about, so this covers the
    facets which the APIs describe in ways that are not a list of values,
    as well as those they do. It also covers the query-style-specific names
    (`product` on CMIP5, and so on), which are the ones we guessed at.

    A facet we report has to have at least one value:
    reporting a facet with nothing in it would be read as
    "this facet has no valid values", which is never what we mean.
    """
    raw = load(f"{name}-facets")
    facets = every_facet(facade)

    res = facade.parse_facet_values(raw, facets)

    assert res, "no facet was reported at all"
    assert set(res) <= facets, "we were told about a facet we did not ask about"
    for facet, values in res.items():
        assert values, f"{facet} was reported with no values"
        assert all(isinstance(value, str) and value for value in values), (
            f"{facet} was reported with a value which is not a non-empty string"
        )


@pytest.mark.parametrize("name, facade", STAC_RECORDED_CASES)
def test_recorded_facets_which_are_not_enumerated_are_left_out(name, facade):
    """
    Test that a facet the API describes without listing its values is left out

    A STAC collection summarises some facets as a regular expression or as a
    range rather than as a list, because their values are generated rather than
    chosen from a vocabulary (`variant_label` is the standing example).

    What counts as "not enumerated" is read out of the recording itself rather
    than hard coded, so this keeps testing the right thing
    if the API starts listing something it used to describe as a pattern.
    """
    raw = load(f"{name}-facets")
    facets = every_facet(facade)

    res = facade.parse_facet_values(raw, facets)

    prefix = f"{facade.parameters.prefix}:"

    asked_for = {
        native: asked
        for asked, native in get_mapping_to_query_style_facet_names(
            facade.parameters.base_query_style, facets
        ).items()
    }
    not_enumerated = {
        asked
        for property_name, summary in raw["summaries"].items()
        if property_name.startswith(prefix)
        and (asked := asked_for.get(property_name[len(prefix) :])) is not None
        and not (
            isinstance(summary, list)
            and any(isinstance(value, str) for value in summary)
        )
    }

    assert not_enumerated, (
        "this recording enumerates every facet, so it cannot test that "
        "a non-enumerated one is left out"
    )
    assert not_enumerated.isdisjoint(res), (
        "a facet the collection did not enumerate was reported as having values"
    )


@pytest.mark.parametrize("name, facade", STAC_RECORDED_CASES)
def test_recorded_variant_label_is_summarised_as_a_pattern(name, facade):
    """
    Test that the generated identifier we build on really is described as a pattern

    `variant_label` being a pattern rather than a list is the reason the value
    checker treats it differently from a controlled vocabulary,
    so it is worth pinning against real data rather than only against
    a collection we wrote ourselves.
    """
    raw = load(f"{name}-facets")

    (native,) = get_mapping_to_query_style_facet_names(
        facade.parameters.base_query_style, {"variant_label"}
    ).values()
    summary = raw["summaries"][f"{facade.parameters.prefix}:{native}"]

    assert isinstance(summary, str), (
        f"{native} was summarised as a {type(summary).__name__}, not a pattern"
    )
    re.compile(summary)


@pytest.mark.parametrize("name, facade", STAC_RECORDED_CASES)
def test_summary_facet_keys_have_not_drifted_in_case(name, facade):
    """
    Test that no summary key differs from ours only by case

    We match summary keys exactly (see `stac_summary_values`): we build
    lowercase-prefixed names like `cmip7:variable_id` and look for that string.
    If a deployment renamed a key only by case (say `CMIP7:variable_id`), the
    exact match would miss it and the facet would be dropped silently, read
    downstream as "this facet has no enumerable values" rather than as an error.
    """
    raw = load(f"{name}-facets")
    facets = every_facet(facade)
    api_names = set(facade.parameters.get_mapping_to_api_facet_names(facets).values())

    summary_keys = set(raw["summaries"])
    summary_keys_by_lower = {key.lower(): key for key in summary_keys}

    drifted = {
        api_name: summary_keys_by_lower[api_name.lower()]
        for api_name in api_names
        # The exact key we look for is gone,
        if api_name not in summary_keys
        # but a differently-cased one is there, so its values would vanish.
        and api_name.lower() in summary_keys_by_lower
    }

    assert not drifted, (
        "these facet keys are present only under a different case, so the "
        f"parser's exact match would drop them silently: {drifted}. "
        "A deployment changed the case of a facet key; update the prefix or "
        "name mapping to match, or the facet's values disappear without an error."
    )


@pytest.mark.parametrize("name, facade", STAC_RECORDED_CASES)
def test_recorded_stac_search_carries_one_file_per_netcdf_asset(name, facade):
    """
    ESGF-NG needs no second request for file access: the item's assets are the files

    This is the whole asymmetry with Solr, pinned against real answers from all four
    live STAC deployments.
    """
    raw = load(f"{name}-search")

    documents = facade.parse_search_results(raw)

    assert documents
    for document, feature in zip(documents, raw["features"], strict=True):
        netcdf_assets = {
            name
            for name, asset in feature["assets"].items()
            if asset.get("type") == "application/netcdf"
        }
        assert {found.filename for found in document.files} == netcdf_assets
        # Every file also has at least one way of being got.
        assert netcdf_assets <= {
            route.filename for route in document.accesses if route.filename
        }


@pytest.mark.parametrize("name, facade", STAC_RECORDED_CASES)
def test_recorded_stac_checksums_all_decode(name, facade):
    """
    Every real netCDF asset publishes a checksum we can name the algorithm of

    If a deployment ever starts publishing a multihash code we do not know, this is
    where it surfaces -- the value would still be stored, but unnamed, and silently
    uncomparable with the Solr answer for the same file.
    """
    raw = load(f"{name}-search")

    documents = facade.parse_search_results(raw)

    checksums = [found for doc in documents for found in doc.files]
    assert checksums
    assert all(found.checksum_type == "SHA256" for found in checksums)
    assert all(found.size and found.size > 0 for found in checksums)


def test_the_recorded_west_cmip6_item_offers_virtual_and_globus_access():
    """
    West CMIP6 is the one recorded deployment publishing whole-version routes

    It is the evidence that the non-file branch of the model is needed now rather
    than someday: a kerchunk reference set covering the whole dataset version, and a
    Globus web-app link to the directory, in the same `assets` dict as the files.

    East CMIP6 publishes neither, which is why this is not parametrised over the STAC
    cases -- the asymmetry between the two mirrors is the finding.
    """
    facade_for_west = next(
        case.values[1]
        for case in STAC_RECORDED_CASES
        if case.id == "esgf-ng-stac-cmip6-west"
    )

    documents = facade_for_west.parse_search_results(
        load("esgf-ng-stac-cmip6-west-search")
    )

    whole_version = [
        route
        for document in documents
        for route in document.accesses
        if route.filename is None
    ]
    assert whole_version
    by_format = {route.container_format: route for route in whole_version}

    kerchunk = by_format[ContainerFormat.KERCHUNK]
    # Fetched over HTTPS, opened as kerchunk: the reason these are two columns.
    assert kerchunk.service is AccessService.HTTPS
    # Parquet, not the JSON kerchunk is more usually written as.
    assert kerchunk.url.endswith(".kerchunk.parq")

    assert by_format[ContainerFormat.HTML].service is AccessService.GLOBUS


def test_the_recorded_west_cmip6_item_advertises_access_it_does_not_offer():
    """
    `properties.access` lies on real data, which is why we never read it

    This recorded item advertises `OPENDAP` and `GridFTP` in `properties.access` and
    carries no asset for either. Believing the advertisement would invent two routes
    that do not exist; reading only the assets records the three that do.
    """
    raw = load("esgf-ng-stac-cmip6-west-search")
    facade_for_west = next(
        case.values[1]
        for case in STAC_RECORDED_CASES
        if case.id == "esgf-ng-stac-cmip6-west"
    )

    feature = raw["features"][0]
    advertised = set(feature["properties"]["access"])
    assert {"OPENDAP", "GridFTP"} <= advertised, (
        "this test is only meaningful while the recording still over-advertises; "
        "if ESGF has fixed `properties.access`, re-record and retire this test"
    )

    document = facade_for_west.parse_search_results(raw)[0]

    recorded = {route.service for route in document.accesses}
    assert AccessService.OPENDAP not in recorded
    assert AccessService.GRIDFTP not in recorded
    assert recorded == {AccessService.HTTPS, AccessService.GLOBUS}


@pytest.mark.parametrize("name, facade", STAC_RECORDED_CASES)
def test_recorded_stac_records_one_route_per_data_asset(name, facade):
    """
    Every data asset becomes exactly one route, and nothing else does

    The count is the check: an asset silently dropped, or a route invented from
    somewhere other than an asset, both show up here.
    """
    raw = load(f"{name}-search")

    documents = facade.parse_search_results(raw)

    assert documents
    for document, feature in zip(documents, raw["features"], strict=True):
        data_assets = [
            asset
            for asset in feature["assets"].values()
            if "data" in (asset.get("roles") or asset.get("role") or [])
        ]
        assert len(document.accesses) == len(data_assets)
        assert {route.url for route in document.accesses} == {
            asset["href"] for asset in data_assets
        }


SOLR_FILE_RECORDED_CASES = tuple(
    case for case in SOLR_RECORDED_CASES if "bridge" not in str(case.id)
)
"""
The Solr cases with a `-files` recording beside their `-search` one

The ESGF-1.5 bridge has none: its `type=File` answers take minutes, which is too
slow to put in the way of a refresh. See `FILE_CASES` in
`scripts/record_search_responses.py`.
"""


@pytest.mark.parametrize("name, facade", SOLR_FILE_RECORDED_CASES)
def test_a_recorded_solr_file_response_parses(name, facade):
    """
    Test that we can read a `type=File` answer an API really sent

    This is the request ESGF-NG does not need: a Solr dataset document publishes no
    per-file URLs, so these documents are the only place they come from.
    """
    docs = facade.search_api.extract_result_documents(load(f"{name}-files"))

    assert docs
    for doc in docs:
        payload = solr_file_access(doc)

        assert len(payload.files) == 1
        assert payload.files[0].filename
        assert payload.accesses
        # ESGF-1 has no way of expressing a whole-version route, so every route
        # a File document describes names the file it is about.
        assert all(
            route.filename == payload.files[0].filename for route in payload.accesses
        )


@pytest.mark.parametrize("name, facade", SOLR_FILE_RECORDED_CASES)
def test_a_recorded_file_documents_dataset_id_is_a_recorded_dataset_documents_id(
    name, facade
):
    """
    The join the whole Solr file step rests on, pinned against real answers

    `record_solr_file_access` places each answer by looking its `dataset_id` up in
    the batch it asked for, which only works because a File document's `dataset_id`
    is character-for-character the `id` the dataset document published --
    `<instance_id>|<data_node>`, node suffix and all. If ESGF ever changed either
    spelling, every file would land nowhere and this is where it would be caught.
    """
    dataset_ids = {
        doc["id"]
        for doc in facade.search_api.extract_result_documents(load(f"{name}-search"))
    }
    file_docs = facade.search_api.extract_result_documents(load(f"{name}-files"))

    assert file_docs
    assert {doc["dataset_id"] for doc in file_docs} <= dataset_ids


@pytest.mark.parametrize("name, facade", SOLR_FILE_RECORDED_CASES)
def test_a_recorded_dataset_response_says_nothing_about_files(name, facade):
    """
    The asymmetry this PR works around, pinned on real data

    A Solr dataset document has `access`, `size` and `number_of_files` -- enough to
    know a dataset *can* be had and how big it is -- and no URL you can fetch. The
    day ESGF-1 starts publishing per-file URLs at dataset level, the second request
    becomes unnecessary, and this test is how we would find out.
    """
    docs = facade.search_api.extract_result_documents(load(f"{name}-search"))

    assert docs
    for doc in docs:
        assert "url" not in doc
        assert doc["access"]
        assert doc["number_of_files"]

    # Whereas the File documents are nothing but.
    file_docs = facade.search_api.extract_result_documents(load(f"{name}-files"))
    assert all(doc["url"] for doc in file_docs)
