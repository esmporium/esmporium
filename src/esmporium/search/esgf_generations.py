"""
Generations of ESGF search APIs that we support

For example, ESGF1, ESGF1.5 bridge and ESGF-NG.

Each generation is a pure translator:
it turns a [QueryCanonical][esmporium.query.canonical_query.QueryCanonical]
into a [Request][esmporium.search.esgf_generations.Request],
and reads the parts we care about back out of a raw response.
Nothing here sends anything: no host, no HTTP client, no retries.
That keeps the bit which knows about wire formats
separate from the bit which knows about the state of the federation,
and it means a generation can be tested without a network connection.

A generation is handed the vocabulary it should speak
(one of the parameter classes below),
rather than choosing one itself.
Nothing in a generation is keyed by project,
so a generation is not coupled to any particular project.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict

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

MIN_LIMIT: int = 1
"""
The smallest page we ask any generation for

The Solr-shaped APIs do accept a page of zero, i.e. "just give me the count",
while STAC rejects it with a 422.
We use one floor for both rather than tracking each API's own,
because the only thing the difference buys us
is not transferring a single record on the Solr path,
and the price is a rule which is different depending on where you are looking.
"""

MAX_LIMIT: int = 10_000
"""
The largest page we can ask any generation for

Above this, ESGF1 returns a hard 400
while ESGF-NG silently truncates,
so we check rather than trusting either.
"""

DEFAULT_LIMIT: int = 10_000
"""
The page size we use if the caller does not choose one

I.e. take a full page.
"""


class LimitOutOfRangeError(ValueError):
    """
    Raised when a page size is one the search APIs will not accept
    """

    def __init__(self, limit: int) -> None:
        """
        Initialise the error

        Parameters
        ----------
        limit
            The page size which was asked for
        """
        self.limit = limit
        super().__init__(
            f"limit must be between {MIN_LIMIT} and {MAX_LIMIT}, received {limit}. "
            "If you want more records than that, paginate."
        )


def check_limit(limit: int) -> None:
    """
    Check that a page size is one the search APIs will accept

    We check rather than quietly clamping,
    because a caller who asks for 50,000 records and receives 10,000
    has no way to tell that they only got part of the answer.

    Parameters
    ----------
    limit
        The page size to check

    Raises
    ------
    LimitOutOfRangeError
        `limit` is outside the range every generation accepts
    """
    if limit < MIN_LIMIT or limit > MAX_LIMIT:
        raise LimitOutOfRangeError(limit)


class StacParams(QueryProtocol, Protocol):
    """
    A STAC parameter class

    The prefix lives with the parameter class because it co-varies exactly with
    project that each parameter class is paired to
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
    using parameters from this class and STAC APIs which use this parameter class
    With STAC, there is a tight coupling between prefixes i.e. projects and searches.
    As a result, each STAC search request can only search a single project,
    which isn't the case with ESGF1
    (having said this, for better error messaging related to facet names,
    our ESGF1 search APIs are also tightly coupled to specific projects).
    """


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


@dataclass(frozen=True)
class Request:
    """
    A ready-to-send HTTP request, minus the host

    A generation produces one of these.
    Which host it is sent to, and what to do if that host does not answer,
    is somebody else's problem.
    """

    method: str
    """
    The HTTP method to use

    Carried here so that whatever sends the request
    never has to branch on the generation.
    """

    path: str
    """The path to request, i.e. everything after the host"""

    params: dict[str, Any] | None = None
    """The query parameters to send, if any"""

    json_body: dict[str, Any] | None = None
    """The JSON body to send, if any"""


class UnaskableFacetError(AssertionError):
    """
    Raised when we read a facet we could never have asked the API about

    Unlike the other errors here, this one is ours rather than the caller's.
    It means a facets request was built and sent naming a facet this vocabulary
    has no name for, which
    [check_facets_expressible][esmporium.search.esgf_generations.check_facets_expressible]
    exists to prevent. Reaching here means something got past it.
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
            named in the canonical vocabulary
        """
        self.params = params
        self.facets = facets
        named = ", ".join(sorted(facets))
        super().__init__(
            f"{params.__name__} has no name for {named}, "
            "so we cannot have asked the API about it and it cannot be in this "
            "response. This request should never have been built."
        )


class FacetListingNotSupported(NotImplementedError):
    """
    Raised when a response does not enumerate facet values at all

    This is deliberately loud.
    The alternative, returning nothing, cannot be told apart from
    "this API says none of your values exist",
    which would make every value the user asked for look like a typo.
    """

    def __init__(
        self,
        msg: str = "this response does not list facet values; check against the CV",
    ) -> None:
        """
        Initialise the error

        Parameters
        ----------
        msg
            The message to show the user
        """
        super().__init__(msg)


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

    def __init__(self, project: str, params: type[StacParams]) -> None:
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


def stac_collection(canonical: QueryCanonical, params: type[StacParams]) -> str:
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


class SearchAPIGeneration(Protocol):
    """
    The wire format spoken by a family of ESGF search endpoints
    """

    name: str
    """What to call this generation in a message to the user"""

    def build_request(self, canonical: QueryCanonical, limit: int) -> Request:
        """
        Turn a canonical query into a request in this generation's format

        Parameters
        ----------
        canonical
            Query to render

        limit
            The PAGE size to ask for,
            i.e. the maximum number of records in one response.

            This is not the total number of matches;
            that comes back in the response itself
            and is what [result_count][(c).result_count] reads.

        Returns
        -------
        :
            The request to send

        Raises
        ------
        FacetNotExpressibleError
            `canonical` sets a facet which this generation's vocabulary
            cannot express
        """
        ...

    def result_count(self, raw: dict[str, Any]) -> int | None:
        """
        Read the total number of matches out of a raw response

        Parameters
        ----------
        raw
            The response to read

        Returns
        -------
        :
            The number of records which matched,
            or `None` if the response does not say.
        """
        ...

    def build_facets_request(
        self, canonical: QueryCanonical, facets: set[str]
    ) -> Request:
        """
        Build a request which lists the values of the given facets

        The request is scoped to `canonical`'s project and nothing else.
        Scoping to the user's other values too
        would mean that one facet's typo could make another look invalid,
        which is the opposite of helpful when the point of asking
        is to tell the user which of their values we do not recognise.

        Parameters
        ----------
        canonical
            The query whose project to scope to

        facets
            The facets to list the values of, named in the canonical vocabulary.

            Every one of them has to be a facet this generation's vocabulary
            can express, because there is no way to ask about one that is not.

        Returns
        -------
        :
            The request to send

        Raises
        ------
        FacetNotExpressibleError
            This generation's vocabulary cannot express one of `facets`
        """
        ...

    def parse_facet_values(
        self, raw: dict[str, Any], facets: set[str]
    ) -> dict[str, set[str]]:
        """
        Read the available facet values out of a raw response

        Parameters
        ----------
        raw
            The response to read,
            i.e. the answer to a [build_facets_request][(c).build_facets_request]

        facets
            The facets we asked about, named in the canonical vocabulary.

            Anything else in `raw` is ignored.

        Returns
        -------
        :
            The values which are available, keyed by canonical facet name

            A facet whose values the API does not enumerate is left out,
            rather than reported as having no values,
            because those are very different answers.

        Raises
        ------
        FacetListingNotSupported
            The response does not enumerate facet values at all

        UnaskableFacetError
            This generation's vocabulary cannot express one of `facets`,
            so this response was never going to answer the question
        """
        ...


def unexpressible_facets(params: type[QueryProtocol], facets: set[str]) -> set[str]:
    """
    Work out which of the given facets a vocabulary has no name for

    Parameters
    ----------
    params
        The vocabulary to check against

    facets
        The facets to check, named in the canonical vocabulary

    Returns
    -------
    :
        The facets which `params` cannot express
    """
    spec = facet_spec(params)

    return {facet for facet in facets if facet not in spec.canonical_to_native}


def check_facets_expressible(params: type[QueryProtocol], facets: set[str]) -> None:
    """
    Check that a vocabulary can express every facet being asked about

    A facet this vocabulary has no name for is a mistake, not something to
    quietly leave out. Dropping it would answer a question the caller did not
    ask: they would be told which models CMIP5 has, having also asked which
    activities it has, with nothing to say that the second question went
    nowhere. This is the same rule, and the same error, as translating a query
    which names a facet the target cannot express.

    Parameters
    ----------
    params
        The vocabulary to check against

    facets
        The facets to check, named in the canonical vocabulary

    Raises
    ------
    FacetNotExpressibleError
        `params` cannot express one of `facets`
    """
    unexpressible = unexpressible_facets(params, facets)
    if unexpressible:
        # Sorted so that a failure is reported deterministically.
        raise FacetNotExpressibleError(
            sorted(unexpressible)[0], facet_spec(params).name
        )


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
        The facets being read, named in the canonical vocabulary

    Raises
    ------
    UnaskableFacetError
        `params` cannot express one of `facets`
    """
    unexpressible = unexpressible_facets(params, facets)
    if unexpressible:
        raise UnaskableFacetError(params, unexpressible)


def solr_num_found(raw: dict[str, Any]) -> int | None:
    """
    Read the number of matches out of a Solr-shaped response

    Both ESGF1's `esg-search` and the ESGF 1.5 bridge answer in this shape.

    Parameters
    ----------
    raw
        The response to read

    Returns
    -------
    :
        The number of records which matched,
        or `None` if the response does not say.

        We also return `None` if the response says something
        we cannot read as a count,
        because guessing what the API meant would be worse
        than telling the caller we do not know.
    """
    num_found = raw.get("response", {}).get("numFound")
    if isinstance(num_found, int):
        return num_found

    return None


def solr_facet_values(
    raw: dict[str, Any], params: type[QueryProtocol], facets: set[str]
) -> dict[str, set[str]]:
    """
    Read the available facet values out of a Solr-shaped response

    Solr answers with a `facet_counts.facet_fields` block,
    keyed by the API's own facet names,
    each holding a flat `[value, count, value, count, ...]` list.
    We take the values, translate the API's names back to canonical ones,
    and keep only the facets which were asked about.

    Parameters
    ----------
    raw
        The response to read

    params
        The vocabulary the response is written in,
        i.e. the parameter class used to build the request

    facets
        The facets we asked about, named in the canonical vocabulary

    Returns
    -------
    :
        The values which are available, keyed by canonical facet name

    Raises
    ------
    FacetListingNotSupported
        `raw` enumerates nothing at all,
        even though we asked about a facet this vocabulary can express

    UnaskableFacetError
        `params` cannot express one of `facets`
    """
    spec = facet_spec(params)

    # Raises if this response was never going to be able to answer the question.
    check_facets_askable(params, facets)

    fields = raw.get("facet_counts", {}).get("facet_fields", {})
    if not fields:
        raise FacetListingNotSupported

    res: dict[str, set[str]] = {}
    for api_name, flat in fields.items():
        canonical = spec.native_to_canonical.get(api_name)
        if canonical in facets:
            res[canonical] = set(flat[0::2])

    return res


def stac_summary_values(
    raw: dict[str, Any], params: type[StacParams], facets: set[str]
) -> dict[str, set[str]]:
    """
    Read the available facet values out of a STAC collection

    A collection carries a `summaries` block,
    keyed by the same prefixed property names a search request uses,
    which is what lets us map it back to the canonical vocabulary.

    Not every summary enumerates values.
    STAC also allows a summary to be a range, and this API uses
    regular expressions for the facets whose values are generated rather than
    chosen (`variant_label`, for example).
    Those are left out of the result:
    "we cannot list this one" and "this one has no values"
    have to stay distinguishable.

    Parameters
    ----------
    raw
        The collection to read

    params
        The vocabulary the summaries are written in,
        i.e. the parameter class used to build the request

    facets
        The facets we asked about, named in the canonical vocabulary

    Returns
    -------
    :
        The values which are available, keyed by canonical facet name

    Raises
    ------
    FacetListingNotSupported
        `raw` summarises nothing at all,
        so this deployment cannot tell us anything about any facet

    UnaskableFacetError
        `params` cannot express one of `facets`
    """
    # See the equivalent note in `solr_facet_values`.
    check_facets_askable(params, facets)

    # An empty block is as useless to us as a missing one:
    # either way this deployment has told us nothing it knows.
    if not raw.get("summaries"):
        raise FacetListingNotSupported

    spec = facet_spec(params)
    prefix = f"{params.prefix}:"

    res: dict[str, set[str]] = {}
    for property_name, summary in raw["summaries"].items():
        if not property_name.startswith(prefix):
            continue

        canonical = spec.native_to_canonical.get(property_name[len(prefix) :])
        if canonical not in facets:
            continue

        if not isinstance(summary, list):
            # A range or a pattern, i.e. not a list of values.
            continue

        values = {value for value in summary if isinstance(value, str)}
        if values:
            res[canonical] = values

    return res


def solr_facets_to_list(params: type[QueryProtocol], facets: set[str]) -> list[str]:
    """
    Translate canonical facet names into the API's names, for a `facets=` list

    Facets which `params` cannot express are dropped:
    there is nothing we could ask the API about.
    The result is sorted so that the request we build is deterministic.

    Parameters
    ----------
    params
        The vocabulary to write the facet names in

    facets
        The facets to list the values of, named in the canonical vocabulary

    Returns
    -------
    :
        The facets, named as this API names them

    Raises
    ------
    FacetNotExpressibleError
        `params` cannot express one of `facets`
    """
    check_facets_expressible(params, facets)
    spec = facet_spec(params)

    return sorted(spec.canonical_to_native[facet] for facet in facets)


@dataclass(frozen=True)
class ESGF1Solr:
    """
    The ESGF1 (`esg-search`) generation

    A flat GET, where a facet with more than one value
    is sent as a repeated parameter.
    """

    params: type[QueryProtocol]
    """The vocabulary this generation speaks"""

    name: str = "ESGF1"
    """See [SearchAPIGeneration.name][esmporium.search.esgf_generations.SearchAPIGeneration.name]."""  # noqa: E501

    def build_request(self, canonical: QueryCanonical, limit: int) -> Request:
        """See [SearchAPIGeneration.build_request][esmporium.search.esgf_generations.SearchAPIGeneration.build_request]."""  # noqa: E501
        check_limit(limit)
        native = from_canonical(canonical=canonical, to=self.params)

        params: dict[str, Any] = {
            "format": "application/solr+json",
            "limit": limit,
            # Ask this node to sweep the federation it mirrors,
            # rather than only answering for itself.
            "distrib": "true",
        }
        for api_name, values in native.facet_values().items():
            # A list becomes a repeated parameter, which is how this API ORs.
            params[api_name] = list(values)

        return Request("GET", "/esg-search/search", params=params)

    def result_count(self, raw: dict[str, Any]) -> int | None:
        """See [SearchAPIGeneration.result_count][esmporium.search.esgf_generations.SearchAPIGeneration.result_count]."""  # noqa: E501
        return solr_num_found(raw)

    def build_facets_request(
        self, canonical: QueryCanonical, facets: set[str]
    ) -> Request:
        """See [SearchAPIGeneration.build_facets_request][esmporium.search.esgf_generations.SearchAPIGeneration.build_facets_request]."""  # noqa: E501
        spec = facet_spec(self.params)

        params: dict[str, Any] = {
            "format": "application/solr+json",
            # `facets=` is a comma-separated list on both Solr-shaped APIs,
            # even though their value encoding differs.
            "facets": ",".join(solr_facets_to_list(self.params, facets)),
            # We want the vocabulary, not the records.
            "limit": MIN_LIMIT,
            "distrib": "true",
        }

        project_api_name = spec.canonical_to_native.get("project")
        if project_api_name and canonical.project:
            params[project_api_name] = list(canonical.project)

        return Request("GET", "/esg-search/search", params=params)

    def parse_facet_values(
        self, raw: dict[str, Any], facets: set[str]
    ) -> dict[str, set[str]]:
        """See [SearchAPIGeneration.parse_facet_values][esmporium.search.esgf_generations.SearchAPIGeneration.parse_facet_values]."""  # noqa: E501
        return solr_facet_values(raw, self.params, facets)


@dataclass(frozen=True)
class ESGF15Bridge:
    """
    The ESGF 1.5 bridge generation

    The replies are Solr-shaped, but the requests are not:

    - a facet with more than one value is comma-joined,
      because a repeated parameter is silently reduced to a single value
    - there is no `distrib`,
      because this is one consolidated ESGF 1.5 index rather than a federation
    - it answers at its own path

    The facet names are the same as ESGF1's,
    so this generation re-uses the same parameter classes.
    Only the encoding differs, which is exactly what a generation is for.
    """

    params: type[QueryProtocol]
    """The vocabulary this generation speaks"""

    name: str = "ESGF15"
    """See [SearchAPIGeneration.name][esmporium.search.esgf_generations.SearchAPIGeneration.name]."""  # noqa: E501

    def build_request(self, canonical: QueryCanonical, limit: int) -> Request:
        """See [SearchAPIGeneration.build_request][esmporium.search.esgf_generations.SearchAPIGeneration.build_request]."""  # noqa: E501
        check_limit(limit)
        native = from_canonical(canonical=canonical, to=self.params)

        params: dict[str, Any] = {
            "format": "application/solr+json",
            "limit": limit,
        }
        for api_name, values in native.facet_values().items():
            # This API ORs on a comma, not on a repeated parameter.
            params[api_name] = ",".join(values)

        return Request("GET", "/esgf-1-5-bridge/", params=params)

    def result_count(self, raw: dict[str, Any]) -> int | None:
        """See [SearchAPIGeneration.result_count][esmporium.search.esgf_generations.SearchAPIGeneration.result_count]."""  # noqa: E501
        return solr_num_found(raw)

    def build_facets_request(
        self, canonical: QueryCanonical, facets: set[str]
    ) -> Request:
        """See [SearchAPIGeneration.build_facets_request][esmporium.search.esgf_generations.SearchAPIGeneration.build_facets_request]."""  # noqa: E501
        spec = facet_spec(self.params)

        params: dict[str, Any] = {
            "format": "application/solr+json",
            "facets": ",".join(solr_facets_to_list(self.params, facets)),
            "limit": MIN_LIMIT,
        }

        project_api_name = spec.canonical_to_native.get("project")
        if project_api_name and canonical.project:
            params[project_api_name] = ",".join(canonical.project)

        return Request("GET", "/esgf-1-5-bridge/", params=params)

    def parse_facet_values(
        self, raw: dict[str, Any], facets: set[str]
    ) -> dict[str, set[str]]:
        """See [SearchAPIGeneration.parse_facet_values][esmporium.search.esgf_generations.SearchAPIGeneration.parse_facet_values]."""  # noqa: E501
        return solr_facet_values(raw, self.params, facets)


@dataclass(frozen=True)
class ESGFNGStac:
    """
    The ESGF-NG generation, i.e. STAC 1.0 with CQL2

    A JSON POST, where the facets become a CQL2 tree
    of `in` clauses joined by `and`.
    """

    params: type[StacParams]
    """
    The vocabulary this generation speaks

    The `cmipN:` prefix rides on the parameter class,
    so a vocabulary can never be paired with the wrong prefix.
    """

    name: str = "ESGF_NG"
    """See [SearchAPIGeneration.name][esmporium.search.esgf_generations.SearchAPIGeneration.name]."""  # noqa: E501

    def build_request(self, canonical: QueryCanonical, limit: int) -> Request:
        """
        See [SearchAPIGeneration.build_request][esmporium.search.esgf_generations.SearchAPIGeneration.build_request].

        With this API, the project is the collection ID rather than a property,
        so it is translated out of the query and into a `collection` clause.
        We take it as the user wrote it (e.g. "CMIP5"),
        i.e. we do not second-guess their capitalisation.
        """  # noqa: E501
        check_limit(limit)
        collection = stac_collection(canonical, self.params)
        without_project = canonical.model_copy(update={"project": ()})
        native = from_canonical(canonical=without_project, to=self.params)

        and_clauses: list[dict[str, Any]] = [
            {"op": "=", "args": [{"property": "collection"}, collection]},
        ]
        for stem, values in native.facet_values().items():
            and_clauses.append(
                {
                    "op": "in",
                    "args": [
                        {"property": f"{self.params.prefix}:{stem}"},
                        list(values),
                    ],
                }
            )

        json_body = {
            "filter-lang": "cql2-json",
            "limit": limit,
            "filter": {"op": "and", "args": and_clauses},
        }

        return Request("POST", "/search", json_body=json_body)

    def result_count(self, raw: dict[str, Any]) -> int | None:
        """
        See [SearchAPIGeneration.result_count][esmporium.search.esgf_generations.SearchAPIGeneration.result_count].

        Not every deployment reports `numberMatched`,
        so we fall back to counting what came back.
        Note that that fall back is only a lower bound:
        it counts one page, not the total.
        """  # noqa: E501
        matched = raw.get("numberMatched")
        if isinstance(matched, int):
            return matched

        return len(raw.get("features", []))

    def build_facets_request(
        self, canonical: QueryCanonical, facets: set[str]
    ) -> Request:
        """
        See [SearchAPIGeneration.build_facets_request][esmporium.search.esgf_generations.SearchAPIGeneration.build_facets_request].

        This API describes a collection's facet values in the collection itself,
        so scoping to the project is the whole request
        and `facets` does not narrow it:
        one response carries every facet the collection summarises.

        Because the project decides which collection we ask about,
        it has to name exactly one, and it has to be a project this
        vocabulary describes.

        The collection also carries the values which are actually published,
        which is the same thing the Solr-shaped APIs report,
        rather than everything the controlled vocabularies allow.
        The two are not the same
        and the published set is the more useful of the two here:
        a value which is in the vocabulary but which nobody has published
        still finds nothing.
        """  # noqa: E501
        # `facets` does not shape the request, but asking about facets this
        # vocabulary cannot express is a mistake all the same,
        # and one worth hearing about here rather than on the way back.
        check_facets_expressible(self.params, facets)

        return Request("GET", f"/collections/{stac_collection(canonical, self.params)}")

    def parse_facet_values(
        self, raw: dict[str, Any], facets: set[str]
    ) -> dict[str, set[str]]:
        """See [SearchAPIGeneration.parse_facet_values][esmporium.search.esgf_generations.SearchAPIGeneration.parse_facet_values]."""  # noqa: E501
        return stac_summary_values(raw, self.params, facets)
