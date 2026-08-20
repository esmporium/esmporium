"""
Test generation of parameters from canonical queries and related erros
"""

import pytest

from esmporium.query import (
    FacetNotExpressibleError,
    QueryCanonical,
    QueryCMIP5,
    QueryCMIP6,
    QueryCMIP7,
    from_canonical,
    to_canonical,
)
from esmporium.search import (
    SolrCMIP5Parameters,
    SolrCMIP6Parameters,
    SolrCMIP7Parameters,
    StacCMIP5Parameters,
    StacCMIP6Parameters,
    StacCMIP7Parameters,
)


@pytest.mark.parametrize(
    "exp",
    (
        pytest.param(
            SolrCMIP5Parameters(
                model=("model",),
                institute=("institution",),
                experiment=("experiment",),
                variable=("variable",),
                ensemble=("variant_label",),
                time_frequency=("reporting_interval",),
                cmor_table=("processing_id",),
                realm=("realm",),
                other_terms={"custom_other": ("other_terms",)},
            ),
            id="solr-cmip5",
        ),
        pytest.param(
            StacCMIP5Parameters(
                model=("model",),
                institute=("institution",),
                experiment=("experiment",),
                variable=("variable",),
                ensemble=("variant_label",),
                time_frequency=("reporting_interval",),
                cmor_table=("processing_id",),
                realm=("realm",),
                other_terms={"custom_other": ("other_terms",)},
            ),
            id="stac-cmip5",
        ),
        pytest.param(
            SolrCMIP6Parameters(
                source_id=("model",),
                institution_id=("institution",),
                experiment_id=("experiment",),
                variable_id=("variable",),
                variant_label=("variant_label",),
                frequency=("reporting_interval",),
                table_id=("processing_id",),
                realm=("realm",),
                other_terms={"custom_other": ("other_terms",)},
            ),
            id="solr-cmip6",
        ),
        pytest.param(
            StacCMIP6Parameters(
                source_id=("model",),
                institution_id=("institution",),
                experiment_id=("experiment",),
                variable_id=("variable",),
                variant_label=("variant_label",),
                frequency=("reporting_interval",),
                table_id=("processing_id",),
                realm=("realm",),
                other_terms={"custom_other": ("other_terms",)},
            ),
            id="stac-cmip6",
        ),
        pytest.param(
            SolrCMIP7Parameters(
                source_id=("model",),
                institution_id=("institution",),
                experiment_id=("experiment",),
                variable_id=("variable",),
                variant_label=("variant_label",),
                frequency=("reporting_interval",),
                branding_suffix=("processing_id",),
                realm=("realm",),
                other_terms={"custom_other": ("other_terms",)},
            ),
            id="solr-cmip7",
        ),
        pytest.param(
            StacCMIP7Parameters(
                source_id=("model",),
                institution_id=("institution",),
                experiment_id=("experiment",),
                variable_id=("variable",),
                variant_label=("variant_label",),
                frequency=("reporting_interval",),
                variable_branding_suffix=("processing_id",),
                realm=("realm",),
                other_terms={"custom_other": ("other_terms",)},
            ),
            id="stac-cmip7",
        ),
    ),
)
def test_every_query_class_translates_to_every_other(exp):
    """
    Note that this only tests facets which can be handled in all parameter classes

    Tests of facets specific to given parameter classes are implemented below.
    """
    start = QueryCanonical(
        model=("model",),
        institution=("institution",),
        experiment=("experiment",),
        variable=("variable",),
        variant_label=("variant_label",),
        reporting_interval=("reporting_interval",),
        processing_id=("processing_id",),
        realm=("realm",),
        other_terms={"custom_other": ("other_terms",)},
    )

    result = from_canonical(canonical=start, to=type(exp))

    assert result == exp


@pytest.mark.parametrize(
    "start_query, exp_params",
    (
        pytest.param(
            QueryCMIP5(
                project=("project",),
                model=("model",),
                institute=("institution",),
                experiment=("experiment",),
                ensemble=("variant_label",),
                variable=("variable",),
                time_frequency=("reporting_interval",),
                cmor_table=("processing_id",),
                realm=("realm",),
                product=("product",),
                other_terms={"custom_other": ("other_terms",)},
            ),
            SolrCMIP5Parameters(
                project=("project",),
                model=("model",),
                institute=("institution",),
                experiment=("experiment",),
                variable=("variable",),
                ensemble=("variant_label",),
                time_frequency=("reporting_interval",),
                cmor_table=("processing_id",),
                realm=("realm",),
                product=("product",),
                other_terms={"custom_other": ("other_terms",)},
            ),
            id="cmip5",
        ),
        pytest.param(
            QueryCMIP6(
                project=("project",),
                source_id=("model",),
                institution_id=("institution",),
                experiment_id=("experiment",),
                variant_label=("variant_label",),
                variable_id=("variable",),
                frequency=("reporting_interval",),
                grid_label=("grid_label",),
                table_id=("processing_id",),
                activity_id=("activity",),
                nominal_resolution=("resolution",),
                realm=("realm",),
                sub_experiment_id=("sub_experiment_id",),
                other_terms={"custom_other": ("other_terms",)},
            ),
            SolrCMIP6Parameters(
                project=("project",),
                source_id=("model",),
                institution_id=("institution",),
                experiment_id=("experiment",),
                variant_label=("variant_label",),
                variable_id=("variable",),
                frequency=("reporting_interval",),
                table_id=("processing_id",),
                activity_id=("activity",),
                nominal_resolution=("resolution",),
                grid_label=("grid_label",),
                realm=("realm",),
                sub_experiment_id=("sub_experiment_id",),
                other_terms={"custom_other": ("other_terms",)},
            ),
            id="cmip6",
        ),
        pytest.param(
            QueryCMIP7(
                # With the STAC API,
                # the project is the collection ID
                # and must be handled by the API generation
                # so `StacCMIP7Parameters` has no `project` facet
                # and the query must not ask for one.
                project=(),
                source_id=("model",),
                institution_id=("institution",),
                experiment_id=("experiment",),
                variant_label=("variant_label",),
                variable_id=("variable",),
                frequency=("reporting_interval",),
                grid_label=("grid_label",),
                branding_suffix=("processing_id",),
                activity_id=("activity",),
                nominal_resolution=("resolution",),
                realm=("realm",),
                temporal_label=("temporal_label",),
                vertical_label=("vertical_label",),
                horizontal_label=("horizontal_label",),
                area_label=("area_label",),
                region=("region",),
                other_terms={"custom_other": ("other_terms",)},
            ),
            StacCMIP7Parameters(
                source_id=("model",),
                institution_id=("institution",),
                experiment_id=("experiment",),
                variant_label=("variant_label",),
                variable_id=("variable",),
                frequency=("reporting_interval",),
                variable_branding_suffix=("processing_id",),
                activity_id=("activity",),
                nominal_resolution=("resolution",),
                grid_label=("grid_label",),
                realm=("realm",),
                temporal_label=("temporal_label",),
                vertical_label=("vertical_label",),
                horizontal_label=("horizontal_label",),
                area_label=("area_label",),
                region=("region",),
                other_terms={"custom_other": ("other_terms",)},
            ),
            id="cmip7-stac",
        ),
    ),
)
def test_full_query_is_supported(start_query, exp_params):
    """
    This ensures that passing of query specific facets works as expected
    """
    res = from_canonical(canonical=to_canonical(start_query), to=type(exp_params))

    assert res.model_copy(update={"source_query": None}) == exp_params


@pytest.mark.parametrize(
    "canonical, to_type, exp",
    (
        pytest.param(
            to_canonical(QueryCMIP5(model=("model",), product=("product",))),
            SolrCMIP6Parameters,
            pytest.raises(
                FacetNotExpressibleError,
                match="facet 'product' cannot be represented in SolrCMIP6Parameters",
            ),
            id="cmip5-specific-facet-cmip6-target",
        ),
        pytest.param(
            to_canonical(QueryCMIP5(model=("model",), product=("product",))),
            SolrCMIP7Parameters,
            pytest.raises(
                FacetNotExpressibleError,
                match="facet 'product' cannot be represented in SolrCMIP7Parameters",
            ),
            id="cmip5-specific-facet-cmip7-target",
        ),
        pytest.param(
            to_canonical(
                QueryCMIP6(
                    source_id=("model",), sub_experiment_id=("sub_experiment_id",)
                )
            ),
            SolrCMIP5Parameters,
            pytest.raises(
                FacetNotExpressibleError,
                match=(
                    "facet 'sub_experiment_id' cannot be represented "
                    "in SolrCMIP5Parameters"
                ),
            ),
            id="cmip6-specific-facet-cmip5-target",
        ),
        pytest.param(
            to_canonical(
                QueryCMIP6(
                    source_id=("model",), sub_experiment_id=("sub_experiment_id",)
                )
            ),
            SolrCMIP7Parameters,
            pytest.raises(
                FacetNotExpressibleError,
                match=(
                    "facet 'sub_experiment_id' cannot be represented "
                    "in SolrCMIP7Parameters"
                ),
            ),
            id="cmip6-specific-facet-cmip7-target",
        ),
        pytest.param(
            to_canonical(
                QueryCMIP7(source_id=("model",), temporal_label=("temporal_label",))
            ),
            SolrCMIP6Parameters,
            pytest.raises(
                FacetNotExpressibleError,
                match=(
                    "facet 'temporal_label' cannot be represented "
                    "in SolrCMIP6Parameters"
                ),
            ),
            id="cmip7-specific-facet-cmip6-target",
        ),
        pytest.param(
            QueryCanonical(
                project=("CMIP5",), model=("model",), activity=("activity",)
            ),
            SolrCMIP5Parameters,
            pytest.raises(
                FacetNotExpressibleError,
                match="facet 'activity' cannot be represented in SolrCMIP5Parameters",
            ),
            id="canonical-activity-solr-cmip5-target",
        ),
        pytest.param(
            QueryCanonical(
                project=("CMIP5",), model=("model",), grid_label=("grid_label",)
            ),
            SolrCMIP5Parameters,
            pytest.raises(
                FacetNotExpressibleError,
                match="facet 'grid_label' cannot be represented in SolrCMIP5Parameters",
            ),
            id="canonical-grid-label-solr-cmip5-target",
        ),
        pytest.param(
            QueryCanonical(model=("model",), resolution=("resolution",)),
            StacCMIP5Parameters,
            pytest.raises(
                FacetNotExpressibleError,
                match="facet 'resolution' cannot be represented in StacCMIP5Parameters",
            ),
            id="canonical-resolution-stac-cmip5-target",
        ),
        pytest.param(
            QueryCanonical(project=("CMIP5",), model=("model",)),
            StacCMIP5Parameters,
            pytest.raises(
                FacetNotExpressibleError,
                match="facet 'project' cannot be represented in StacCMIP5Parameters",
            ),
            id="canonical-project-stac-cmip5-target",
        ),
        pytest.param(
            QueryCanonical(project=("CMIP6",), model=("model",)),
            StacCMIP6Parameters,
            pytest.raises(
                FacetNotExpressibleError,
                match="facet 'project' cannot be represented in StacCMIP6Parameters",
            ),
            id="canonical-project-stac-cmip6-target",
        ),
        pytest.param(
            QueryCanonical(project=("CMIP7",), model=("model",)),
            StacCMIP7Parameters,
            pytest.raises(
                FacetNotExpressibleError,
                match="facet 'project' cannot be represented in StacCMIP7Parameters",
            ),
            id="canonical-project-stac-cmip7-target",
        ),
    ),
)
def test_unsupported_facets_raise(canonical, to_type, exp):
    """
    This ensures that passing of unsupported facets works as expected
    """
    with exp:
        from_canonical(canonical=canonical, to=to_type)
