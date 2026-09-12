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
    ESGFNGResultParser,
    MissingResultFieldError,
    MultipleFacetValuesError,
    NoSearchResultNumberOfMatchesReturnedError,
    SearchAPIESGF1Solr,
    SearchAPIESGFNGSTAC,
    SolrSingleRowResultParser,
    SolrVariableBundleResultParser,
    UnreadableResponseError,
    build_transient_retrying,
    stac_east_n_matches,
    stac_west_n_matches,
)


def esgfng_parser(east: bool = True) -> ESGFNGResultParser:
    """An ESGF-NG parser for one deployment or the other

    One parser serves every project on this API: what differs between the
    deployments is only where each writes its match count.
    """
    if east:
        return ESGFNGResultParser(
            read_n_matches=stac_east_n_matches,
        )

    return ESGFNGResultParser(
        read_n_matches=stac_west_n_matches,
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


def test_east_n_matches_reads_the_stac_spelling():
    """East writes the count where STAC says to, and that is all east's reader reads"""
    assert stac_east_n_matches({"numberMatched": 7, "features": []}) == 7


@pytest.mark.parametrize(
    "raw, exp",
    (
        pytest.param({"numMatched": 7}, 7, id="top-level"),
        pytest.param({"context": {"matched": 7}}, 7, id="context"),
        # West sends both, and they agree; `numMatched` is the one we read.
        pytest.param({"numMatched": 7, "context": {"matched": 7}}, 7, id="both"),
    ),
)
def test_west_n_matches_reads_wests_own_spellings(raw, exp):
    """West does not write `numberMatched` at all; it writes these instead"""
    assert stac_west_n_matches(raw) == exp


# Each reader looks only where its own deployment writes the count, so the error names
# only those places.
WHERE_EAST_LOOKED = (
    "This response does not report how many records matched the search. "
    "We expected to read the count from 'numberMatched', "
)
WHERE_WEST_LOOKED = (
    "This response does not report how many records matched the search. "
    "We expected to read the count from one of 'numMatched' or 'context.matched', "
)


def test_east_n_matches_does_not_read_wests_spellings():
    """Reading west's spellings on east would hide east changing shape

    The whole point of a reader per deployment is that the day one of them starts
    answering like the other, we are told rather than quietly carrying on.
    """
    with pytest.raises(
        NoSearchResultNumberOfMatchesReturnedError,
        match=re.escape(
            f"{WHERE_EAST_LOOKED}but 'numberMatched' is not in the response's top "
            "level, there is only: 'context', 'numMatched'"
        ),
    ):
        stac_east_n_matches({"numMatched": 7, "context": {"matched": 7}})


def test_west_n_matches_does_not_read_easts_spelling():
    """And the same the other way round"""
    with pytest.raises(
        NoSearchResultNumberOfMatchesReturnedError,
        match=re.escape(f"{WHERE_WEST_LOOKED}but: "),
    ):
        stac_west_n_matches({"numberMatched": 7})


@pytest.mark.parametrize(
    "read_n_matches, raw, exp",
    (
        pytest.param(
            stac_east_n_matches,
            {},
            pytest.raises(
                NoSearchResultNumberOfMatchesReturnedError,
                match=re.escape(f"{WHERE_EAST_LOOKED}but the response is empty."),
            ),
            id="east-nothing-we-recognise",
        ),
        pytest.param(
            stac_east_n_matches,
            {"features": [{"id": "a"}]},
            pytest.raises(
                NoSearchResultNumberOfMatchesReturnedError,
                match=re.escape(
                    f"{WHERE_EAST_LOOKED}but 'numberMatched' is not in the response's "
                    "top level, there is only: 'features'."
                ),
            ),
            id="east-records-but-no-count",
        ),
        pytest.param(
            stac_east_n_matches,
            {"numberMatched": "7"},
            pytest.raises(
                TypeError,
                match=re.escape(
                    "We expected to get an integer at numberMatched, "
                    "but instead got '7'"
                ),
            ),
            id="east-a-count-we-cannot-read",
        ),
        pytest.param(
            stac_west_n_matches,
            {},
            pytest.raises(
                NoSearchResultNumberOfMatchesReturnedError,
                match=re.escape(f"{WHERE_WEST_LOOKED}but the response is empty."),
            ),
            id="west-nothing-we-recognise",
        ),
        pytest.param(
            stac_west_n_matches,
            {"numMatched": None, "context": {"total": 4}},
            # Each place we looked is explained as far as we got in it,
            # so the one which came closest says so.
            pytest.raises(
                NoSearchResultNumberOfMatchesReturnedError,
                match=re.escape(
                    f"{WHERE_WEST_LOOKED}but: we found None at 'numMatched'; "
                    "'matched' is not in 'context', there is only: 'total'."
                ),
            ),
            id="west-a-context-which-does-not-carry-the-count",
        ),
    ),
)
def test_stac_n_matches_with_no_count_raises(read_n_matches, raw, exp):
    """A response we cannot read a count out of is one we have not understood"""
    with exp:
        read_n_matches(raw)


def test_a_stac_parser_reads_the_count_with_the_reader_it_was_given():
    """The parser counts the way its deployment does, not the way STAC says to"""
    parser = esgfng_parser(east=False)

    assert parser.get_n_matches({"numMatched": 7, "features": []}) == 7

    with pytest.raises(NoSearchResultNumberOfMatchesReturnedError):
        parser.get_n_matches({"numberMatched": 7, "features": []})


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
