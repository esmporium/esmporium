"""
Search API facade

This contains our facades to search APIs.
These facades are introduced to add more robust handling.
Complete documentation of this will be added in future.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, ClassVar

from pydantic import BaseModel, ConfigDict
from tenacity import Retrying

from esmporium.query import (
    FacetValues,
    FacetValuesByName,
    QueryCanonical,
    QueryFacet,
    QueryProtocol,
    SourceQuery,
    facet_values_from_attributes,
)
from esmporium.search.apis import (
    SearchAPI,
    SearchAPIESGF1Solr,
    SearchAPIESGF15BridgeSolr,
    SearchAPIESGFNGSTAC,
)
from esmporium.search.retry import build_transient_retrying


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
    """

    query_style: type[QueryProtocol]
    """
    The query style that this facade uses
    """

    search_api: SearchAPI
    """
    The search API for which we are providing a facade
    """


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


class StacCMIP5Parameters(BaseModel):
    """
    CMIP5 facet values under their ESGF-NG/STAC property stems
    """

    model_config = ConfigDict(extra="forbid")

    prefix: ClassVar[str] = "cmip5"
    """See [StacParams.prefix][esmporium.search.esgf_generations.StacParams.prefix]."""

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


class StacCMIP6Parameters(BaseModel):
    """CMIP6 facet values under their ESGF-NG/STAC property stems"""

    model_config = ConfigDict(extra="forbid")

    prefix: ClassVar[str] = "cmip6"
    """See [StacParams.prefix][esmporium.search.esgf_generations.StacParams.prefix]."""

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


class StacCMIP7Parameters(BaseModel):
    """
    CMIP7 facet values under their ESGF-NG/STAC property stems
    """

    model_config = ConfigDict(extra="forbid")

    prefix: ClassVar[str] = "cmip7"
    """See [StacParams.prefix][esmporium.search.esgf_generations.StacParams.prefix]."""

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


SearchAPIFacadeSelector = Callable[[QueryCanonical, int], SearchAPIFacade | None]
"""
Chooses which facade to try next

Given the canonical query and a 0-based attempt index,
returns the next
[SearchAPIFacade][esmporium.search.search_api.SearchAPIFacade] to try,
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
        A selector which yields faades in an order specific to the query's project
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
    A store of seach API facades

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
        matches_host = self.get_api_facades_from_host(host)
        matches = [v for v in matches_host if project in v.projects]
        if len(matches) < 1:
            host_projects = {}
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
    def intialise_with_default_api_facades(
        cls, retrying: Retrying = build_transient_retrying(3)
    ) -> SearchAPIFacadeStore:
        """
        Initialise with our default API facade set

        Parameters
        ----------
        retrying
            Retrying strategy to use with all the APIs

        Returns
        -------
        :
            Initalised object
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
                StacCMIP5Parameters,
                SearchAPIESGFNGSTAC,
                "search.east.esgf.io",
            ),
            (
                StacCMIP5Parameters,
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
                StacCMIP6Parameters,
                SearchAPIESGFNGSTAC,
                "search.east.esgf.io",
            ),
            (
                StacCMIP6Parameters,
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
                StacCMIP7Parameters,
                SearchAPIESGFNGSTAC,
                "search.east.esgf.io",
            ),
            (
                StacCMIP7Parameters,
                SearchAPIESGFNGSTAC,
                "search.west.esgf.io",
            ),
        )

        for projects, facade_definitions in (
            (("CMIP5"), cmip5_facades),
            (("CMIP6", "CMIP6Plus"), cmip6_facades),
            (("CMIP7"), cmip7_facades),
        ):
            for query_style, search_api_type, host in facade_definitions:
                classifications_l.append(
                    SearchAPIFacadeClassification(
                        SearchAPIFacade(
                            query_style=query_style,
                            search_api=search_api_type(host=host, retrying=retrying),
                        ),
                        projects=projects,
                    )
                )

        res = cls(classifications=tuple(classifications_l))

        return res


INBUILT_SEARCH_API_FACADE_STORE = (
    SearchAPIFacadeStore.intialise_with_default_api_facades()
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
