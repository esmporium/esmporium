"""
The queries we support

The only thing each query has to declare is its own fields.
This handles definition of all key information:

- the normalisation of each facet's values should be handled by the type
  [FacetValues][esmporium.query.canonical.FacetValues]
  or an equivalent
- how a facet translates is defined by its
  [QueryFacet][esmporium.query.canonical.QueryFacet] annotation
- moving a query to and from the canonical form is handled by
  [esmporium.query.translate][]
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from typing import Annotated, ClassVar, get_origin, get_type_hints

from pydantic import BaseModel, ConfigDict

from esmporium.query.canonical_query import (
    CANONICAL_FACETS,
    FacetValues,
    FacetValuesByName,
    QueryFacet,
)
from esmporium.query.protocol import QueryProtocol, SourceQuery

NON_FACET_FIELDS: frozenset[str] = frozenset({"other_terms", "source_query"})
"""
The fields of a query which are allowed to carry no
[QueryFacet][esmporium.query.canonical.QueryFacet]

Every other field must be annotated.
"""


class UnknownProjectError(ValueError):
    """Raised when we do not know what query to use for a given project."""

    def __init__(self, project: str, supported_projects: Iterable[str]) -> None:
        """
        Initialise the error

        Parameters
        ----------
        project
            The project which was requested

        supported_projects
            The projects for which we know the query to use
        """
        self.project = project
        supported = ", ".join(sorted(supported_projects))
        super().__init__(
            f"We don't support {project!r}; supported projects: {supported}"
        )


class UnannotatedFacetError(TypeError):
    """
    Raised when a query class has a field we cannot tell is a facet or not.
    """

    def __init__(self, query_class: type, fields: Collection[str]) -> None:
        """
        Initialise the error

        Parameters
        ----------
        query_class
            The query class under consideration

        fields
            The fields of `query_class` which carry no
            [QueryFacet][esmporium.query.canonical.QueryFacet]
        """
        self.query_class = query_class
        self.fields = tuple(sorted(fields))
        named = ", ".join(f"{query_class.__name__}.{field}" for field in self.fields)
        allowed = ", ".join(sorted(NON_FACET_FIELDS))
        super().__init__(
            "Annotate with `QueryFacet` to say how these translate, "
            f"or move them off the query: {named}. "
            f"Only {allowed} may go unannotated."
        )


class NoFacetsDeclaredError(TypeError):
    """Raised when a query class declares no facets at all."""

    def __init__(self, query_class: type) -> None:
        """
        Initialise the error

        Parameters
        ----------
        query_class
            The query class which declares no facets
        """
        self.query_class = query_class
        super().__init__(
            f"{query_class.__name__} declares no query facets, "
            "so it could only ever describe an empty search. "
            "Annotate its facets with `QueryFacet`."
        )


class MultipleFacetAnnotationsError(TypeError):
    """Raised when a field carries more than one `QueryFacet` annotation."""

    def __init__(
        self, query_class: type, field: str, declared: Collection[QueryFacet]
    ) -> None:
        """
        Initialise the error

        Parameters
        ----------
        query_class
            The query class under consideration

        field
            The field of `query_class` which carries more than one annotation

        declared
            The [QueryFacet][esmporium.query.canonical.QueryFacet]
            annotations `field` carries, in the order they are declared
        """
        self.query_class = query_class
        self.field = field
        self.declared = tuple(declared)
        claimed = ", ".join(repr(facet.canonical_equivalent) for facet in self.declared)
        super().__init__(
            f"{query_class.__name__}.{field} carries {len(self.declared)} "
            f"`QueryFacet` annotations, claiming: {claimed}. "
            "Annotate each facet with exactly one, "
            "so how it translates is unambiguous."
        )


class DuplicateCanonicalFacetError(TypeError):
    """Raised when two facets have the same equivalent canonical facet."""

    def __init__(
        self, query_class: type, canonical: str, fields: Collection[str]
    ) -> None:
        """
        Initialise the error

        Parameters
        ----------
        query_class
            The query class under consideration

        canonical
            The canonical facet which is claimed more than once

        fields
            The fields of `query_class` which claim `canonical`
        """
        self.query_class = query_class
        self.canonical = canonical
        self.fields = tuple(sorted(fields))
        named = ", ".join(self.fields)
        super().__init__(
            f"{query_class.__name__} maps more than one facet ({named}) "
            f"onto the canonical facet {canonical!r}."
        )


@dataclass(frozen=True)
class FacetSpec:
    """
    What a query class names, and what each of its facets translates to

    Read off the class's annotations by
    [facet_spec][esmporium.query.known_queries.facet_spec] rather than declared,
    so it cannot fall out of step with the class it describes.
    """

    name: str
    """
    What to call this query in a message to the user
    """

    facet_names: tuple[str, ...]
    """Every facet the query class names, in the order the class declares them"""

    canonical_to_native: Mapping[str, str]
    """
    Mapping from canonical facet names -> this query class's names
    """

    native_to_canonical: Mapping[str, str]
    """
    Mapping from this query class's facet names -> canonical facet names

    The inverse of `canonical_to_native`.
    """

    language_specific_facets: frozenset[str]
    """
    Facets this query class names which have no canonical equivalent
    """

    @property
    def absent_canonical_facets(self) -> frozenset[str]:
        """
        Canonical facets this class cannot express
        """
        return CANONICAL_FACETS - set(self.canonical_to_native)


def _declared_facets(query_class: type) -> dict[str, QueryFacet]:
    """
    Read the [QueryFacet][esmporium.query.canonical.QueryFacet] annotations off a class

    Parameters
    ----------
    query_class
        Query class whose annotations to read

    Returns
    -------
    :
        Each annotated facet, in declaration order

    Raises
    ------
    UnannotatedFacetError
        `query_class` has a field which is neither annotated nor in
        [NON_FACET_FIELDS][esmporium.query.languages.NON_FACET_FIELDS]

    MultipleFacetAnnotationsError
        A field of `query_class` carries more than one
        [QueryFacet][esmporium.query.canonical.QueryFacet]
    """
    # `include_extras` is what keeps the `Annotated` metadata;
    # without it the annotations come back as bare types
    # and every facet looks unannotated.
    # `get_type_hints` rather than pydantic's `model_fields`, because a query does
    # not have to be a pydantic model: anything carrying annotations works.
    hints = get_type_hints(query_class, include_extras=True)

    facets: dict[str, QueryFacet] = {}
    unannotated: list[str] = []
    for name, hint in hints.items():
        # A ClassVar is shared by every instance, so it cannot hold one query's
        # facet values, whatever it is annotated with.
        if get_origin(hint) is ClassVar or name in NON_FACET_FIELDS:
            continue

        declared = [
            metadata
            for metadata in getattr(hint, "__metadata__", ())
            if isinstance(metadata, QueryFacet)
        ]
        if not declared:
            unannotated.append(name)
            continue

        if len(declared) > 1:
            raise MultipleFacetAnnotationsError(query_class, name, declared)

        facets[name] = declared[0]

    if unannotated:
        raise UnannotatedFacetError(query_class, unannotated)

    return facets


@cache
def facet_spec(query_class: type) -> FacetSpec:
    """
    Work out what a query class names, and how each of its facets translates

    This doubles as a check of the annotations of a query class,
    hence helps ensure that error messages for user-defined query classes are sensible.

    Parameters
    ----------
    query_class
        Query class to inspect

    Returns
    -------
    :
        What `query_class` names, and what each of its facets translates to

    Raises
    ------
    UnannotatedFacetError
        `query_class` has a field which is neither annotated with
        [QueryFacet][esmporium.query.canonical.QueryFacet] nor in
        [NON_FACET_FIELDS][esmporium.query.languages.NON_FACET_FIELDS]

    MultipleFacetAnnotationsError
        A field of `query_class` carries more than one
        [QueryFacet][esmporium.query.canonical.QueryFacet]

    NoFacetsDeclaredError
        `query_class` declares no facets at all

    DuplicateCanonicalFacetError
        Two of `query_class`'s facets claim the same canonical facet
    """
    declared = _declared_facets(query_class)
    if not declared:
        raise NoFacetsDeclaredError(query_class)

    canonical_to_native: dict[str, str] = {}
    native_to_canonical: dict[str, str] = {}
    language_specific: list[str] = []
    for native, facet in declared.items():
        canonical = facet.canonical_equivalent
        if canonical is None:
            language_specific.append(native)
            continue

        if canonical in canonical_to_native:
            raise DuplicateCanonicalFacetError(
                query_class, canonical, (canonical_to_native[canonical], native)
            )

        canonical_to_native[canonical] = native
        native_to_canonical[native] = canonical

    return FacetSpec(
        name=query_class.__name__,
        facet_names=tuple(declared),
        canonical_to_native=canonical_to_native,
        native_to_canonical=native_to_canonical,
        language_specific_facets=frozenset(language_specific),
    )


DEFAULT_QUERY_MODEL_CONFIG = ConfigDict(extra="forbid")
"""
Default config used when declaring query models

A facet a query does not have is a typo,
not a facet to quietly drop.
`other_terms` is the escape hatch for facets we have not modelled.
"""


def facet_values_from_attributes(query: QueryProtocol) -> dict[str, tuple[str, ...]]:
    """
    Get the set (i.e. not empty) facets of a query which holds facets as attributes

    Which facets to look for comes from the class's own annotations, so this works
    for any query which holds its facets as attributes — a pydantic model, an
    attrs class, or something of your own.

    Parameters
    ----------
    query
        Query whose facets to read

    Returns
    -------
    :
        The facets which are set, keyed by their name in `query`'s language
    """
    spec = facet_spec(type(query))

    return {
        name: values for name in spec.facet_names if (values := getattr(query, name))
    }


class Query(BaseModel):
    """
    Query in our vocabulary (i.e. in line with [Dataset][esmporium.db.schema.Dataset])

    Every facet is its own canonical equivalent,
    which is what makes this the language every other one translates through.
    """

    model_config = DEFAULT_QUERY_MODEL_CONFIG

    project: Annotated[FacetValues, QueryFacet("project")] = ()
    """
    See [Dataset.project][esmporium.db.schema.Dataset.project].
    """

    model: Annotated[FacetValues, QueryFacet("model")] = ()
    """See [Dataset.model][esmporium.db.schema.Dataset.model]."""

    institution: Annotated[FacetValues, QueryFacet("institution")] = ()
    """See [Dataset.institution][esmporium.db.schema.Dataset.institution]."""

    experiment: Annotated[FacetValues, QueryFacet("experiment")] = ()
    """See [Dataset.experiment][esmporium.db.schema.Dataset.experiment]."""

    variable: Annotated[FacetValues, QueryFacet("variable")] = ()
    """See [Dataset.variable][esmporium.db.schema.Dataset.variable]."""

    variant_label: Annotated[FacetValues, QueryFacet("variant_label")] = ()
    """See [Dataset.variant_label][esmporium.db.schema.Dataset.variant_label]."""

    reporting_interval: Annotated[FacetValues, QueryFacet("reporting_interval")] = ()
    """See [Dataset.reporting_interval][esmporium.db.schema.Dataset.reporting_interval]."""  # noqa: E501

    processing_id: Annotated[FacetValues, QueryFacet("processing_id")] = ()
    """See [Dataset.processing_id][esmporium.db.schema.Dataset.processing_id]."""

    activity: Annotated[FacetValues, QueryFacet("activity")] = ()
    """See [Dataset.activity][esmporium.query.canonical.QueryCanonical.activity]."""

    resolution: Annotated[FacetValues, QueryFacet("resolution")] = ()
    """See [Dataset.resolution][esmporium.query.canonical.QueryCanonical.resolution]."""

    grid_label: Annotated[FacetValues, QueryFacet("grid_label")] = ()
    """See [Dataset.grid_label][esmporium.db.schema.Dataset.grid_label]."""

    realm: Annotated[FacetValues, QueryFacet("realm")] = ()
    """See [Dataset.realm][esmporium.query.canonical.QueryCanonical.realm]."""

    other_terms: FacetValuesByName = {}
    """
    Facets we have not modelled, passed through untranslated.

    The escape hatch for anything this language's fields do not name.
    Never checked, that is up to you to manage.
    """

    source_query: SourceQuery = None
    """
    Source from which this query was created

    Useful for debugging the results of translations
    """

    def facet_values(self) -> dict[str, tuple[str, ...]]:
        """See [QueryProtocol.facet_values][esmporium.query.languages.QueryProtocol.facet_values]."""  # noqa: E501
        return facet_values_from_attributes(self)


class QueryCMIP5(BaseModel):
    """
    A query in CMIP5's native vocabulary
    """

    model_config = DEFAULT_QUERY_MODEL_CONFIG

    project: Annotated[FacetValues, QueryFacet("project")] = ("CMIP5",)
    """
    See [Dataset.project][esmporium.db.schema.Dataset.project].
    """

    model: Annotated[FacetValues, QueryFacet("model")] = ()
    """See [Dataset.model][esmporium.db.schema.Dataset.model]."""

    institute: Annotated[FacetValues, QueryFacet("institution")] = ()
    """See [Dataset.institution][esmporium.db.schema.Dataset.institution]."""

    experiment: Annotated[FacetValues, QueryFacet("experiment")] = ()
    """See [Dataset.experiment][esmporium.db.schema.Dataset.experiment]."""

    ensemble: Annotated[FacetValues, QueryFacet("variant_label")] = ()
    """See [Dataset.variant_label][esmporium.db.schema.Dataset.variant_label]."""

    variable: Annotated[FacetValues, QueryFacet("variable")] = ()
    """See [Dataset.variable][esmporium.db.schema.Dataset.variable]."""

    time_frequency: Annotated[FacetValues, QueryFacet("reporting_interval")] = ()
    """See [Dataset.reporting_interval][esmporium.db.schema.Dataset.reporting_interval]."""  # noqa: E501

    cmor_table: Annotated[FacetValues, QueryFacet("processing_id")] = ()
    """See [Dataset.processing_id][esmporium.db.schema.Dataset.processing_id]."""

    realm: Annotated[FacetValues, QueryFacet("realm")] = ()
    """See [Dataset.realm][esmporium.query.canonical.QueryCanonical.realm]."""

    product: Annotated[FacetValues, QueryFacet(None)] = ()
    """
    Kind of product

    This expects values like "output1".
    It is not equivalent to the way product is used in other projects,
    so it has no canonical equivalent.
    """

    other_terms: FacetValuesByName = {}
    """See [Query.other_terms][(m).Query.other_terms]."""

    source_query: SourceQuery = None
    """See [Query.source_query][(m).Query.source_query]."""

    def facet_values(self) -> dict[str, tuple[str, ...]]:
        """See [QueryProtocol.facet_values][esmporium.query.languages.QueryProtocol.facet_values]."""  # noqa: E501
        return facet_values_from_attributes(self)


class QueryCMIP6(BaseModel):
    """
    A query in CMIP6's native vocabulary
    """

    model_config = DEFAULT_QUERY_MODEL_CONFIG

    project: Annotated[FacetValues, QueryFacet("project")] = ("CMIP6",)
    """
    See [Dataset.project][esmporium.db.schema.Dataset.project].
    """

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

    grid_label: Annotated[FacetValues, QueryFacet("grid_label")] = ()
    """See [Dataset.grid_label][esmporium.db.schema.Dataset.grid_label]."""

    table_id: Annotated[FacetValues, QueryFacet("processing_id")] = ()
    """See [Dataset.processing_id][esmporium.db.schema.Dataset.processing_id]."""

    activity_id: Annotated[FacetValues, QueryFacet("activity")] = ()
    """See [Dataset.activity][esmporium.query.canonical.QueryCanonical.activity]."""

    nominal_resolution: Annotated[FacetValues, QueryFacet("resolution")] = ()
    """See [Dataset.resolution][esmporium.query.canonical.QueryCanonical.resolution]."""

    realm: Annotated[FacetValues, QueryFacet("realm")] = ()
    """See [Dataset.realm][esmporium.query.canonical.QueryCanonical.realm]."""

    sub_experiment_id: Annotated[FacetValues, QueryFacet(None)] = ()
    """
    Sub-experiment within an experiment

    This is a CMIP6-only facet (with values like "s1960",
    identifying the start year of a decadal prediction).
    """

    other_terms: FacetValuesByName = {}
    """See [Query.other_terms][(m).Query.other_terms]."""

    source_query: SourceQuery = None
    """See [Query.source_query][(m).Query.source_query]."""

    def facet_values(self) -> dict[str, tuple[str, ...]]:
        """See [QueryProtocol.facet_values][esmporium.query.languages.QueryProtocol.facet_values]."""  # noqa: E501
        return facet_values_from_attributes(self)


class QueryCMIP7(BaseModel):
    """
    A query in CMIP7's native vocabulary
    """

    model_config = DEFAULT_QUERY_MODEL_CONFIG

    project: Annotated[FacetValues, QueryFacet("project")] = ("CMIP7",)
    """
    See [Dataset.project][esmporium.db.schema.Dataset.project].
    """

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

    grid_label: Annotated[FacetValues, QueryFacet("grid_label")] = ()
    """See [Dataset.grid_label][esmporium.db.schema.Dataset.grid_label]."""

    branding_suffix: Annotated[FacetValues, QueryFacet("processing_id")] = ()
    """See [Dataset.processing_id][esmporium.db.schema.Dataset.processing_id]."""

    activity_id: Annotated[FacetValues, QueryFacet("activity")] = ()
    """See [Dataset.activity][esmporium.query.canonical.QueryCanonical.activity]."""

    nominal_resolution: Annotated[FacetValues, QueryFacet("resolution")] = ()
    """See [Dataset.resolution][esmporium.query.canonical.QueryCanonical.resolution]."""

    realm: Annotated[FacetValues, QueryFacet("realm")] = ()
    """See [Dataset.realm][esmporium.query.canonical.QueryCanonical.realm]."""

    temporal_label: Annotated[FacetValues, QueryFacet(None)] = ()
    """Temporal part of CMIP7's branding suffix, e.g. "tavg"."""

    vertical_label: Annotated[FacetValues, QueryFacet(None)] = ()
    """Vertical part of CMIP7's branding suffix, e.g. "h2m"."""

    horizontal_label: Annotated[FacetValues, QueryFacet(None)] = ()
    """Horizontal part of CMIP7's branding suffix, e.g. "hxy"."""

    area_label: Annotated[FacetValues, QueryFacet(None)] = ()
    """Area part of CMIP7's branding suffix, e.g. "air"."""

    region: Annotated[FacetValues, QueryFacet(None)] = ()
    """Region the data covers, e.g. "global"."""

    other_terms: FacetValuesByName = {}
    """See [Query.other_terms][(m).Query.other_terms]."""

    source_query: SourceQuery = None
    """See [Query.source_query][(m).Query.source_query]."""

    def facet_values(self) -> dict[str, tuple[str, ...]]:
        """See [QueryProtocol.facet_values][esmporium.query.languages.QueryProtocol.facet_values]."""  # noqa: E501
        return facet_values_from_attributes(self)
