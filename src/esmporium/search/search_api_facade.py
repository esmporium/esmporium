"""
Search API facade

This contains our facades to search APIs.
These facades are introduced to add more robust
query creation, result parsing and error handling.
Complete documentation of this will be added in future.

A facade pairs a *query style* (the vocabulary a project is written in,
e.g. [SolrCMIP6Parameters][(m).SolrCMIP6Parameters])
with a *search API* (the wire format spoken by a family of endpoints,
e.g. [SearchAPIESGF1Solr][esmporium.search.apis.SearchAPIESGF1Solr]).
The query style is the facade's own concern:
it is the facade which turns a canonical query into the names and shapes
a search API speaks, and which turns the answer back into the canonical vocabulary.
The search API knows nothing about canonical queries;
it only knows how to encode a request and decode a response for its wire format.
Keeping the two layers visibly distinct is deliberate:
every consumer reaches through to `facade.search_api.host` explicitly,
rather than the facade re-exposing it, so it is always clear which layer you are on.
"""
# TODO: devs - add more complete docs in a follow up PR

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, Protocol, cast

from pydantic import BaseModel, ConfigDict
from tenacity import Retrying

from esmporium.query import (
    FacetNotExpressibleError,
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
from esmporium.search.apis import (
    Request,
    SearchAPI,
    SearchAPIESGF1Solr,
    SearchAPIESGF15BridgeSolr,
    SearchAPIESGFNGSTAC,
)
from esmporium.search.retry import build_transient_retrying


class STACParams(QueryProtocol, Protocol):
    """
    A STAC parameter class

    The prefix lives with the parameter class because it co-varies exactly with
    the project each parameter class is paired to
    (because, with the STAC API, you can only search one project at a time).

    On STAC parameter classes, there should also be no `project` field.
    The prefix implicitly defines the supported project.
    When we build queries, the builder should make sure that the query
    aligns with the project (i.e. prefix).
    """

    prefix: ClassVar[str]
    """
    The prefix to put in front of each field name to get the API property name

    This also implicitly defines the project which can be searched
    using parameters from this class and STAC APIs which use this parameter class.
    With STAC, there is a tight coupling between prefixes i.e. projects and searches.
    As a result, each STAC search request can only search a single project,
    which isn't the case with ESGF1
    (having said this, for better error messaging related to facet names,
    our ESGF1 search APIs are also tightly coupled to specific projects).
    """


class UnaskableFacetError(AssertionError):
    """
    Raised when we ask for a facet we could never have asked the API about

    This error means a facets request was built
    and sent naming a facet the vocabulary has no name for, which
    [check_facets_expressible][(m).check_facets_expressible]
    exists to prevent.
    Raising this means something got past the checks in
    [check_facets_expressible][(m).check_facets_expressible].
    """

    def __init__(self, params: type[QueryProtocol], facets: set[str]) -> None:
        """
        Initialise the error

        Parameters
        ----------
        params
            The vocabulary the response is written in

        facets
            The facets which could not have been asked about,
            named as [native_facet_names][(m).native_facet_names] describes
        """
        self.params = params
        self.facets = facets
        named = ", ".join(sorted(facets))
        super().__init__(
            f"{params.__name__} has no name for {named}, "
            "so we cannot have asked the API about it and it cannot be in this "
            "response. This request should never have been built."
        )


class OneProjectRequiredError(ValueError):
    """
    Raised when a STAC search is given anything other than exactly one project

    On these APIs the project is the collection being searched,
    and a search is scoped to a single collection,
    so "no project" and "several projects" are both unanswerable.
    """

    def __init__(self, projects: tuple[str, ...]) -> None:
        """
        Initialise the error

        Parameters
        ----------
        projects
            The projects which were asked for
        """
        self.projects = projects
        super().__init__(
            "A STAC search is scoped to one collection, and therefore to one "
            f"project. Received {len(projects)}: {projects}. "
            "Search each project separately and combine the results."
        )


class ProjectPrefixMismatchError(ValueError):
    """
    Raised when a STAC vocabulary is used to search a project it does not describe

    Each collection names its properties with its own prefix
    (`cmip6:` for CMIP6, `cmip6plus:` for CMIP6Plus, and so on),
    so a vocabulary used against the wrong collection
    builds a filter which cannot match anything.
    Nothing comes back, and nothing says why, which is the worst of both worlds.
    """

    def __init__(self, project: str, params: type[STACParams]) -> None:
        """
        Initialise the error

        Parameters
        ----------
        project
            The project which was asked for

        params
            The vocabulary which was going to be used to search it
        """
        self.project = project
        self.params = params
        super().__init__(
            f"{params.__name__} writes its properties with the "
            f"{params.prefix!r} prefix, so it cannot describe the {project!r} "
            "collection. Use the parameter class for that project."
        )


def native_facet_names(params: type[QueryProtocol], facets: set[str]) -> dict[str, str]:
    """
    Work out what a vocabulary calls each of the facets being asked about

    Parameters
    ----------
    params
        The vocabulary to translate into

    facets
        The facets to translate, named as above

    Returns
    -------
    :
        The name `params` uses for each facet it can express,
        keyed by the name it was asked for

        Facets `params` cannot express are left out,
        so the caller can see which ones went missing.
    """
    spec = facet_spec(params)

    res: dict[str, str] = {}
    for facet in facets:
        canonical_native = spec.canonical_to_native.get(facet)
        if canonical_native is not None:
            res[facet] = canonical_native

        elif facet in spec.query_specific_facets:
            res[facet] = facet

        # Facets not handled above are dropped,
        # we expect the caller to check for and handle this.

    return res


def unexpressible_facets(params: type[QueryProtocol], facets: set[str]) -> set[str]:
    """
    Work out which of the given facets a vocabulary has no name for

    Parameters
    ----------
    params
        The vocabulary to check against

    facets
        The facets to check, named as
        [native_facet_names][(m).native_facet_names] describes

    Returns
    -------
    :
        The facets which `params` cannot express
    """
    return set(facets) - set(native_facet_names(params, facets))


def check_facets_expressible(params: type[QueryProtocol], facets: set[str]) -> None:
    """
    Check that a vocabulary can express every facet being asked about

    Parameters
    ----------
    params
        The vocabulary to check against

    facets
        The facets to check, named as
        [native_facet_names][(m).native_facet_names] describes

    Raises
    ------
    FacetNotExpressibleError
        `params` cannot express one or more of `facets`
    """
    unexpressible = unexpressible_facets(params, facets)
    if unexpressible:
        raise FacetNotExpressibleError(unexpressible, facet_spec(params).name)


def check_facets_askable(params: type[QueryProtocol], facets: set[str]) -> None:
    """
    Check that every facet being read is one we could have asked the API about

    Unlike [check_facets_expressible][(m).check_facets_expressible],
    which guards the request we are about to build,
    this guards a response we have already been given.
    Getting here with a facet this vocabulary cannot express
    means a request was built and sent that never should have been,
    so the fault is ours rather than the caller's.

    Parameters
    ----------
    params
        The vocabulary the response is written in

    facets
        The facets being read, named as
        [native_facet_names][(m).native_facet_names] describes

    Raises
    ------
    UnaskableFacetError
        `params` cannot express one of `facets`
    """
    unexpressible = unexpressible_facets(params, facets)
    if unexpressible:
        raise UnaskableFacetError(params, unexpressible)


def stac_collection(canonical: QueryCanonical, params: type[STACParams]) -> str:
    """
    Work out which STAC collection a query is asking about

    Parameters
    ----------
    canonical
        The query whose project to read

    params
        The vocabulary the query is going to be written in

    Returns
    -------
    :
        The collection to search

        This is the project exactly as the caller wrote it,
        because the caller knows what they typed
        and second-guessing their capitalisation would only hide their mistakes.

    Raises
    ------
    OneProjectRequiredError
        `canonical` does not name exactly one project

    ProjectPrefixMismatchError
        `params` does not describe the project `canonical` names
    """
    if len(canonical.project) != 1:
        raise OneProjectRequiredError(canonical.project)

    collection = canonical.project[0]
    if collection.lower() != params.prefix:
        raise ProjectPrefixMismatchError(collection, params)

    return collection


# The parameters used for different projects with different APIs.
# These are almost identical to the queries,
# but we have both definitions because there can be subtle differences
# and we don't want to couple these things unnecessarily.
#
# We also pre-build various facades at the end of this file
# so users don't have to write this up themselves if they don't want.
#
# Maybe move these into their own module.
class SolrCMIP5Parameters(BaseModel):
    """CMIP5 facet values under their ESGF1/Solr parameter names"""

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

    def facet_values(self) -> dict[str, tuple[str, ...]]:
        """Facets that are set, keyed by this class's own (wire) names"""
        return facet_values_from_attributes(self)


class SolrCMIP6Parameters(BaseModel):
    """
    CMIP6 facet values under their ESGF1/Solr parameter names
    """

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

    def facet_values(self) -> dict[str, tuple[str, ...]]:
        """Facets that are set, keyed by this class's own (wire) names"""
        return facet_values_from_attributes(self)


class SolrCMIP7Parameters(BaseModel):
    """
    CMIP7 facet values under their ESGF1/Solr parameter names
    """

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

    branding_suffix: Annotated[FacetValues, QueryFacet("processing_id")] = ()
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

    def facet_values(self) -> dict[str, tuple[str, ...]]:
        """Facets that are set, keyed by this class's own (wire) names"""
        return facet_values_from_attributes(self)


class STACCMIP5Parameters(BaseModel):
    """
    CMIP5 facet values under their ESGF-NG/STAC property stems
    """

    model_config = ConfigDict(extra="forbid")

    prefix: ClassVar[str] = "cmip5"
    """See [STACParams.prefix][(m).STACParams.prefix]."""

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

    def facet_values(self) -> dict[str, tuple[str, ...]]:
        """Facets that are set, keyed by this class's own (stem) names"""
        return facet_values_from_attributes(self)


class STACCMIP6Parameters(BaseModel):
    """CMIP6 facet values under their ESGF-NG/STAC property stems"""

    model_config = ConfigDict(extra="forbid")

    prefix: ClassVar[str] = "cmip6"
    """See [STACParams.prefix][(m).STACParams.prefix]."""

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

    def facet_values(self) -> dict[str, tuple[str, ...]]:
        """Facets that are set, keyed by this class's own (stem) names"""
        return facet_values_from_attributes(self)


class STACCMIP7Parameters(BaseModel):
    """
    CMIP7 facet values under their ESGF-NG/STAC property stems
    """

    model_config = ConfigDict(extra="forbid")

    prefix: ClassVar[str] = "cmip7"
    """See [STACParams.prefix][(m).STACParams.prefix]."""

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

    def facet_values(self) -> dict[str, tuple[str, ...]]:
        """Facets that are set, keyed by this class's own (stem) names"""
        return facet_values_from_attributes(self)


@dataclass(frozen=True)
class SearchAPIFacade:
    """
    A search API facade

    This turns the search API from something which will accept queries for any project,
    into something which will only accept queries for a single project.
    This makes query creation, result parsing and error handling much more robust,
    at the price of having to make multiple queries
    if we want to search more than one project
    (in practice this is a tiny price to pay,
    so we deliberately make this tradeoff throughout the package).

    The facade owns the vocabulary translation:
    [build_search_request][(c).build_search_request] and
    [build_get_facet_values_request][(c).build_get_facet_values_request]
    turn a canonical query into a request in `search_api`'s wire format, and
    [parse_facet_values][(c).parse_facet_values] and
    [parse_facet_patterns][(c).parse_facet_patterns]
    read `search_api`'s answer back into the canonical vocabulary.
    """

    query_style: type[QueryProtocol]
    """
    The query style that this facade uses
    """

    search_api: SearchAPI
    """
    The search API for which we are providing a facade
    """

    @property
    def _stac_prefix(self) -> str | None:
        """
        The STAC property prefix for this facade, or `None` if it is not STAC-shaped

        A STAC vocabulary carries a `prefix` (e.g. `cmip6`) which every property
        name is written under; a Solr vocabulary does not, and names its project
        as an ordinary facet. This tells the two apart.
        """
        return getattr(self.query_style, "prefix", None)

    def _wire_facet_names(self, facets: set[str]) -> dict[str, str]:
        """
        Map each canonical facet to the name `search_api` speaks it under

        For a STAC vocabulary the wire name carries the collection's prefix
        (`cmip6:experiment_id`); for a Solr vocabulary it is just the parameter
        name (`experiment_id`). Facets this vocabulary cannot express are dropped.
        """
        names = native_facet_names(self.query_style, facets)
        prefix = self._stac_prefix
        if prefix is not None:
            return {canonical: f"{prefix}:{stem}" for canonical, stem in names.items()}

        return names

    @staticmethod
    def _single_project(canonical: QueryCanonical) -> str:
        """Pull the one project out of a query, or explain why we cannot"""
        if len(canonical.project) != 1:
            msg = (
                "We can only unambiguously scope a facet-values request "
                "if there is exactly one project, "
                f"received: {canonical.project}"
            )
            raise ValueError(msg)

        return canonical.project[0]

    def askable_facets(self, facets: set[str]) -> set[str]:
        """
        Get the subset of `facets` this facade's vocabulary can express

        The rest cannot be asked about (there is no name to ask them under),
        so the caller is expected to record them as unchecked rather than
        pretend a source was silent about them.

        Parameters
        ----------
        facets
            The facets to filter, named canonically

        Returns
        -------
        :
            The facets `query_style` can express, named canonically
        """
        return set(native_facet_names(self.query_style, facets))

    def build_search_request(self, canonical: QueryCanonical, limit: int) -> Request:
        """
        Build a search request for a canonical query

        Parameters
        ----------
        canonical
            Query to render

        limit
            The page size to ask for,
            i.e. the maximum number of records in one response.

        Returns
        -------
        :
            The request to send to `search_api`

        Raises
        ------
        FacetNotExpressibleError
            `canonical` sets a facet this facade's vocabulary cannot express

        LimitOutOfRangeError
            `limit` is outside the range `search_api` accepts

        OneProjectRequiredError
            A STAC facade was given anything other than exactly one project

        ProjectPrefixMismatchError
            A STAC facade was given a project its vocabulary does not describe
        """
        prefix = self._stac_prefix
        if prefix is None:
            native = from_canonical(canonical=canonical, to=self.query_style)
            return self.search_api.build_search_request(native.facet_values(), limit)

        # With this API, the project is the collection ID rather than a property,
        # so it is translated out of the query and into a `collection` facet,
        # and every other property carries the collection's prefix.
        query_style = cast("type[STACParams]", self.query_style)
        collection = stac_collection(canonical, query_style)
        without_project = canonical.model_copy(update={"project": ()})
        native = from_canonical(canonical=without_project, to=self.query_style)

        facet_values: dict[str, tuple[str, ...]] = {"collection": (collection,)}
        for stem, values in native.facet_values().items():
            facet_values[f"{prefix}:{stem}"] = values

        return self.search_api.build_search_request(facet_values, limit)

    def build_get_facet_values_request(
        self, canonical: QueryCanonical, facets: set[str]
    ) -> Request:
        """
        Build a request which lists the values of the given facets

        The facade is already scoped to one project, so it derives the project
        from `canonical` rather than taking it as an argument.

        Parameters
        ----------
        canonical
            The query whose project to scope to

        facets
            The facets to list the values of, named canonically.

            Every one has to be a facet this vocabulary can express,
            because there is no way to ask about one that is not.

        Returns
        -------
        :
            The request to send to `search_api`

        Raises
        ------
        FacetNotExpressibleError
            This facade's vocabulary cannot express one of `facets`

        OneProjectRequiredError
            A STAC facade was given anything other than exactly one project

        ProjectPrefixMismatchError
            A STAC facade was given a project its vocabulary does not describe
        """
        check_facets_expressible(self.query_style, facets)
        wire_facets = set(self._wire_facet_names(facets).values())

        if self._stac_prefix is None:
            project = self._single_project(canonical)
        else:
            project = stac_collection(
                canonical, cast("type[STACParams]", self.query_style)
            )

        return self.search_api.build_get_facet_values_for_project_request(
            wire_facets, project
        )

    def parse_facet_values(
        self, raw: dict[str, Any], facets: set[str]
    ) -> dict[str, set[str]]:
        """
        Read the available facet values out of a raw response

        Parameters
        ----------
        raw
            The response to read, i.e. the answer to a request built with
            [build_get_facet_values_request][(c).build_get_facet_values_request]

        facets
            The facets we asked about, named canonically

        Returns
        -------
        :
            The values which are available, keyed by the canonical facet name

            A facet whose values the API does not enumerate is left out.

        Raises
        ------
        NoFacetValuesReturned
            The response does not enumerate facet values at all

        UnaskableFacetError
            This facade's vocabulary cannot express one of `facets`,
            so this response was never going to answer the question
        """
        return self._read_back(self.search_api.parse_facet_values, raw, facets)

    def parse_facet_patterns(
        self, raw: dict[str, Any], facets: set[str]
    ) -> dict[str, re.Pattern[str]]:
        """
        Read the supported facet patterns out of a raw response

        The counterpart to [parse_facet_values][(c).parse_facet_values],
        for the facets an API describes by their form rather than by listing them.

        Parameters
        ----------
        raw
            The response to read, i.e. the answer to a request built with
            [build_get_facet_values_request][(c).build_get_facet_values_request]

        facets
            The facets we asked about, named canonically

        Returns
        -------
        :
            The pattern each facet's values must take, keyed by the canonical name

        Raises
        ------
        UncompilableFacetPatternError
            A pattern given for a facet is not a valid regular expression

        UnaskableFacetError
            This facade's vocabulary cannot express one of `facets`
        """
        return self._read_back(self.search_api.parse_facet_patterns, raw, facets)

    def _read_back(
        self,
        parse: Callable[[dict[str, Any], set[str]], dict[str, Any]],
        raw: dict[str, Any],
        facets: set[str],
    ) -> dict[str, Any]:
        """
        Parse a facet-values response and translate its keys back to canonical names

        `parse` reads `raw` keyed by the wire names; this asks it about the wire
        names for `facets`, then hands the answer back under the names they were
        asked for.
        """
        check_facets_askable(self.query_style, facets)

        wire = self._wire_facet_names(facets)
        asked_for = {wire_name: canonical for canonical, wire_name in wire.items()}

        native_keyed = parse(raw, set(wire.values()))
        return {
            asked_for[wire_name]: value
            for wire_name, value in native_keyed.items()
            if wire_name in asked_for
        }


SearchAPIFacadeSelector = Callable[[QueryCanonical, int], SearchAPIFacade | None]
"""
Chooses which facade to try next

Given the canonical query and a 0-based attempt index,
returns the next
[SearchAPIFacade][esmporium.search.search_api_facade.SearchAPIFacade] to try,
or `None` to say that there is nothing to try for this query and attempt number.
"""


class SelectorOfferedNoAPIFacadeError(ValueError):
    """
    Raised when a selector offers no search API facade for a query from the very start

    Asking for a search is asking for it to happen.
    Handing back an empty answer would read as
    "we asked, and nobody had anything for you",
    when what happened is that nobody was asked at all:
    a selector with an empty list,
    or one whose rules rule out every endpoint for this query,
    is a bug in the calling code
    and is worth saying out loud rather than quietly returning nothing.

    This is only about having nobody to ask.
    Endpoints which were asked and did not answer are a different thing,
    and are reported as such.
    """

    def __init__(
        self, canonical: QueryCanonical, selector: SearchAPIFacadeSelector
    ) -> None:
        """
        Initialise the error

        Parameters
        ----------
        canonical
            The query we were going to ask about

        selector
            The selector which had nothing to offer for it
        """
        self.canonical = canonical
        self.selector = selector
        super().__init__(
            "No API facade was offered on the very first attempt, "
            "so there was nobody to ask. "
            f"The selector was: {selector}. The query was: {canonical!r}."
        )


def build_list_selector(facades: Sequence[SearchAPIFacade]) -> SearchAPIFacadeSelector:
    """
    Build a selector that yields search API facades in order

    Every query works through the same list.

    Parameters
    ----------
    facades
        The search API facades to yield, in order

    Returns
    -------
    :
        A selector over `facades`
    """

    def select(canonical: QueryCanonical, attempt: int) -> SearchAPIFacade | None:
        return facades[attempt] if attempt < len(facades) else None

    return select


def build_project_list_selector(
    project_lists: Mapping[str, Sequence[SearchAPIFacade]],
) -> SearchAPIFacadeSelector:
    """
    Build a selector that works through a project specific list of facades

    Parameters
    ----------
    project_lists
        The facades to yield for each project, in order

    Returns
    -------
    :
        A selector which yields facades in an order specific to the query's project
    """

    def select(canonical: QueryCanonical, attempt: int) -> SearchAPIFacade | None:
        """
        Select search API facade to use

        Parameters
        ----------
        canonical
            Query (in canonical form)

        attempt
            Search attempt

        Returns
        -------
        :
            [SearchAPIFacade][(m).] to use

            If we have run out of APIs to try, we return `None`

        Raises
        ------
        ValueError
            The query specifies a search that is not for exactly one project

        KeyError
            We do not have a list of facades to try for the input project
        """
        if len(canonical.project) != 1:
            msg = (
                "We can only unambiguously pick the SearchAPI list "
                "if there is exactly one project, "
                f"received: {canonical.project}"
            )
            raise ValueError(msg)

        project = canonical.project[0]

        apis = project_lists[project]

        return apis[attempt] if attempt < len(apis) else None

    return select


@dataclass(frozen=True)
class SearchAPIFacadeClassification:
    """
    Classification of a search API facade

    Provides extra classification information (i.e. metadata) which
    [SearchAPIFacade][(m).] doesn't hold.

    Note that these classifications are generally based on experience.
    If we were 100% sure about this metadata,
    we would adjust the underlying classes directly instead.
    """

    facade: SearchAPIFacade
    """
    Search API facade
    """

    projects: tuple[str, ...]
    """
    Projects which `facade` supports working with
    """


@dataclass(frozen=True)
class SearchAPIFacadeStore:
    """
    A store of search API facades

    This store helps manage a set of API facades
    and get them in more convenient ways than looking through lists.
    """

    classifications: tuple[SearchAPIFacadeClassification, ...]
    """
    Search API facade classifications
    """

    def get_api_facades_for_project(self, project: str) -> list[SearchAPIFacade]:
        """
        Get the API facades that can be used to search a specific project

        Parameters
        ----------
        project
            The project for which we want to get
            all the API facades that can be used to search the project.

        Returns
        -------
        :
            API facades that can be used to search `project`.
        """
        return [v.facade for v in self.classifications if project in v.projects]

    def get_api_facades_from_host(self, host: str) -> list[SearchAPIFacade]:
        """
        Get the API facades that use a specific host

        Parameters
        ----------
        host
            The host for which we want to get API facades.

        Returns
        -------
        :
            API facades that use `host`
        """
        return [
            v.facade for v in self.classifications if v.facade.search_api.host == host
        ]

    def get_api_facade_for_project_from_host(
        self, project: str, host: str
    ) -> SearchAPIFacade:
        """
        Get the API facade that can be used to search a project from a specific host

        Parameters
        ----------
        project
            The project for which we want to get the API facade.

        host
            The host for which we want to get API facade.

        Returns
        -------
        :
            API facade for `project` that uses `host`
        """
        matches = [
            v
            for v in self.classifications
            if v.facade.search_api.host == host and project in v.projects
        ]
        if len(matches) < 1:
            host_projects: dict[str, list[str]] = {}
            for v in self.classifications:
                host_projects.setdefault(v.facade.search_api.host, []).extend(
                    v.projects
                )

            supported_hosts_and_projects = "\n".join(
                f"  - {host}: {projects}" for host, projects in host_projects.items()
            )
            msg = (
                f"No API from {host=} is associated with {project=}. "
                "Available hosts and supported projects:\n"
                f"{supported_hosts_and_projects}"
            )
            raise ValueError(msg)

        elif len(matches) > 1:
            msg = f"More than one candidate for {host=} and {project=}. {matches=}"
            raise AssertionError(msg)

        return matches[0].facade

    @classmethod
    def initialise_with_default_api_facades(
        cls, retrying: Retrying | None = None
    ) -> SearchAPIFacadeStore:
        """
        Initialise with our default API facade set

        Parameters
        ----------
        retrying
            Retrying strategy to use with all the APIs.

            If `None` (the default), a fresh
            [build_transient_retrying][esmporium.search.retry.build_transient_retrying]
            is built for each API. This matters because a `Retrying` carries
            per-run state, so sharing one across APIs is not safe once calls can
            be made in parallel; pass your own only if you know you want it shared.

        Returns
        -------
        :
            Initialised object
        """
        classifications_l = []

        # There are probably clearer ways to do this.
        # One for another day.
        cmip5_facades = (
            (
                SolrCMIP5Parameters,
                SearchAPIESGF1Solr,
                "esg-dn1.nsc.liu.se",
            ),
            (
                SolrCMIP5Parameters,
                SearchAPIESGF1Solr,
                "esgf.nci.org.au",
            ),
            (
                SolrCMIP5Parameters,
                SearchAPIESGF15BridgeSolr,
                "esgf-node.ornl.gov",
            ),
            (
                SolrCMIP5Parameters,
                SearchAPIESGF1Solr,
                "esgf.ceda.ac.uk",
            ),
            (
                SolrCMIP5Parameters,
                SearchAPIESGF1Solr,
                "esgf-data.dkrz.de",
            ),
            (
                STACCMIP5Parameters,
                SearchAPIESGFNGSTAC,
                "search.east.esgf.io",
            ),
            (
                STACCMIP5Parameters,
                SearchAPIESGFNGSTAC,
                "search.west.esgf.io",
            ),
        )

        cmip6_facades = (
            (
                SolrCMIP6Parameters,
                SearchAPIESGF1Solr,
                "esg-dn1.nsc.liu.se",
            ),
            (
                SolrCMIP6Parameters,
                SearchAPIESGF1Solr,
                "esgf.nci.org.au",
            ),
            (
                SolrCMIP6Parameters,
                SearchAPIESGF15BridgeSolr,
                "esgf-node.ornl.gov",
            ),
            (
                SolrCMIP6Parameters,
                SearchAPIESGF1Solr,
                "esgf.ceda.ac.uk",
            ),
            (
                SolrCMIP6Parameters,
                SearchAPIESGF1Solr,
                "esgf-data.dkrz.de",
            ),
            (
                STACCMIP6Parameters,
                SearchAPIESGFNGSTAC,
                "search.east.esgf.io",
            ),
            (
                STACCMIP6Parameters,
                SearchAPIESGFNGSTAC,
                "search.west.esgf.io",
            ),
        )

        cmip7_facades = (
            (
                SolrCMIP7Parameters,
                SearchAPIESGF1Solr,
                "esgf.nci.org.au",
            ),
            (
                SolrCMIP7Parameters,
                SearchAPIESGF1Solr,
                "esgf-data.dkrz.de",
            ),
            (
                STACCMIP7Parameters,
                SearchAPIESGFNGSTAC,
                "search.east.esgf.io",
            ),
            (
                STACCMIP7Parameters,
                SearchAPIESGFNGSTAC,
                "search.west.esgf.io",
            ),
        )

        # To add CMIP6Plus support in future:
        # add a `cmip6plus_facades` block here (its own STAC vocabulary with a
        # `cmip6plus` prefix would be needed, as STACCMIP6Parameters is tied to
        # the `cmip6` collection), classify it against `("CMIP6Plus",)` in the
        # loop below, and add "CMIP6Plus" to DEFAULT_SEARCH_API_FACADES_BY_PROJECT.
        for projects, facade_definitions in (
            (("CMIP5",), cmip5_facades),
            (("CMIP6",), cmip6_facades),
            (("CMIP7",), cmip7_facades),
        ):
            for query_style, search_api_type, host in facade_definitions:
                # A fresh retry policy per API unless the caller shared one:
                # tenacity's Retrying carries per-run state.
                api_retrying = (
                    retrying if retrying is not None else build_transient_retrying(3)
                )
                search_api = cast(
                    "SearchAPI", search_api_type(host=host, retrying=api_retrying)
                )
                classifications_l.append(
                    SearchAPIFacadeClassification(
                        SearchAPIFacade(
                            query_style=query_style,
                            search_api=search_api,
                        ),
                        projects=projects,
                    )
                )

        res = cls(classifications=tuple(classifications_l))

        return res


INBUILT_SEARCH_API_FACADE_STORE = (
    SearchAPIFacadeStore.initialise_with_default_api_facades()
)
"""
Our in-built search API facade store.

This should not be taken to be exhaustive.
You may need to add more APIs or adjust retry policies etc. yourself.
"""

DEFAULT_SEARCH_API_FACADES_BY_PROJECT: Mapping[str, Sequence[SearchAPIFacade]] = {
    project: INBUILT_SEARCH_API_FACADE_STORE.get_api_facades_for_project(project)
    for project in ("CMIP5", "CMIP6", "CMIP7")
}
"""
Default search APIs to use, grouped by project
"""

DEFAULT_SELECTOR = build_project_list_selector(DEFAULT_SEARCH_API_FACADES_BY_PROJECT)
"""The selector used when the caller does not choose one"""
