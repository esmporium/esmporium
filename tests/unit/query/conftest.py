"""
Here we define some pieces shared by the translation tests
"""

from __future__ import annotations

import pytest

from esmporium.query import Query, QueryCMIP5, QueryCMIP6, QueryCMIP7


@pytest.fixture
def common_inputs() -> dict[str, object]:
    """
    The same query, written in each query class

    The expected outputs for translations using these inputs
    is captured in [common_expected][].

    Specifically, the same query, written in each query class we support,
    plus what it should look like once rendered into each other query class.

    Only the facets which every query class can express are set, so these can be
    translated in any direction without tripping the fail-loud rule.
    Tests which care about a facet some query does not support
    need to set it up themselves.
    """
    return {
        "canonical": Query(
            project="custom-project",
            model="ACCESS-CM2",
            institution="CSIRO",
            experiment="historical",
            variable="tas",
            variant_label="r1i1p1f1",
            reporting_interval="mon",
            processing_id="Amon",
            realm="atmos",
        ),
        "CMIP5": QueryCMIP5(
            project="custom-project",
            model="ACCESS-CM2",
            institute="CSIRO",
            experiment="historical",
            variable="tas",
            ensemble="r1i1p1f1",
            time_frequency="mon",
            cmor_table="Amon",
            realm="atmos",
        ),
        "CMIP6": QueryCMIP6(
            project="custom-project",
            source_id="ACCESS-CM2",
            institution_id="CSIRO",
            experiment_id="historical",
            variable_id="tas",
            variant_label="r1i1p1f1",
            frequency="mon",
            table_id="Amon",
            realm="atmos",
        ),
        "CMIP7": QueryCMIP7(
            project="custom-project",
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


@pytest.fixture
def common_expected() -> dict[str, object]:
    """
    What [common_inputs][] should look like when rendered into each query class

    Written out by hand rather than derived from `common_inputs`,
    so that a change to translation has to be also done here to pass.
    """
    return {
        "CMIP5": QueryCMIP5(
            project="custom-project",
            model="ACCESS-CM2",
            institute="CSIRO",
            experiment="historical",
            variable="tas",
            ensemble="r1i1p1f1",
            time_frequency="mon",
            cmor_table="Amon",
            realm="atmos",
        ),
        "CMIP6": QueryCMIP6(
            project="custom-project",
            source_id="ACCESS-CM2",
            institution_id="CSIRO",
            experiment_id="historical",
            variable_id="tas",
            variant_label="r1i1p1f1",
            frequency="mon",
            table_id="Amon",
            realm="atmos",
        ),
        "CMIP7": QueryCMIP7(
            project="custom-project",
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
