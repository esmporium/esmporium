"""
Test translation cases using real project-specific values as inputs.

For concrete queries, thenative params that come out are exactly what a
human checked against the DRS of ach project. They double as
documentation of what a translation looks like.

Two layers of coverage:

- Every project can be translated to every other project - run on the facets
every project shares (so no arm hits the fail-loud rule), and
- each facet query class (skin) fully populated and rendered back to its own project
(exercising the project-only facets: `grid_label`/`activity`/`resolution`,
CMIP5 `product`).

plus the edge cases: fail-loud, passthrough, retention, normalisation.
"""

from __future__ import annotations

import pytest

from esmporium.esgf import (
    ESGFQuery,
    ESGFQueryCMIP5,
    ESGFQueryCMIP6,
    ESGFQueryCMIP7,
    FacetNotRepresentableError,
    translate,
)

# Define facets shared by every project and the unified (common) language
COMMON_INPUTS = {
    "unified": ESGFQuery(
        model="ACCESS-CM2",
        institution="CSIRO",
        experiment="historical",
        variable="tas",
        variant_label="r1i1p1f1",
        reporting_interval="mon",
        processing_id="Amon",
        realm="atmos",
    ),
    "CMIP5": ESGFQueryCMIP5(
        model="ACCESS-CM2",
        institute="CSIRO",
        experiment="historical",
        variable="tas",
        ensemble="r1i1p1f1",
        time_frequency="mon",
        cmor_table="Amon",
        realm="atmos",
    ),
    "CMIP6": ESGFQueryCMIP6(
        source_id="ACCESS-CM2",
        institution_id="CSIRO",
        experiment_id="historical",
        variable_id="tas",
        variant_label="r1i1p1f1",
        frequency="mon",
        table_id="Amon",
        realm="atmos",
    ),
    "CMIP7": ESGFQueryCMIP7(
        source_id="ACCESS-CM2",
        institution_id="CSIRO",
        experiment_id="historical",
        variable_id="tas",
        variant_label="r1i1p1f1",
        frequency="mon",
        branding_suffix="Amon",
        realm="atmos",
    ),
}

# What that same query must become in each project's native words.
COMMON_EXPECTED = {
    "CMIP5": {
        "model": "ACCESS-CM2",
        "institute": "CSIRO",
        "experiment": "historical",
        "variable": "tas",
        "ensemble": "r1i1p1f1",
        "time_frequency": "mon",
        "cmor_table": "Amon",
        "realm": "atmos",
        "project": "CMIP5",
    },
    "CMIP6": {
        "source_id": "ACCESS-CM2",
        "institution_id": "CSIRO",
        "experiment_id": "historical",
        "variable_id": "tas",
        "variant_label": "r1i1p1f1",
        "frequency": "mon",
        "table_id": "Amon",
        "realm": "atmos",
        "project": "CMIP6",
    },
    "CMIP7": {
        "source_id": "ACCESS-CM2",
        "institution_id": "CSIRO",
        "experiment_id": "historical",
        "variable_id": "tas",
        "variant_label": "r1i1p1f1",
        "frequency": "mon",
        "branding_suffix": "Amon",
        "realm": "atmos",
        "project": "CMIP7",
    },
}


@pytest.mark.parametrize("target", ["CMIP5", "CMIP6", "CMIP7"])
@pytest.mark.parametrize("dialect", list(COMMON_INPUTS))
def test_common_subset_journeys(dialect: str, target: str):
    """
    Every input dialect renders to every project's native words, via the hub.

    The input dialect is independent of the target project:
    a CMIP5 skin can render CMIP7 params and vice versa, because both go through
    the same canonical form.
    """
    query = COMMON_INPUTS[dialect]

    result = translate(query, projects=[target])

    assert result[target] == COMMON_EXPECTED[target]


# Each skin fully populated, rendered back to its own project (identity round-trip).
# Covers the project-only facets the common grid above cannot.
def test_cmip5_full_identity():
    """A fully-populated CMIP5 query (incl. `product`) renders to CMIP5 unchanged."""
    query = ESGFQueryCMIP5(
        model="ACCESS-CM2",
        institute="CSIRO",
        experiment="historical",
        variable="tas",
        ensemble="r1i1p1f1",
        time_frequency="mon",
        cmor_table="Amon",
        realm="atmos",
        product="output1",
    )

    assert translate(query)["CMIP5"] == {
        "model": "ACCESS-CM2",
        "institute": "CSIRO",
        "experiment": "historical",
        "variable": "tas",
        "ensemble": "r1i1p1f1",
        "time_frequency": "mon",
        "cmor_table": "Amon",
        "realm": "atmos",
        "product": "output1",
        "project": "CMIP5",
    }


def test_cmip6_full_identity():
    """A fully-populated CMIP6 query (incl. grid/activity/resolution) is unchanged."""
    query = ESGFQueryCMIP6(
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
    )

    assert translate(query)["CMIP6"] == {
        "source_id": "ACCESS-CM2",
        "institution_id": "CSIRO",
        "experiment_id": "historical",
        "variable_id": "tas",
        "variant_label": "r1i1p1f1",
        "frequency": "mon",
        "grid_label": "gn",
        "table_id": "Amon",
        "activity_id": "CMIP",
        "nominal_resolution": "250 km",
        "realm": "atmos",
        "project": "CMIP6",
    }


def test_cmip7_full_identity():
    """A fully-populated CMIP7 query (branding_suffix, grid/activity/resolution)."""
    query = ESGFQueryCMIP7(
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
    )

    assert translate(query)["CMIP7"] == {
        "source_id": "ACCESS-CM2",
        "institution_id": "CSIRO",
        "experiment_id": "historical",
        "variable_id": "tas",
        "variant_label": "r1i1p1f1",
        "frequency": "mon",
        "grid_label": "gn",
        "branding_suffix": "tavg-h2m-hxy-air",
        "activity_id": "CMIP",
        "nominal_resolution": "250 km",
        "realm": "atmos",
        "project": "CMIP7",
    }


# Edge cases that will fail by our definition.
# i.e. asserting facets that are not defined in a project's native facet search.
@pytest.mark.parametrize(
    "query",
    [
        ESGFQueryCMIP6(grid_label="gn", project=["CMIP5"]),
        ESGFQueryCMIP6(activity_id="CMIP", project=["CMIP5"]),
        ESGFQueryCMIP6(nominal_resolution="250 km", project=["CMIP5"]),
    ],
    ids=["grid_label", "activity", "resolution"],
)
def test_canonical_facet_absent_in_target_raises(query):
    """A canonical facet the target lacks raises, naming the facet and project."""
    with pytest.raises(FacetNotRepresentableError, match="CMIP5"):
        translate(query)


def test_project_specific_facet_wrong_project_raises():
    """A CMIP5 `product` sent to CMIP6 raises, rather than being dropped."""
    query = ESGFQueryCMIP5(model="ACCESS-CM2", product="output1", project=["CMIP6"])

    with pytest.raises(FacetNotRepresentableError) as excinfo:
        translate(query)

    assert excinfo.value.facet == "product"
    assert excinfo.value.project == "CMIP6"


def test_project_specific_facet_own_project_emits():
    """The same `product` renders fine to CMIP5, the project that owns it."""
    query = ESGFQueryCMIP5(model="ACCESS-CM2", product="output1", project=["CMIP5"])

    assert translate(query)["CMIP5"] == {
        "model": "ACCESS-CM2",
        "product": "output1",
        "project": "CMIP5",
    }


# TODO: @Zeb: we want users to be able to search across multiple projects, right?
def test_multi_project_fails_if_any_arm_fails():
    """If one requested project cannot express the query, the whole call raises."""
    query = ESGFQueryCMIP5(
        model="ACCESS-CM2", product="output1", project=["CMIP5", "CMIP6"]
    )

    with pytest.raises(FacetNotRepresentableError, match="CMIP6"):
        translate(query)


# Passthrough, retention, normalisation.
def test_other_terms_pass_through_best_effort():
    """An unmodelled `other_terms` facet is emitted as-is to any project, no error."""
    query = ESGFQuery(
        model="ACCESS-CM2", project=["CMIP6"], other_terms={"made_up_facet": "foo"}
    )

    assert translate(query)["CMIP6"] == {
        "source_id": "ACCESS-CM2",
        "made_up_facet": "foo",
        "project": "CMIP6",
    }


def test_source_spec_retains_the_original_as_typed():
    """
    The original query, including a dropped-in-render facet, is kept on
    the intermediate response.
    """
    query = ESGFQueryCMIP5(model="ACCESS-CM2", product="output1")

    spec = query.to_canonical().source_spec

    assert spec["dialect"] == "CMIP5"
    assert spec["facets"] == {"model": ("ACCESS-CM2",), "product": ("output1",)}


def test_single_string_becomes_one_value():
    """A lone string is one value, not an iterable of characters."""
    result = translate(ESGFQueryCMIP6(source_id="ACCESS-CM2", project=["CMIP6"]))

    assert result["CMIP6"]["source_id"] == "ACCESS-CM2"


def test_multiple_values_are_or_joined():
    """Values within a facet are OR-ed, i.e. comma-joined in the params."""
    query = ESGFQueryCMIP6(
        variable_id=["tas", "pr"], experiment_id=("historical", "piControl")
    )

    result = translate(query)["CMIP6"]

    assert result["variable_id"] == "tas,pr"
    assert result["experiment_id"] == "historical,piControl"
