"""
Test the result parsers, i.e. the reading of a search answer

These never touch the network: each one hands a parser a response or a document we
wrote ourselves. What they pin is the half of result reading that varies with the
*project and endpoint* rather than with the response format -- which is exactly why
these parsers exist:

- where the project specific id is written
- where the project is written (a Solr facet, a top-level STAC `collection` key)
- how many dataset rows one document maps to

Parsing of real recorded responses is in `test_recorded_responses.py`; this file is the
controlled counterpart to it.
"""

from __future__ import annotations

import re

import pytest

from esmporium.search import (
    ESGF1_CMIP5_FACADE_PARAMETERS,
    ESGF1_CMIP6_FACADE_PARAMETERS,
    ESGFNG_CMIP6_FACADE_PARAMETERS,
    ESGFNG_CMIP7_FACADE_PARAMETERS,
    DatasetFacets,
    ESGFNGResultParser,
    MissingResultFieldError,
    MultipleFacetValuesError,
    SearchAPIESGF1Solr,
    SearchAPIESGFNGSTAC,
    SolrSingleRowResultParser,
    SolrVariableBundleResultParser,
    UnreadableResponseError,
    build_transient_retrying,
)


def esgfng_parser() -> ESGFNGResultParser:
    """An ESGF-NG parser

    One parser serves every project on this API, and every deployment of it too.
    """
    return ESGFNGResultParser()


CMIP6_ROW = DatasetFacets(
    id_project_specific="CMIP6.CMIP.CSIRO.ACCESS-CM2.historical.r1i1p1f1.Amon.tas.gn",
    project="CMIP6",
    model="ACCESS-CM2",
    institution="CSIRO",
    experiment="historical",
    variant_label="r1i1p1f1",
    variable="tas",
    reporting_interval="mon",
    grid_label="gn",
    processing_id="Amon",
)
"""One CMIP6 dataset row, used to build the documents that should parse back to it"""

CMIP7_ROW = CMIP6_ROW.model_copy(
    update={
        "id_project_specific": "MIP-DRS7.CMIP7.CMIP.CSIRO.ACCESS-CM2.historical"
        ".r1i1p1f1.glb.mon.tas.tavg-h2m-hxy-u.g110",
        "project": "CMIP7",
        "processing_id": "tavg-h2m-hxy-u",
    }
)
"""The same, as CMIP7 writes it"""


def solr_api():
    """A Solr-shaped API whose retry policy never sleeps (nothing is sent)

    The ESGF1.5 bridge answers in the same format, so the same parsers read it; the
    store is what pairs each endpoint with its parser.
    """
    return SearchAPIESGF1Solr("node.example", build_transient_retrying(1))


def stac_api():
    """A STAC-shaped API whose retry policy never sleeps (nothing is sent)"""
    return SearchAPIESGFNGSTAC("search.example.io", build_transient_retrying(1))


def facet_fields(parameters):
    """The API field name of every facet column except the ones a parser works out"""
    return parameters.get_mapping_to_api_facet_names(
        set(DatasetFacets.model_fields) - {"id_project_specific", "project"}
    )


def solr_doc(parameters, row: DatasetFacets, **overrides) -> dict:
    """Build a Solr record which should parse back to `row`

    Solr writes its facets as single-element lists at the top level, its project
    specific id as `master_id` and its project as a facet like any other.
    """
    doc = {
        "master_id": [row.id_project_specific],
        "id": [f"{row.id_project_specific}.v20200623|node.example"],
        "version": ["20200623"],
        "latest": [True],
        "retracted": [False],
        "data_node": ["node.example"],
        "project": [row.project],
    }
    for column, api_field in facet_fields(parameters).items():
        value = getattr(row, column)
        if value is not None:
            doc[api_field] = [value]

    return {**doc, **overrides}


def stac_feature(
    parameters,
    row: DatasetFacets,
    feature_id: str,
    collection: str | None = None,
    **prop_overrides,
):
    """
    Build a STAC feature which should parse back to `row`
    """
    props: dict = {
        "version": "20200623",
        "latest": True,
        "retracted": False,
    }
    for column, api_field in facet_fields(parameters).items():
        value = getattr(row, column)
        if value is not None:
            props[api_field] = value

    res = {
        "id": feature_id,
        "properties": {**props, **prop_overrides},
        "assets": {"data": {"alternate:name": "node.example"}},
    }
    if collection is not None:
        res["collection"] = collection

    return res


def test_stac_project_is_read_from_collection():
    feature = stac_feature(
        ESGFNG_CMIP6_FACADE_PARAMETERS,
        CMIP6_ROW,
        f"{CMIP6_ROW.id_project_specific}.v20200623",
        title=CMIP6_ROW.id_project_specific,
        collection="CMIP6",
    )

    (row,) = esgfng_parser().get_dataset_rows(
        feature, api=stac_api(), facade_parameters=ESGFNG_CMIP6_FACADE_PARAMETERS
    )

    assert row == CMIP6_ROW


@pytest.mark.parametrize(
    "parameters, feature",
    (
        pytest.param(ESGFNG_CMIP6_FACADE_PARAMETERS, "cmip6", id="cmip6-stac"),
        pytest.param(ESGFNG_CMIP7_FACADE_PARAMETERS, "cmip7", id="cmip7-stac"),
    ),
)
def test_a_stac_feature_with_no_project_raises(parameters, feature):
    """
    A row we cannot say the project of is not one we can quietly store
    """
    row = CMIP6_ROW if feature == "cmip6" else CMIP7_ROW
    doc = stac_feature(
        parameters,
        row,
        f"{row.id_project_specific}.v20200623",
        title=row.id_project_specific,
    )

    with pytest.raises(
        UnreadableResponseError,
        match=re.escape(
            "This response does not carry the project this record belongs to. "
            "We expected to read the project this record belongs to from "
            "'collection', but 'collection' is not in the response's top level, "
            "there is only: 'assets', 'id', 'properties'. This response came from "
            "SearchAPIESGFNGSTAC at https://search.example.io."
        ),
    ):
        esgfng_parser().get_dataset_rows(
            doc, api=stac_api(), facade_parameters=parameters
        )


def test_a_stac_feature_with_no_title_raises():
    doc = stac_feature(
        ESGFNG_CMIP6_FACADE_PARAMETERS,
        CMIP6_ROW,
        f"{CMIP6_ROW.id_project_specific}.v20200623",
        collection="CMIP6",
    )

    with pytest.raises(
        UnreadableResponseError,
        match=re.escape(
            "This response does not carry the project specific id. "
            "We expected to read the project specific id from 'properties.title', "
            "but 'title' is not in 'properties', there is only: "
        ),
    ):
        esgfng_parser().get_dataset_rows(
            doc, api=stac_api(), facade_parameters=ESGFNG_CMIP6_FACADE_PARAMETERS
        )


def test_a_solr_record_with_no_project_raises():
    """The same for Solr, where the project is a facet the record simply lacks"""
    doc = solr_doc(ESGF1_CMIP6_FACADE_PARAMETERS, CMIP6_ROW)
    del doc["project"]

    with pytest.raises(
        MissingResultFieldError,
        match=re.escape(
            f"The document with id {CMIP6_ROW.id_project_specific!r} carries no value "
            "for 'project'"
        ),
    ):
        SolrSingleRowResultParser().get_dataset_rows(
            doc, api=solr_api(), facade_parameters=ESGF1_CMIP6_FACADE_PARAMETERS
        )


def test_a_solr_cmip5_record_explodes_into_one_row_per_variable():
    """CMIP5 bundles many variables into one record; every other facet is shared"""
    row = DatasetFacets(
        id_project_specific="cmip5.output1.CSIRO-BOM.ACCESS1-0.rcp45.mon.atmos.Amon.r1i1p1",
        project="CMIP5",
        model="ACCESS1-0",
        institution="CSIRO-BOM",
        experiment="rcp45",
        variant_label="r1i1p1",
        variable="tas",
        reporting_interval="mon",
        grid_label=None,
        processing_id="Amon",
    )
    doc = solr_doc(ESGF1_CMIP5_FACADE_PARAMETERS, row, variable=["tas", "pr"])

    rows = SolrVariableBundleResultParser().get_dataset_rows(
        doc, api=solr_api(), facade_parameters=ESGF1_CMIP5_FACADE_PARAMETERS
    )

    assert rows == (row, row.model_copy(update={"variable": "pr"}))
    # CMIP5 models no grid, so the parser states the absence explicitly as None.
    assert all(parsed.grid_label is None for parsed in rows)


def test_a_single_row_parser_will_not_quietly_split_a_bundle():
    """A record carrying several variables where we expect one is a loud failure

    Silently taking the first (or stringifying the list) would put a value in the
    database that no one published.
    """
    doc = solr_doc(ESGF1_CMIP6_FACADE_PARAMETERS, CMIP6_ROW, variable_id=["tas", "pr"])

    with pytest.raises(MultipleFacetValuesError):
        SolrSingleRowResultParser().get_dataset_rows(
            doc, api=solr_api(), facade_parameters=ESGF1_CMIP6_FACADE_PARAMETERS
        )
