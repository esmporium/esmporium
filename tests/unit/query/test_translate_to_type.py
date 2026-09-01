"""
Test [translate_to_type][esmporium.query.translate.translate_to_type]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated

import pytest

from esmporium.query import (
    FacetNotExpressibleError,
    Query,
    QueryCMIP5,
    QueryCMIP6,
    QueryCMIP7,
    QueryFacet,
    SourceQuery,
    translate_to_type,
)

START_PROJECTS = ("canonical", "CMIP5", "CMIP6", "CMIP7")
TARGET_PROJECTS = ("CMIP5", "CMIP6", "CMIP7")


def test_the_project_lists_match_the_shared_queries(common_inputs, common_expected):
    """
    Guard: the lists we parametrise over must stay in step with the fixtures.

    Otherwise a project (and associated query class)
    added to the fixtures would silently go untested.
    """
    assert set(START_PROJECTS) == set(common_inputs)
    assert set(TARGET_PROJECTS) == set(common_expected)


@pytest.mark.parametrize("target_key", TARGET_PROJECTS)
@pytest.mark.parametrize("start_key", START_PROJECTS)
def test_every_query_class_translates_to_every_other(
    start_key: str, target_key: str, common_inputs, common_expected
):
    query_start = common_inputs[start_key]
    query_expected = common_expected[target_key]

    result = translate_to_type(query_start, to=type(query_expected))

    assert result.source_query == query_start

    result_compare = result.model_copy(update={"source_query": None})
    assert result_compare == query_expected


def test_returns_an_instance_of_the_target_type():
    result = translate_to_type(QueryCMIP5(model="ACCESS-CM2"), to=QueryCMIP6)

    assert isinstance(result, QueryCMIP6)
    assert result.source_id == ("ACCESS-CM2",)


def test_project_is_not_overridden():
    query = QueryCMIP6(source_id="ACCESS-CM2", project=["CMIP6"])

    res = translate_to_type(query, to=QueryCMIP5)

    # project preserved by translate_to_type, even though we go to a different query.
    # Only translate_to_projects overwrites
    assert res.project == query.project


def test_values_remain_normalised():
    query = QueryCMIP6(
        variable_id=["tas", "pr"], experiment_id=("historical", "piControl")
    )

    result = translate_to_type(query, to=QueryCMIP6)

    assert result.variable_id == ("tas", "pr")
    assert result.experiment_id == ("historical", "piControl")


@pytest.mark.parametrize("to_type", [QueryCMIP5, QueryCMIP6, QueryCMIP7])
def test_source_query_is_preserved(to_type):
    start = QueryCMIP5(model="ACCESS-CM2")

    res = translate_to_type(start, to=to_type)

    assert res.source_query == start


def test_can_roundtrip_to_another_project():
    query_cmip6 = QueryCMIP6(
        source_id="ACCESS-CM2",
        # Use CMIP6 query, but say you want to search for CMIP7
        project=["CMIP7"],
    )

    query_cmip5 = translate_to_type(query_cmip6, to=QueryCMIP5)

    # project not auto-updated
    assert query_cmip5.project == ("CMIP7",)
    # Check source_query while we're here
    assert query_cmip5.source_query == query_cmip6

    round_trip = translate_to_type(query_cmip5, to=QueryCMIP6)
    assert round_trip.model_copy(update={"source_query": None}) == query_cmip6
    # Check source_query while we're here
    assert round_trip.source_query == query_cmip5
    # Can walk back up the tree
    assert round_trip.source_query.source_query == query_cmip6


@pytest.mark.parametrize(
    "query",
    [
        pytest.param(
            QueryCMIP5(
                model="ACCESS-CM2",
                institute="CSIRO",
                experiment="historical",
                variable="tas",
                ensemble="r1i1p1f1",
                time_frequency="mon",
                cmor_table="Amon",
                realm="atmos",
                product="output1",
            ),
            id="CMIP5",
        ),
        pytest.param(
            QueryCMIP6(
                source_id="ACCESS-CM2",
                institution_id="CSIRO",
                experiment_id="historical",
                variable_id="tas",
                variant_label="r1i1p1f1",
                frequency="mon",
                grid_label="gn",
                table_id="Amon",
                activity_id="CMIP",
                nominal_resolution="250 km",
                realm="atmos",
                sub_experiment_id="s1960",
            ),
            id="CMIP6",
        ),
        pytest.param(
            QueryCMIP7(
                source_id="ACCESS-CM2",
                institution_id="CSIRO",
                experiment_id="historical",
                variable_id="tas",
                variant_label="r1i1p1f1",
                frequency="mon",
                grid_label="gn",
                branding_suffix="tavg-h2m-hxy-air",
                activity_id="CMIP",
                nominal_resolution="250 km",
                realm="atmos",
                temporal_label="tavg",
                vertical_label="h2m",
                horizontal_label="hxy",
                area_label="air",
                region="global",
            ),
            id="CMIP7",
        ),
    ],
)
def test_full_identity(query):
    """
    A fully-populated query, translated to its own project, comes back unchanged

    This makes sure that we don't lose query-specific facets
    if we just go to canonical and back.
    """
    assert (
        translate_to_type(query, to=type(query)).model_copy(
            update={"source_query": None}
        )
        == query
    )


@pytest.mark.parametrize(
    "query",
    [
        QueryCMIP6(grid_label="gn"),
        QueryCMIP6(activity_id="CMIP"),
        QueryCMIP6(nominal_resolution="250 km"),
    ],
    ids=["grid_label", "activity", "resolution"],
)
def test_canonical_facet_absent_in_target_raises(query):
    with pytest.raises(
        FacetNotExpressibleError,
        match=r"facet '.*' cannot be represented in QueryCMIP5",
    ):
        translate_to_type(query, to=QueryCMIP5)


def test_query_specific_facet_wrong_project_raises():
    query = QueryCMIP5(model="ACCESS-CM2", product="output1")

    with pytest.raises(
        FacetNotExpressibleError,
        match="facet 'product' cannot be represented in QueryCMIP6",
    ) as excinfo:
        translate_to_type(query, to=QueryCMIP6)

    assert excinfo.value.facets == ("product",)
    assert excinfo.value.query_class == "QueryCMIP6"


def test_query_specific_facet_round_trips():
    query = QueryCMIP5(model="ACCESS-CM2", product="output1")

    assert (
        translate_to_type(query, to=QueryCMIP5).model_copy(
            update={"source_query": None}
        )
        == query
    )


def test_other_terms_pass_through():
    query = Query(model="ACCESS-CM2", other_terms={"made_up_facet": "foo"})

    assert translate_to_type(query, to=QueryCMIP6).model_copy(
        update={"source_query": None}
    ) == QueryCMIP6(
        project="CMIP6",
        source_id="ACCESS-CM2",
        other_terms={"made_up_facet": ("foo",)},
    )


def test_query_class_can_be_injected():
    """
    A caller can decide which class a translation is built in.
    """

    class MyCMIP6Query(QueryCMIP6):
        """A CMIP6 query of the caller's own"""

    query = Query(model="ACCESS-CM2", project=["CMIP6"])

    result = translate_to_type(query, to=MyCMIP6Query)

    assert isinstance(result, MyCMIP6Query)
    assert result.model_copy(update={"source_query": None}) == MyCMIP6Query(
        project="CMIP6", source_id="ACCESS-CM2"
    )


def test_a_query_we_did_not_write_can_be_translated():
    """
    Anything satisfying `QueryProtocol` can be translated

    A query of your own needs no registration and no pydantic:
    annotate its facets and it can be translated in and out like ours.
    """

    @dataclass
    class QueryMIP1:
        """A query in MIP1's vocabulary"""

        mip: Annotated[tuple[str, ...], QueryFacet("project")] = ()
        esm: Annotated[tuple[str, ...], QueryFacet("model")] = ()
        vintage: Annotated[tuple[str, ...], QueryFacet(None)] = ()
        other_terms: dict[str, tuple[str, ...]] = field(default_factory=dict)
        source_query: SourceQuery = None

    # Out of our query class into theirs, query-specific facet and all
    start = QueryMIP1(esm=("ACCESS-CM2",), vintage=("2026",))

    assert translate_to_type(start, to=QueryMIP1) == QueryMIP1(
        esm=("ACCESS-CM2",), vintage=("2026",), source_query=start
    )

    # ...and into one of ours
    plain = QueryMIP1(esm=("ACCESS-CM2",))
    assert translate_to_type(plain, to=QueryCMIP6) == QueryCMIP6(
        source_id="ACCESS-CM2", source_query=plain
    )


def test_a_query_we_did_not_write_fails_loud_on_a_facet_it_cannot_express():
    """
    The fail-loud rule applies to an injected query like any other
    """

    @dataclass
    class QueryMIP1:
        """A query in MIP1's vocabulary, which has no concept of a variable"""

        esm: Annotated[tuple[str, ...], QueryFacet("model")] = ()
        other_terms: dict[str, tuple[str, ...]] = field(default_factory=dict)
        source_query: SourceQuery = None

    query = Query(model="ACCESS-CM2", variable="tas")

    with pytest.raises(
        FacetNotExpressibleError,
        match="facet 'variable' cannot be represented in QueryMIP1",
    ):
        translate_to_type(query, to=QueryMIP1)
