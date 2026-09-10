"""
Test the result parsers, i.e. the reading of a search answer

These never touch the network: each one hands a parser a response or a document we
wrote ourselves. What they pin is the half of result reading that varies with the
*project and endpoint* rather than with the response format -- which is exactly why
these parsers exist:

- where the bundle id is written (CMIP6 STAC says it outright, CMIP7 STAC does not)
- where the project is written (a Solr facet, `cmip6:mip_era`, a `project` property)
- how many dataset rows one document maps to
- where the endpoint puts the number of records that matched

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
    ESGFNGCMIP6ResultParser,
    ESGFNGCMIP7ResultParser,
    MissingResultFieldError,
    MultipleFacetValuesError,
    NoSearchResultNumberOfMatchesReturnedError,
    SearchAPIESGF1Solr,
    SearchAPIESGFNGSTAC,
    SolrSingleRowResultParser,
    SolrVariableBundleResultParser,
    build_transient_retrying,
)

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

    Solr writes its facets as single-element lists at the top level, its bundle id as
    `master_id` and its project as a facet like any other.
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


def stac_feature(parameters, row: DatasetFacets, feature_id: str, **prop_overrides):
    """Build a STAC feature which should parse back to `row`

    The project is left to the caller: where it is written is the very thing the two
    ESGF-NG parsers disagree about.
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

    return {
        "id": feature_id,
        "properties": {**props, **prop_overrides},
        "assets": {"data": {"alternate:name": "node.example"}},
    }


def test_cmip6_stac_bundle_id_is_read_from_base_id():
    """CMIP6 features say what their bundle id is, so we do not infer one

    The `base_id` here deliberately disagrees with "the feature id minus its version
    token", so a parser inferring the id instead of reading it fails this.
    """
    feature = stac_feature(
        ESGFNG_CMIP6_FACADE_PARAMETERS,
        CMIP6_ROW,
        "CMIP6.something.else.entirely.v20200623",
        base_id=CMIP6_ROW.id_project_specific,
        **{"cmip6:mip_era": "CMIP6"},
    )

    (document,) = ESGFNGCMIP6ResultParser().parse_search_results(
        {"features": [feature]},
        api=stac_api(),
        facade_parameters=ESGFNG_CMIP6_FACADE_PARAMETERS,
    )

    assert document.id_project_specific == CMIP6_ROW.id_project_specific
    # The edition it actually came from is still remembered.
    assert document.esgf_doc_id == "CMIP6.something.else.entirely.v20200623"


def test_cmip7_stac_bundle_id_drops_the_version_token():
    """CMIP7 features carry no `base_id`, so the bundle id is recovered from the id"""
    feature = stac_feature(
        ESGFNG_CMIP7_FACADE_PARAMETERS,
        CMIP7_ROW,
        f"{CMIP7_ROW.id_project_specific}.v20200623",
        project="CMIP7",
    )

    (document,) = ESGFNGCMIP7ResultParser().parse_search_results(
        {"features": [feature]},
        api=stac_api(),
        facade_parameters=ESGFNG_CMIP7_FACADE_PARAMETERS,
    )

    assert document.id_project_specific == CMIP7_ROW.id_project_specific


def test_cmip7_stac_bundle_id_of_an_id_with_no_version_is_left_alone():
    """Only a real version token is dropped, so a bare `v` is not mistaken for one"""
    feature = stac_feature(
        ESGFNG_CMIP7_FACADE_PARAMETERS,
        CMIP7_ROW,
        f"{CMIP7_ROW.id_project_specific}.v",
        project="CMIP7",
    )

    (document,) = ESGFNGCMIP7ResultParser().parse_search_results(
        {"features": [feature]},
        api=stac_api(),
        facade_parameters=ESGFNG_CMIP7_FACADE_PARAMETERS,
    )

    assert document.id_project_specific == f"{CMIP7_ROW.id_project_specific}.v"


def test_cmip6_stac_project_is_read_from_mip_era():
    """CMIP6 features have no `project` property, so `cmip6:mip_era` is the project"""
    feature = stac_feature(
        ESGFNG_CMIP6_FACADE_PARAMETERS,
        CMIP6_ROW,
        f"{CMIP6_ROW.id_project_specific}.v20200623",
        base_id=CMIP6_ROW.id_project_specific,
        **{"cmip6:mip_era": "CMIP6"},
    )

    (row,) = ESGFNGCMIP6ResultParser().get_dataset_rows(
        feature, api=stac_api(), facade_parameters=ESGFNG_CMIP6_FACADE_PARAMETERS
    )

    assert row == CMIP6_ROW


def test_cmip7_stac_project_is_read_from_the_project_property():
    """CMIP7 features write the project plainly, so that is what is read"""
    feature = stac_feature(
        ESGFNG_CMIP7_FACADE_PARAMETERS,
        CMIP7_ROW,
        f"{CMIP7_ROW.id_project_specific}.v20200623",
        project="CMIP7",
    )

    (row,) = ESGFNGCMIP7ResultParser().get_dataset_rows(
        feature, api=stac_api(), facade_parameters=ESGFNG_CMIP7_FACADE_PARAMETERS
    )

    assert row == CMIP7_ROW


@pytest.mark.parametrize(
    "parser, parameters, feature, api_field",
    (
        pytest.param(
            ESGFNGCMIP6ResultParser(),
            ESGFNG_CMIP6_FACADE_PARAMETERS,
            "cmip6",
            "cmip6:mip_era",
            id="cmip6-stac",
        ),
        pytest.param(
            ESGFNGCMIP7ResultParser(),
            ESGFNG_CMIP7_FACADE_PARAMETERS,
            "cmip7",
            "project",
            id="cmip7-stac",
        ),
    ),
)
def test_a_stac_feature_with_no_project_raises(parser, parameters, feature, api_field):
    """A row we cannot say the project of is not one we can quietly store"""
    row = CMIP6_ROW if feature == "cmip6" else CMIP7_ROW
    doc = stac_feature(
        parameters,
        row,
        f"{row.id_project_specific}.v20200623",
        base_id=row.id_project_specific,
    )

    with pytest.raises(
        MissingResultFieldError,
        match=re.escape(f"carries no value for {api_field!r}"),
    ):
        parser.get_dataset_rows(doc, api=stac_api(), facade_parameters=parameters)


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
        processing_id="Amon",
    )
    doc = solr_doc(ESGF1_CMIP5_FACADE_PARAMETERS, row, variable=["tas", "pr"])

    rows = SolrVariableBundleResultParser().get_dataset_rows(
        doc, api=solr_api(), facade_parameters=ESGF1_CMIP5_FACADE_PARAMETERS
    )

    assert rows == (row, row.model_copy(update={"variable": "pr"}))
    # CMIP5 models no grid, so the column is left at its default rather than guessed.
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


@pytest.mark.parametrize(
    "parser",
    (
        pytest.param(SolrSingleRowResultParser(), id="single-row"),
        pytest.param(SolrVariableBundleResultParser(), id="variable-bundle"),
    ),
)
@pytest.mark.parametrize(
    "raw, exp",
    (
        pytest.param({"response": {"numFound": 3, "docs": []}}, 3, id="a-count"),
        pytest.param({"response": {"numFound": 0, "docs": []}}, 0, id="no-matches"),
    ),
)
def test_solr_n_matches(parser, raw, exp):
    """Both Solr parsers read the count the one way Solr writes it"""
    assert parser.get_n_matches(raw) == exp


@pytest.mark.parametrize(
    "raw, exp",
    (
        pytest.param(
            {"response": {"docs": []}},
            pytest.raises(
                NoSearchResultNumberOfMatchesReturnedError,
                match=re.escape(
                    "This response does not report "
                    "how many records matched the search. "
                    "We expected to read the count from 'response.numFound', "
                    "but 'numFound' is not in 'response', there is only: 'docs'"
                ),
            ),
            id="no-count",
        ),
        pytest.param(
            {},
            pytest.raises(
                NoSearchResultNumberOfMatchesReturnedError,
                match=re.escape(
                    "This response does not report "
                    "how many records matched the search. "
                    "We expected to read the count from 'response.numFound', "
                    "but the response is empty."
                ),
            ),
            id="nothing-we-recognise",
        ),
        pytest.param(
            {"response": {"numFound": "3"}},
            pytest.raises(
                TypeError,
                match=re.escape(
                    "We expected to get an integer at 'response.numFound', "
                    "but instead got '3'"
                ),
            ),
            id="a-count-we-cannot-read",
        ),
    ),
)
def test_solr_n_matches_with_no_count_raises(raw, exp):
    """A response we cannot read a count out of is one we have not understood"""
    with exp:
        SolrSingleRowResultParser().get_n_matches(raw)


@pytest.mark.parametrize(
    "parser",
    (
        pytest.param(ESGFNGCMIP6ResultParser(), id="cmip6"),
        pytest.param(ESGFNGCMIP7ResultParser(), id="cmip7"),
    ),
)
@pytest.mark.parametrize(
    "raw, exp",
    (
        pytest.param({"numberMatched": 7}, 7, id="stac-spelling"),
        pytest.param({"numMatched": 0}, 0, id="west-spelling"),
        pytest.param({"context": {"matched": 4}}, 4, id="west-context"),
    ),
)
def test_stac_n_matches_reads_whichever_spelling_is_present(parser, raw, exp):
    """The two deployments disagree on where the total lives; we read either

    This is the reason the count is read by something picked per endpoint rather than
    by the search API class: east and west speak the same format and still differ.
    """
    assert parser.get_n_matches(raw) == exp


# The count lives in one of three places on the two deployments,
# so the error reports on all three: any of them could have answered us.
WHERE_WE_LOOKED_FOR_THE_COUNT = (
    "This response does not report how many records matched the search. "
    "We expected to read the count from "
    "one of 'numberMatched', 'numMatched' or 'context.matched', "
)


@pytest.mark.parametrize(
    "raw, exp",
    (
        pytest.param(
            {},
            pytest.raises(
                NoSearchResultNumberOfMatchesReturnedError,
                match=re.escape(
                    f"{WHERE_WE_LOOKED_FOR_THE_COUNT}but the response is empty."
                ),
            ),
            id="nothing-we-recognise",
        ),
        pytest.param(
            {"features": [{"id": "a"}]},
            pytest.raises(
                NoSearchResultNumberOfMatchesReturnedError,
                match=re.escape(
                    f"{WHERE_WE_LOOKED_FOR_THE_COUNT}but: "
                    "'numberMatched' is not in the response's top level, "
                    "there is only: 'features'; "
                    "'numMatched' is not in the response's top level, "
                    "there is only: 'features'; "
                    "'context' is not in the response's top level, "
                    "there is only: 'features'."
                ),
            ),
            id="records-but-no-count",
        ),
        pytest.param(
            {"numMatched": None, "context": {"total": 4}},
            # Each place we looked is explained as far as we got in it,
            # so the one which came closest says so.
            pytest.raises(
                NoSearchResultNumberOfMatchesReturnedError,
                match=re.escape(
                    f"{WHERE_WE_LOOKED_FOR_THE_COUNT}but: "
                    "'numberMatched' is not in the response's top level, "
                    "there is only: 'context', 'numMatched'; "
                    "we found None at 'numMatched'; "
                    "'matched' is not in 'context', there is only: 'total'."
                ),
            ),
            id="a-context-which-does-not-carry-the-count",
        ),
    ),
)
def test_stac_n_matches_with_no_count_raises(raw, exp):
    with exp:
        ESGFNGCMIP7ResultParser().get_n_matches(raw)


def test_no_search_result_n_matches_returned_error_when_there_is_a_match_raises():
    """The error is for responses we could not read; a readable one is a bug"""
    with pytest.raises(
        AssertionError,
        match=re.escape(
            "context.matched is in {'context': {'matched': 4}}, raw[context][matched]=4"
        ),
    ):
        NoSearchResultNumberOfMatchesReturnedError(
            {"context": {"matched": 4}},
            expected_at=("numberMatched", "context.matched"),
        )
