"""
Known facade parameter definitions
"""
# Developer note:
#
# We considered just using the query classes directly for these specifications.
# We rejected this on two grounds:
#
# 1. it creates a coupling between queries and how we pass to the API.
#    We don't want such a coupling.
#    If the API changes in future, that shouldn't change how queries are formed.
# 2. ESGF-NG has this idea of a prefix, which isn't nicely expressed anywhere
#    on the QueryProtocol alone.
#
# This does lead to quite a lot of duplication.
# We are ok with this because of the decoupling it introduces.
# There should also be relatively little churn in this part of the code,
# making the issue even smaller.

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from pydantic import BaseModel, ConfigDict, PlainValidator

from esmporium.query import (
    FacetValues,
    FacetValuesByName,
    QueryCanonical,
    QueryFacet,
    QueryProtocol,
    SourceQuery,
    facet_spec,
    facet_values_from_attributes,
    from_canonical,
)
from esmporium.query.protocol import accept_without_validation


def get_mapping_to_query_style_facet_names(
    query_style: type[QueryProtocol], facets: set[str]
) -> dict[str, str]:
    """
    Get the mapping from input names to this query style's parameter names

    Like everywhere else, mapping is only supported from canonical names
    or from facets this query style names which have no canonical equivalent.

    All values in `facets` which cannot be mapped are left out of the result,
    so the caller can see/has to check which ones went missing.

    Parameters
    ----------
    query_style
        Query style for which to get the mapping

    facets
        Facets for which to get the mapping, if one exists

    Returns
    -------
    :
        Mapping from values in `facets` to `query_style`'s parameter name for them,
        for the facet names for which a mapping exists.
    """
    spec = facet_spec(query_style)

    res: dict[str, str] = {}
    for facet in facets:
        canonical_native = spec.canonical_to_native.get(facet)
        if canonical_native is not None:
            # In this query style, this facet name maps to a canonical name
            res[facet] = canonical_native

        elif facet in spec.query_specific_facets:
            # This facet name is specific to this query style
            # (and therefore maps to itself)
            res[facet] = facet

        # No mapping for this facet, hence do not include in the mapping.
        # We expect the caller to check for and handle this.

    return res


class DirectMappingFacadeParameters(BaseModel):
    """
    Facade parameters whose API parameter names are the query style's parameter names

    In other words, there is no extra layer on top of the base query style,
    unlike [STACFacadeParameters][(m).], which adds a collection prefix.
    """

    base_query_style: Annotated[
        type[QueryProtocol], PlainValidator(accept_without_validation)
    ]
    """See [FacadeParametersProtocol.base_query_style][esmporium.search.search_api_facade.parameters.protocol.FacadeParametersProtocol.base_query_style]."""  # noqa: E501

    def get_facet_values_request_facet_names(
        self, canonical: QueryCanonical, facets: set[str]
    ) -> set[str]:
        """See [FacadeParametersProtocol.get_facet_values_request_facet_names][esmporium.search.search_api_facade.parameters.protocol.FacadeParametersProtocol.get_facet_values_request_facet_names]."""  # noqa: E501
        res = set(self.get_mapping_to_api_facet_names(facets).values())

        return res

    def get_mapping_to_api_facet_names(self, facets: set[str]) -> dict[str, str]:
        """See [FacadeParametersProtocol.get_mapping_to_api_facet_names][esmporium.search.search_api_facade.parameters.protocol.FacadeParametersProtocol.get_mapping_to_api_facet_names]."""  # noqa: E501
        return get_mapping_to_query_style_facet_names(self.base_query_style, facets)

    def get_search_request_facet_values(
        self, canonical: QueryCanonical
    ) -> dict[str, tuple[str, ...]]:
        """See [FacadeParametersProtocol.get_search_request_facet_values][esmporium.search.search_api_facade.parameters.protocol.FacadeParametersProtocol.get_search_request_facet_values]."""  # noqa: E501
        native = from_canonical(canonical=canonical, to=self.base_query_style)

        return facet_values_from_attributes(native)


class ESGF1CMIP5ParametersQueryStyle(BaseModel):
    """CMIP5 facet values under their ESGF1 parameter names"""

    model_config = ConfigDict(extra="forbid")

    project: Annotated[FacetValues, QueryFacet("project")] = ()
    """See [Dataset.project][esmporium.db.schema.Dataset.project]."""

    model: Annotated[FacetValues, QueryFacet("model")] = ()
    """See [Dataset.model][esmporium.db.schema.Dataset.model]."""

    institute: Annotated[FacetValues, QueryFacet("institution")] = ()
    """See [Dataset.institution][esmporium.db.schema.Dataset.institution]."""

    experiment: Annotated[FacetValues, QueryFacet("experiment")] = ()
    """See [Dataset.experiment][esmporium.db.schema.Dataset.experiment]."""

    variable: Annotated[FacetValues, QueryFacet("variable")] = ()
    """See [Dataset.variable][esmporium.db.schema.Dataset.variable]."""

    ensemble: Annotated[FacetValues, QueryFacet("variant_label")] = ()
    """See [Dataset.variant_label][esmporium.db.schema.Dataset.variant_label]."""

    time_frequency: Annotated[FacetValues, QueryFacet("reporting_interval")] = ()
    """See [Dataset.reporting_interval][esmporium.db.schema.Dataset.reporting_interval]."""  # noqa: E501

    cmor_table: Annotated[FacetValues, QueryFacet("processing_id")] = ()
    """See [Dataset.processing_id][esmporium.db.schema.Dataset.processing_id]."""

    realm: Annotated[FacetValues, QueryFacet("realm")] = ()
    """See [Dataset.realm][esmporium.query.canonical_query.QueryCanonical.realm]."""

    product: Annotated[FacetValues, QueryFacet(None)] = ()
    """See [QueryCMIP5.product][esmporium.query.known_queries.QueryCMIP5.product]."""

    other_terms: FacetValuesByName = {}
    """See [Query.other_terms][esmporium.query.known_queries.Query.other_terms]."""

    source_query: SourceQuery = None
    """See [Query.source_query][esmporium.query.known_queries.Query.source_query]."""


ESGF1_CMIP5_FACADE_PARAMETERS = DirectMappingFacadeParameters(
    base_query_style=ESGF1CMIP5ParametersQueryStyle
)
"""Parameters for CMIP5 with an ESGF1 API"""


class ESGF1CMIP6ParametersQueryStyle(BaseModel):
    """CMIP6 facet values under their ESGF1 parameter names"""

    model_config = ConfigDict(extra="forbid")

    project: Annotated[FacetValues, QueryFacet("project")] = ()
    """See [Dataset.project][esmporium.db.schema.Dataset.project]."""

    source_id: Annotated[FacetValues, QueryFacet("model")] = ()
    """See [Dataset.model][esmporium.db.schema.Dataset.model]."""

    institution_id: Annotated[FacetValues, QueryFacet("institution")] = ()
    """See [Dataset.institution][esmporium.db.schema.Dataset.institution]."""

    experiment_id: Annotated[FacetValues, QueryFacet("experiment")] = ()
    """See [Dataset.experiment][esmporium.db.schema.Dataset.experiment]."""

    variant_label: Annotated[FacetValues, QueryFacet("variant_label")] = ()
    """See [Dataset.variant_label][esmporium.db.schema.Dataset.variant_label]."""

    variable_id: Annotated[FacetValues, QueryFacet("variable")] = ()
    """See [Dataset.variable][esmporium.db.schema.Dataset.variable]."""

    frequency: Annotated[FacetValues, QueryFacet("reporting_interval")] = ()
    """See [Dataset.reporting_interval][esmporium.db.schema.Dataset.reporting_interval]."""  # noqa: E501

    table_id: Annotated[FacetValues, QueryFacet("processing_id")] = ()
    """See [Dataset.processing_id][esmporium.db.schema.Dataset.processing_id]."""

    activity_id: Annotated[FacetValues, QueryFacet("activity")] = ()
    """See [Dataset.activity][esmporium.query.canonical_query.QueryCanonical.activity]."""  # noqa: E501

    nominal_resolution: Annotated[FacetValues, QueryFacet("resolution")] = ()
    """See [Dataset.resolution][esmporium.query.canonical_query.QueryCanonical.resolution]."""  # noqa: E501

    grid_label: Annotated[FacetValues, QueryFacet("grid_label")] = ()
    """See [Dataset.grid_label][esmporium.db.schema.Dataset.grid_label]."""

    realm: Annotated[FacetValues, QueryFacet("realm")] = ()
    """See [Dataset.realm][esmporium.query.canonical_query.QueryCanonical.realm]."""

    sub_experiment_id: Annotated[FacetValues, QueryFacet(None)] = ()
    """See [QueryCMIP6.sub_experiment_id][esmporium.query.known_queries.QueryCMIP6.sub_experiment_id]."""  # noqa: E501

    other_terms: FacetValuesByName = {}
    """See [Query.other_terms][esmporium.query.known_queries.Query.other_terms]."""

    source_query: SourceQuery = None
    """See [Query.source_query][esmporium.query.known_queries.Query.source_query]."""


ESGF1_CMIP6_FACADE_PARAMETERS = DirectMappingFacadeParameters(
    base_query_style=ESGF1CMIP6ParametersQueryStyle
)
"""Parameters for CMIP6 with an ESGF1 API"""


class ESGF1CMIP7ParametersQueryStyle(BaseModel):
    """CMIP7 facet values under their ESGF1 parameter names"""

    model_config = ConfigDict(extra="forbid")

    project: Annotated[FacetValues, QueryFacet("project")] = ()
    """See [Dataset.project][esmporium.db.schema.Dataset.project]."""

    source_id: Annotated[FacetValues, QueryFacet("model")] = ()
    """See [Dataset.model][esmporium.db.schema.Dataset.model]."""

    institution_id: Annotated[FacetValues, QueryFacet("institution")] = ()
    """See [Dataset.institution][esmporium.db.schema.Dataset.institution]."""

    experiment_id: Annotated[FacetValues, QueryFacet("experiment")] = ()
    """See [Dataset.experiment][esmporium.db.schema.Dataset.experiment]."""

    variable_id: Annotated[FacetValues, QueryFacet("variable")] = ()
    """See [Dataset.variable][esmporium.db.schema.Dataset.variable]."""

    variant_label: Annotated[FacetValues, QueryFacet("variant_label")] = ()
    """See [Dataset.variant_label][esmporium.db.schema.Dataset.variant_label]."""

    frequency: Annotated[FacetValues, QueryFacet("reporting_interval")] = ()
    """See [Dataset.reporting_interval][esmporium.db.schema.Dataset.reporting_interval]."""  # noqa: E501

    variable_branding_suffix: Annotated[FacetValues, QueryFacet("processing_id")] = ()
    """See [Dataset.processing_id][esmporium.db.schema.Dataset.processing_id]."""

    activity_id: Annotated[FacetValues, QueryFacet("activity")] = ()
    """See [Dataset.activity][esmporium.query.canonical_query.QueryCanonical.activity]."""  # noqa: E501

    grid_label: Annotated[FacetValues, QueryFacet("grid_label")] = ()
    """See [Dataset.grid_label][esmporium.db.schema.Dataset.grid_label]."""

    nominal_resolution: Annotated[FacetValues, QueryFacet("resolution")] = ()
    """See [Dataset.resolution][esmporium.query.canonical_query.QueryCanonical.resolution]."""  # noqa: E501

    realm: Annotated[FacetValues, QueryFacet("realm")] = ()
    """See [Dataset.realm][esmporium.query.canonical_query.QueryCanonical.realm]."""

    temporal_label: Annotated[FacetValues, QueryFacet(None)] = ()
    """See [QueryCMIP7.temporal_label][esmporium.query.known_queries.QueryCMIP7.temporal_label]."""  # noqa: E501

    vertical_label: Annotated[FacetValues, QueryFacet(None)] = ()
    """See [QueryCMIP7.vertical_label][esmporium.query.known_queries.QueryCMIP7.vertical_label]."""  # noqa: E501

    horizontal_label: Annotated[FacetValues, QueryFacet(None)] = ()
    """See [QueryCMIP7.horizontal_label][esmporium.query.known_queries.QueryCMIP7.horizontal_label]."""  # noqa: E501

    area_label: Annotated[FacetValues, QueryFacet(None)] = ()
    """See [QueryCMIP7.area_label][esmporium.query.known_queries.QueryCMIP7.area_label]."""  # noqa: E501

    region: Annotated[FacetValues, QueryFacet(None)] = ()
    """See [QueryCMIP7.region][esmporium.query.known_queries.QueryCMIP7.region]."""

    other_terms: FacetValuesByName = {}
    """See [Query.other_terms][esmporium.query.known_queries.Query.other_terms]."""

    source_query: SourceQuery = None
    """See [Query.source_query][esmporium.query.known_queries.Query.source_query]."""


ESGF1_CMIP7_FACADE_PARAMETERS = DirectMappingFacadeParameters(
    base_query_style=ESGF1CMIP7ParametersQueryStyle
)
"""Parameters for CMIP7 with an ESGF1 API"""


class OneProjectRequiredError(ValueError):
    """
    Raised when exactly one project is required, but there isn't exactly one project
    """

    def __init__(self, query: QueryCanonical, projects: tuple[str, ...]) -> None:
        """
        Initialise the error

        Parameters
        ----------
        query
            Query which does not specify exactly one project

        projects
            The projects which were asked for
        """
        self.query = query
        self.projects = projects
        super().__init__(
            f"Received {len(projects)}: {projects}. "
            "We need the query to have exactly one project. "
            f"{query=}"
        )


class ProjectPrefixMismatchError(ValueError):
    """
    Raised when there is a mismatch between project and prefix

    ESGF STAC APIs name their collections' properties with a specific prefix,
    so a mismatch will mean that we build requests which cannot match anything.
    Without this error, no results would come back and nothing would say why.
    """

    def __init__(
        self, project: str, prefix: str, facade_parameters: STACFacadeParameters
    ) -> None:
        """
        Initialise the error

        Parameters
        ----------
        project
            The project which was asked for

        prefix
            The prefix we expect

        facade_parameters
            The facade parameter definition
        """
        self.project = project
        self.prefix = prefix
        self.facade_parameters = facade_parameters
        super().__init__(
            f"For {project=}, we expected {prefix=}."
            f"You may need to create a new {type(facade_parameters).__name__} "
            "instance and inject different checks of consistency "
            "between projects and prefixes. "
            f"For reference, {facade_parameters=}."
        )


def identity_string(in_string: str) -> str:
    """
    Return the input string unchanged

    Parameters
    ----------
    in_string
        Input value

    Returns
    -------
    :
        `in_string`
    """
    return in_string


class STACFacadeParameters(BaseModel):
    """
    Facade parameters for ESGF-NG STAC facades

    In these, the API parameter names are the query style's parameter names
    with a collection prefix on the front,
    and project must be handled carefully when being passed.
    """

    prefix: str
    """
    Prefix to apply when creating the API parameter names
    """

    base_query_style: Annotated[
        type[QueryProtocol], PlainValidator(accept_without_validation)
    ]
    """See [FacadeParametersProtocol.base_query_style][esmporium.search.search_api_facade.parameters.protocol.FacadeParametersProtocol.base_query_style]."""  # noqa: E501

    project_to_collection_converter: Callable[[str], str] = identity_string
    """
    Function which converts a project name into a collection name
    """

    project_to_prefix_converter: Callable[[str], str] = str.lower
    """
    Function which converts a project name into the expected facet name prefix

    This is used to check consistency between projects specified in queries
    and `self.prefix`.
    """

    def get_facet_values_request_facet_names(
        self, canonical: QueryCanonical, facets: set[str]
    ) -> set[str]:
        """See [FacadeParametersProtocol.get_facet_values_request_facet_names][esmporium.search.search_api_facade.parameters.protocol.FacadeParametersProtocol.get_facet_values_request_facet_names]."""  # noqa: E501
        # check alignment of project and prefix
        self.get_collection(canonical)

        res = set(self.get_mapping_to_api_facet_names(facets).values())

        return res

    def get_mapping_to_api_facet_names(self, facets: set[str]) -> dict[str, str]:
        """See [FacadeParametersProtocol.get_mapping_to_api_facet_names][esmporium.search.search_api_facade.parameters.protocol.FacadeParametersProtocol.get_mapping_to_api_facet_names]."""  # noqa: E501
        raw = get_mapping_to_query_style_facet_names(self.base_query_style, facets)
        res = {
            facet_name: f"{self.prefix}:{base_query_name}"
            for facet_name, base_query_name in raw.items()
        }

        return res

    def get_collection(self, canonical: QueryCanonical) -> str:
        """
        Get the STAC collection to pass to the ESGF-NG API

        Parameters
        ----------
        canonical
            Canonical query from which to get the collection

        Returns
        -------
        :
            STAC collection to pass to the API

        Raises
        ------
        OneProjectRequiredError
            `canonical` has anything other than exactly one project

            The way the ESGF-NG API is set up,
            only one collection can be specified at a time.
            Given that project drives the collection,
            this means we can only handle one project at a time.

        ProjectPrefixMismatchError
            `canonical.project` disagrees with `self.prefix`
        """
        if len(canonical.project) != 1:
            raise OneProjectRequiredError(canonical, canonical.project)

        project = canonical.project[0]
        if self.project_to_prefix_converter(project) != self.prefix:
            raise ProjectPrefixMismatchError(project, self.prefix, self)

        collection = self.project_to_collection_converter(project)

        return collection

    def get_search_request_facet_values(
        self, canonical: QueryCanonical
    ) -> dict[str, tuple[str, ...]]:
        """See [FacadeParametersProtocol.get_search_request_facet_values][esmporium.search.search_api_facade.parameters.protocol.FacadeParametersProtocol.get_search_request_facet_values]."""  # noqa: E501
        collection = self.get_collection(canonical)
        without_project = canonical.model_copy(update={"project": ()})
        native = from_canonical(canonical=without_project, to=self.base_query_style)

        # Special project handling
        facet_values: dict[str, tuple[str, ...]] = {"collection": (collection,)}
        # Other facets are 'normal'
        for facet_name, values in facet_values_from_attributes(native).items():
            facet_values[f"{self.prefix}:{facet_name}"] = values

        return facet_values


class ESGFNGCMIP5ParametersQueryStyle(BaseModel):
    """CMIP5 facet values under their ESGF-NG parameter names"""

    model_config = ConfigDict(extra="forbid")

    model: Annotated[FacetValues, QueryFacet("model")] = ()
    """See [Dataset.model][esmporium.db.schema.Dataset.model]."""

    institute: Annotated[FacetValues, QueryFacet("institution")] = ()
    """See [Dataset.institution][esmporium.db.schema.Dataset.institution]."""

    experiment: Annotated[FacetValues, QueryFacet("experiment")] = ()
    """See [Dataset.experiment][esmporium.db.schema.Dataset.experiment]."""

    variable: Annotated[FacetValues, QueryFacet("variable")] = ()
    """See [Dataset.variable][esmporium.db.schema.Dataset.variable]."""

    ensemble: Annotated[FacetValues, QueryFacet("variant_label")] = ()
    """See [Dataset.variant_label][esmporium.db.schema.Dataset.variant_label]."""

    time_frequency: Annotated[FacetValues, QueryFacet("reporting_interval")] = ()
    """See [Dataset.reporting_interval][esmporium.db.schema.Dataset.reporting_interval]."""  # noqa: E501

    cmor_table: Annotated[FacetValues, QueryFacet("processing_id")] = ()
    """See [Dataset.processing_id][esmporium.db.schema.Dataset.processing_id]."""

    realm: Annotated[FacetValues, QueryFacet("realm")] = ()
    """See [Dataset.realm][esmporium.query.canonical_query.QueryCanonical.realm]."""

    product: Annotated[FacetValues, QueryFacet(None)] = ()
    """See [QueryCMIP5.product][esmporium.query.known_queries.QueryCMIP5.product]."""

    other_terms: FacetValuesByName = {}
    """See [Query.other_terms][esmporium.query.known_queries.Query.other_terms]."""

    source_query: SourceQuery = None
    """See [Query.source_query][esmporium.query.known_queries.Query.source_query]."""


ESGFNG_CMIP5_FACADE_PARAMETERS = STACFacadeParameters(
    base_query_style=ESGFNGCMIP5ParametersQueryStyle,
    prefix="cmip5",
)
"""Parameters for CMIP5 with an ESGF-NG API"""


class ESGFNGCMIP6ParametersQueryStyle(BaseModel):
    """CMIP6 facet values under their ESGF-NG parameter names"""

    model_config = ConfigDict(extra="forbid")

    source_id: Annotated[FacetValues, QueryFacet("model")] = ()
    """See [Dataset.model][esmporium.db.schema.Dataset.model]."""

    institution_id: Annotated[FacetValues, QueryFacet("institution")] = ()
    """See [Dataset.institution][esmporium.db.schema.Dataset.institution]."""

    experiment_id: Annotated[FacetValues, QueryFacet("experiment")] = ()
    """See [Dataset.experiment][esmporium.db.schema.Dataset.experiment]."""

    variant_label: Annotated[FacetValues, QueryFacet("variant_label")] = ()
    """See [Dataset.variant_label][esmporium.db.schema.Dataset.variant_label]."""

    variable_id: Annotated[FacetValues, QueryFacet("variable")] = ()
    """See [Dataset.variable][esmporium.db.schema.Dataset.variable]."""

    frequency: Annotated[FacetValues, QueryFacet("reporting_interval")] = ()
    """See [Dataset.reporting_interval][esmporium.db.schema.Dataset.reporting_interval]."""  # noqa: E501

    table_id: Annotated[FacetValues, QueryFacet("processing_id")] = ()
    """See [Dataset.processing_id][esmporium.db.schema.Dataset.processing_id]."""

    activity_id: Annotated[FacetValues, QueryFacet("activity")] = ()
    """See [Dataset.activity][esmporium.query.canonical_query.QueryCanonical.activity]."""  # noqa: E501

    nominal_resolution: Annotated[FacetValues, QueryFacet("resolution")] = ()
    """See [Dataset.resolution][esmporium.query.canonical_query.QueryCanonical.resolution]."""  # noqa: E501

    grid_label: Annotated[FacetValues, QueryFacet("grid_label")] = ()
    """See [Dataset.grid_label][esmporium.db.schema.Dataset.grid_label]."""

    realm: Annotated[FacetValues, QueryFacet("realm")] = ()
    """See [Dataset.realm][esmporium.query.canonical_query.QueryCanonical.realm]."""

    sub_experiment_id: Annotated[FacetValues, QueryFacet(None)] = ()
    """See [QueryCMIP6.sub_experiment_id][esmporium.query.known_queries.QueryCMIP6.sub_experiment_id]."""  # noqa: E501

    other_terms: FacetValuesByName = {}
    """See [Query.other_terms][esmporium.query.known_queries.Query.other_terms]."""

    source_query: SourceQuery = None
    """See [Query.source_query][esmporium.query.known_queries.Query.source_query]."""


ESGFNG_CMIP6_FACADE_PARAMETERS = STACFacadeParameters(
    base_query_style=ESGFNGCMIP6ParametersQueryStyle,
    prefix="cmip6",
)
"""Parameters for CMIP6 with an ESGF-NG API"""


class ESGFNGCMIP7ParametersQueryStyle(BaseModel):
    """CMIP7 facet values under their ESGF-NG parameter names"""

    model_config = ConfigDict(extra="forbid")

    source_id: Annotated[FacetValues, QueryFacet("model")] = ()
    """See [Dataset.model][esmporium.db.schema.Dataset.model]."""

    institution_id: Annotated[FacetValues, QueryFacet("institution")] = ()
    """See [Dataset.institution][esmporium.db.schema.Dataset.institution]."""

    experiment_id: Annotated[FacetValues, QueryFacet("experiment")] = ()
    """See [Dataset.experiment][esmporium.db.schema.Dataset.experiment]."""

    variant_label: Annotated[FacetValues, QueryFacet("variant_label")] = ()
    """See [Dataset.variant_label][esmporium.db.schema.Dataset.variant_label]."""

    variable_id: Annotated[FacetValues, QueryFacet("variable")] = ()
    """See [Dataset.variable][esmporium.db.schema.Dataset.variable]."""

    frequency: Annotated[FacetValues, QueryFacet("reporting_interval")] = ()
    """See [Dataset.reporting_interval][esmporium.db.schema.Dataset.reporting_interval]."""  # noqa: E501

    # Note for developers:
    # if anyone ever asks why we don't just use the query classes directly,
    # this is an example of why.
    # The query (based on CMIP7 guidance) uses `branding_suffix`,
    # but the API uses `variable_branding_suffix`, which isn't the same.
    variable_branding_suffix: Annotated[FacetValues, QueryFacet("processing_id")] = ()
    """See [Dataset.processing_id][esmporium.db.schema.Dataset.processing_id]."""

    activity_id: Annotated[FacetValues, QueryFacet("activity")] = ()
    """See [Dataset.activity][esmporium.query.canonical_query.QueryCanonical.activity]."""  # noqa: E501

    nominal_resolution: Annotated[FacetValues, QueryFacet("resolution")] = ()
    """See [Dataset.resolution][esmporium.query.canonical_query.QueryCanonical.resolution]."""  # noqa: E501

    grid_label: Annotated[FacetValues, QueryFacet("grid_label")] = ()
    """See [Dataset.grid_label][esmporium.db.schema.Dataset.grid_label]."""

    realm: Annotated[FacetValues, QueryFacet("realm")] = ()
    """See [Dataset.realm][esmporium.query.canonical_query.QueryCanonical.realm]."""

    temporal_label: Annotated[FacetValues, QueryFacet(None)] = ()
    """See [QueryCMIP7.temporal_label][esmporium.query.known_queries.QueryCMIP7.temporal_label]."""  # noqa: E501

    vertical_label: Annotated[FacetValues, QueryFacet(None)] = ()
    """See [QueryCMIP7.vertical_label][esmporium.query.known_queries.QueryCMIP7.vertical_label]."""  # noqa: E501

    horizontal_label: Annotated[FacetValues, QueryFacet(None)] = ()
    """See [QueryCMIP7.horizontal_label][esmporium.query.known_queries.QueryCMIP7.horizontal_label]."""  # noqa: E501

    area_label: Annotated[FacetValues, QueryFacet(None)] = ()
    """See [QueryCMIP7.area_label][esmporium.query.known_queries.QueryCMIP7.area_label]."""  # noqa: E501

    region: Annotated[FacetValues, QueryFacet(None)] = ()
    """See [QueryCMIP7.region][esmporium.query.known_queries.QueryCMIP7.region]."""

    other_terms: FacetValuesByName = {}
    """See [Query.other_terms][esmporium.query.known_queries.Query.other_terms]."""

    source_query: SourceQuery = None
    """See [Query.source_query][esmporium.query.known_queries.Query.source_query]."""


ESGFNG_CMIP7_FACADE_PARAMETERS = STACFacadeParameters(
    base_query_style=ESGFNGCMIP7ParametersQueryStyle,
    prefix="cmip7",
)
"""Parameters for CMIP7 with an ESGF-NG API"""
