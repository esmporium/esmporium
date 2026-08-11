"""
Golden translation tests — exact, hand-verified native params.

Where the invariant tests (`test_facet_invariants.py`) prove the *config* is
consistent, these prove the *mappings are correct*: for concrete queries, the
native params that come out are exactly what a human checked against the DRS of
each era. They double as documentation of what a translation looks like.

Two layers of coverage:

- the `N x M` journey grid, run on the facets every era shares (so no arm hits
  the fail-loud rule), and
- each skin fully populated and rendered back to its own era (exercising the
  era-only facets: `grid_label`/`activity`/`resolution`, CMIP5 `product`).

plus the edge cases: fail-loud, passthrough, retention, normalisation.
"""

from __future__ import annotations

import pytest

from esmporium.db.esgf import (
    ESGFQuery,
    ESGFQueryCMIP5,
    ESGFQueryCMIP6,
    ESGFQueryCMIP7,
    FacetNotRepresentableError,
    translate,
)

# --------------------------------------------------------------------------- #
# The N x M grid, on the subset of facets shared by every era.
# --------------------------------------------------------------------------- #

# One query per input dialect, all describing the *same* thing in that dialect's
# words. CMIP5 has no grid/activity/resolution, so we stay on the common subset
# here and cover the era-only facets in the identity tests below.
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

# What that same query must become in each era's native words.
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
    Every input dialect renders to every era's native words, via the hub.

    This is the `N x M` grid. The input dialect is independent of the target era:
    a CMIP5 skin can render CMIP7 params and vice versa, because both go through
    the same canonical form.
    """
    query = COMMON_INPUTS[dialect]

    result = translate(query, projects=[target])

    assert result[target] == COMMON_EXPECTED[target]


# --------------------------------------------------------------------------- #
# Each skin fully populated, rendered back to its own era (identity round-trip).
# Covers the era-only facets the common grid above cannot.
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# Fail-loud edge cases.
# --------------------------------------------------------------------------- #


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
    """A canonical facet the target era lacks raises, naming the facet and era."""
    with pytest.raises(FacetNotRepresentableError, match="CMIP5"):
        translate(query)


def test_era_specific_facet_wrong_era_raises():
    """A CMIP5 `product` sent to CMIP6 raises (rule 2a), rather than being dropped."""
    query = ESGFQueryCMIP5(model="ACCESS-CM2", product="output1", project=["CMIP6"])

    with pytest.raises(FacetNotRepresentableError) as excinfo:
        translate(query)

    assert excinfo.value.facet == "product"
    assert excinfo.value.mip_era == "CMIP6"


def test_era_specific_facet_own_era_emits():
    """The same `product` renders fine to CMIP5, the era that owns it (rule 2b)."""
    query = ESGFQueryCMIP5(model="ACCESS-CM2", product="output1", project=["CMIP5"])

    assert translate(query)["CMIP5"] == {
        "model": "ACCESS-CM2",
        "product": "output1",
        "project": "CMIP5",
    }


def test_multi_era_fails_if_any_arm_fails():
    """If one requested era cannot express the query, the whole call raises."""
    query = ESGFQueryCMIP5(
        model="ACCESS-CM2", product="output1", project=["CMIP5", "CMIP6"]
    )

    with pytest.raises(FacetNotRepresentableError, match="CMIP6"):
        translate(query)


# --------------------------------------------------------------------------- #
# Passthrough, retention, normalisation.
# --------------------------------------------------------------------------- #


def test_other_terms_pass_through_best_effort():
    """An unmodelled `other_terms` facet is emitted as-is to any era, no error."""
    query = ESGFQuery(
        model="ACCESS-CM2", project=["CMIP6"], other_terms={"made_up_facet": "foo"}
    )

    assert translate(query)["CMIP6"] == {
        "source_id": "ACCESS-CM2",
        "made_up_facet": "foo",
        "project": "CMIP6",
    }


def test_source_spec_retains_the_original_as_typed():
    """The original query, including a dropped-in-render facet, is kept on the IR."""
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
