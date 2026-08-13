"""
Test [translate_to_projects][esmporium.query.translate.translate_to_projects]
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Annotated

import pytest

from esmporium.query import (
    FacetNotExpressibleError,
    NoTargetProjectError,
    Query,
    QueryCMIP5,
    QueryCMIP6,
    QueryFacet,
    SourceQuery,
    UnknownProjectError,
    facet_values_from_attributes,
    translate_to_projects,
)


@dataclass
class QueryMIP1:
    """
    A query in MIP1's vocabulary, which is not one of ours

    It calls the project facet `mip`,
    so it doubles as the case of a query which does not use our name for the project.
    """

    mip: Annotated[tuple[str, ...], QueryFacet("project")] = ()
    esm: Annotated[tuple[str, ...], QueryFacet("model")] = ()
    vintage: Annotated[tuple[str, ...], QueryFacet(None)] = ()
    other_terms: dict[str, tuple[str, ...]] = field(default_factory=dict)
    source_query: SourceQuery = None

    def facet_values(self) -> dict[str, tuple[str, ...]]:
        """See `QueryProtocol.facet_values`"""
        return facet_values_from_attributes(self)


def test_each_project_gets_its_own_query(common_inputs, common_expected):
    start = common_inputs["canonical"].model_copy(
        update={"project": ("CMIP5", "CMIP6", "CMIP7")}
    )
    result = translate_to_projects(start)

    assert isinstance(result, dict)

    for project in ("CMIP5", "CMIP6", "CMIP7"):
        assert result[project].model_copy(
            update={"source_query": None}
        ) == common_expected[project].model_copy(update={"project": (project,)})


@pytest.mark.parametrize(
    "query, project_facet",
    [
        pytest.param(Query(model="ACCESS-CM2"), "project", id="canonical-language"),
        pytest.param(
            QueryCMIP6(source_id="ACCESS-CM2", project=()), "project", id="cmip6"
        ),
        pytest.param(QueryMIP1(esm=("ACCESS-CM2",)), "mip", id="renamed-project-facet"),
    ],
)
def test_no_target_project_error(query, project_facet: str):
    with pytest.raises(
        NoTargetProjectError,
        match=re.escape(
            f"Please supply `projects` or set the query's `{project_facet}` facet"
        ),
    ):
        translate_to_projects(query)


def test_no_project_facet_at_all_error():
    @dataclass
    class QueryMIP1NoProject:
        """A query in MIP1's vocabulary, which has no concept of a project"""

        esm: Annotated[tuple[str, ...], QueryFacet("model")] = ()
        other_terms: dict[str, tuple[str, ...]] = field(default_factory=dict)
        source_query: SourceQuery = None

        def facet_values(self) -> dict[str, tuple[str, ...]]:
            """See `QueryProtocol.facet_values`"""
            return facet_values_from_attributes(self)

    with pytest.raises(
        NoTargetProjectError,
        match=re.escape(
            "Please supply `projects` "
            "(QueryMIP1NoProject has no facet equivalent to `project`)"
        ),
    ):
        translate_to_projects(QueryMIP1NoProject(esm=("ACCESS-CM2",)))


def test_targets_inferred_from_a_renamed_project_facet():
    """
    The project facet is read (and written) under whatever the query calls it
    """
    query = QueryMIP1(mip=("CMIP5", "CMIP6"), esm=("ACCESS-CM2",))

    result = translate_to_projects(query)

    assert sorted(result) == ["CMIP5", "CMIP6"]
    assert result["CMIP5"].model_copy(update={"source_query": None}) == QueryCMIP5(
        project="CMIP5", model="ACCESS-CM2"
    )
    assert result["CMIP6"].model_copy(update={"source_query": None}) == QueryCMIP6(
        project="CMIP6", source_id="ACCESS-CM2"
    )

    # Back the other way, the project lands on `mip`, not on `project`
    round_trip = translate_to_projects(
        query,
        project_query_map={"CMIP5": QueryMIP1, "CMIP6": QueryMIP1},
    )

    assert round_trip == {
        "CMIP5": QueryMIP1(mip=("CMIP5",), esm=("ACCESS-CM2",), source_query=query),
        "CMIP6": QueryMIP1(mip=("CMIP6",), esm=("ACCESS-CM2",), source_query=query),
    }


@pytest.mark.parametrize("project", ["cmip7", "CMIP7", "cmIP7"])
def test_project_lookup_is_case_insensitive(
    project: str, common_inputs, common_expected
):
    """
    The project a user asks for is matched without regard to case.
    """
    result = translate_to_projects(common_inputs["CMIP6"], projects=[project])

    assert result[project].model_copy(update={"source_query": None}) == common_expected[
        "CMIP7"
    ].model_copy(update={"project": (project,)})


def test_multi_project_fails_if_any_arm_fails():
    """If one requested project cannot express the query, the whole call raises."""
    query = QueryCMIP5(model="ACCESS-CM2", product="output1")

    # Just CMIP5, fine
    translate_to_projects(query, projects=["CMIP5"])
    with pytest.raises(FacetNotExpressibleError, match="CMIP6"):
        translate_to_projects(query, projects=["CMIP5", "CMIP6"])


def test_unknown_project_raises():
    """A project we have no language for raises, listing the ones we do have."""
    query = Query(model="ACCESS-CM2")

    with pytest.raises(
        UnknownProjectError,
        match=(
            "We don't support 'CMIP4'; "
            "supported projects: CMIP5, CMIP6, CMIP6Plus, CMIP7"
        ),
    ):
        translate_to_projects(query, projects=["CMIP4"])


def test_project_query_map_can_be_injected():
    query = Query(model="ACCESS-CM2", project=["CMIP6Plusplus"])

    result = translate_to_projects(
        query, project_query_map={"CMIP6Plusplus": QueryCMIP6}
    )

    assert result == {
        "CMIP6Plusplus": QueryCMIP6(
            project="CMIP6Plusplus", source_id="ACCESS-CM2", source_query=query
        )
    }


def test_injected_project_query_map_replaces_the_defaults():
    """An injected map is the whole map, so our defaults no longer apply."""
    query = Query(model="ACCESS-CM2")

    with pytest.raises(
        UnknownProjectError,
        match="We don't support 'CMIP6'; supported projects: MIP1",
    ):
        translate_to_projects(
            query, projects=["CMIP6"], project_query_map={"MIP1": QueryCMIP6}
        )


def test_query_class_can_be_injected():
    """
    A caller can decide which class each project's translation is built in.
    """

    class MyCMIP6Query(QueryCMIP6):
        """A CMIP6 query of the caller's own"""

    query = Query(model="ACCESS-CM2", project=["CMIP6"])

    result = translate_to_projects(query, project_query_map={"CMIP6": MyCMIP6Query})

    assert result == {
        "CMIP6": MyCMIP6Query(
            project="CMIP6", source_id="ACCESS-CM2", source_query=query
        )
    }


def test_a_query_we_did_not_write_can_be_a_target():
    """
    A query of your own can be the thing a project renders into
    """
    start = QueryMIP1(esm=("ACCESS-CM2",), vintage=("2026",))

    result = translate_to_projects(
        start, projects=["MIP1"], project_query_map={"MIP1": QueryMIP1}
    )

    assert result == {
        "MIP1": QueryMIP1(
            mip=("MIP1",), esm=("ACCESS-CM2",), vintage=("2026",), source_query=start
        )
    }
