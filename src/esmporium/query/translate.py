"""
Translating between query types
"""

from collections.abc import Collection, Mapping
from typing import TypeVar

from esmporium.query.canonical_query import CANONICAL_FACETS, QueryCanonical
from esmporium.query.known_queries import (
    QueryCMIP5,
    QueryCMIP6,
    QueryCMIP7,
    UnknownProjectError,
    facet_spec,
)
from esmporium.query.protocol import QueryProtocol

Q = TypeVar("Q", bound=QueryProtocol)
"""
The type of query a translation produces
"""


class FacetNotExpressibleError(ValueError):
    """
    Raised when a query facet cannot be expressed by a requested query class.
    """

    def __init__(self, facet: str, query_class: str) -> None:
        """
        Initialise the error

        Parameters
        ----------
        facet
            The facet under consideration

        query_class
            The name of the query class in which `facet` cannot be expressed
        """
        self.facet = facet
        self.query_class = query_class
        super().__init__(f"facet {facet!r} cannot be represented in {query_class}")


class NoTargetProjectError(ValueError):
    """
    Raised when a translate call does not specify the project to render to.
    """

    def __init__(self, msg: str) -> None:
        """
        Initialise the error

        Parameters
        ----------
        msg
            The message to show the user

            Written by the caller, because only the caller knows
            which of its arguments could have supplied the project.
        """
        super().__init__(msg)


def to_canonical(query: QueryProtocol) -> QueryCanonical:
    """
    Convert a query to the canonical form

    Parameters
    ----------
    query
        Query to convert

    Returns
    -------
    :
        `query`, expressed in the canonical vocabulary
    """
    spec = facet_spec(type(query))

    canonical_fields: dict[str, tuple[str, ...]] = {}
    query_specific: dict[str, tuple[str, ...]] = {}
    for native, values in query.facet_values().items():
        canonical = spec.native_to_canonical.get(native)
        if canonical is None:
            query_specific[native] = values
        else:
            canonical_fields[canonical] = values

    return QueryCanonical(
        **canonical_fields,
        query_specific_facets=query_specific,
        other_terms=query.other_terms,
        source_query=query,
    )


def from_canonical(
    *,
    canonical: QueryCanonical,
    to: type[Q],
) -> Q:
    """
    Convert a canonical query into another query

    Parameters
    ----------
    canonical
        Query to convert

    to
        The query type to convert to

    Returns
    -------
    :
        `canonical`, as type `to` (and therefore written in `to`'s vocabulary)

    Raises
    ------
    FacetNotExpressibleError
        `canonical` sets a facet which `to` cannot express
    """
    spec = facet_spec(to)

    facets: dict[str, tuple[str, ...]] = {}

    # Sorted so a failure is reported deterministically.
    for facet in sorted(CANONICAL_FACETS):
        values = getattr(canonical, facet)
        if not values:
            continue

        native = spec.canonical_to_native.get(facet)
        if native is None:
            raise FacetNotExpressibleError(facet, spec.name)

        facets[native] = values

    for facet, values in canonical.query_specific_facets.items():
        if not values:
            continue

        if facet not in spec.query_specific_facets:
            raise FacetNotExpressibleError(facet, spec.name)

        facets[facet] = values

    return to(
        **facets, other_terms=canonical.other_terms, source_query=canonical.source_query
    )


PROJECT_QUERY_MAP_DEFAULT: Mapping[str, type[QueryProtocol]] = {
    "CMIP5": QueryCMIP5,
    "CMIP6": QueryCMIP6,
    "CMIP6Plus": QueryCMIP6,
    "CMIP7": QueryCMIP7,
}
"""
The query class to use for each project

This is our default. Anywhere we use it, you can pass your own mapping instead
(e.g. to search a project we do not know about yet).
"""


def translate_to_type(
    query: QueryProtocol,
    *,
    to: type[Q],
) -> Q:
    """
    Translate a query to another query type

    This is our low-level function.
    Most users will probably prefer to use
    [translate_to_projects][esmporium.query.translate.translate_to_projects].

    The `project` facet is translated like any other,
    i.e. it is carried across unchanged.
    Only [translate_to_projects][esmporium.query.translate.translate_to_projects]
    rewrites it.

    Parameters
    ----------
    query
        Query to translate

    to
        Type of query to translate to

    Returns
    -------
    :
        Translated query

        This is an instance of `to`, as the type hints reflect.

    Raises
    ------
    FacetNotExpressibleError
        `to` cannot express a facet in the query.
    """
    return from_canonical(to=to, canonical=to_canonical(query))


def translate_to_projects(
    query: QueryProtocol,
    *,
    projects: Collection[str] | None = None,
    project_query_map: Mapping[str, type[QueryProtocol]] | None = None,
) -> dict[str, QueryProtocol]:
    """
    Translate a query to one or more projects

    Unlike [translate_to_type][esmporium.query.translate.translate_to_type],
    each result has its `project` facet set to the project it was rendered for.

    Parameters
    ----------
    query
        Query to translate

    projects
        Projects to translate to

        If not supplied, we translate to the projects named by `query`'s
        equivalent of the canonical `project` facet.

    project_query_map
        Mapping from projects to the query class to return for them

        If not supplied, we use
        [PROJECT_QUERY_MAP_DEFAULT][esmporium.query.translate.PROJECT_QUERY_MAP_DEFAULT].

    Returns
    -------
    :
        Translated queries, keyed by the project they were rendered for

    Raises
    ------
    NoTargetProjectError
        The target projects could not be inferred

        I.e. `projects` was not supplied and `query` does not set the project

    UnknownProjectError
        We do not know what kind of query to return for a requested project

    FacetNotExpressibleError
        A requested project's query class cannot express a facet in the query.
        The call fails as a whole; no partial result is returned.

    Examples
    --------
    >>> from esmporium.query import Query
    >>>
    >>> query = Query(model=("ACCESS-CM2",), project=("CMIP5", "CMIP6", "CMIP7"))
    >>> res = translate_to_projects(query)
    >>>
    >>> # We cut out all the extra detail here and just focus on the key bits
    >>> # Project set to the target query type
    >>> res["CMIP5"].project
    ('CMIP5',)
    >>> res["CMIP5"].model
    ('ACCESS-CM2',)
    >>>
    >>> res["CMIP6"].project
    ('CMIP6',)
    >>> # Notice that this uses the CMIP6 facet name, "source_id",
    >>> # translated from the facet name that `Query` uses, "model".
    >>> res["CMIP6"].source_id
    ('ACCESS-CM2',)
    >>>
    >>> res["CMIP7"].project
    ('CMIP7',)
    >>> res["CMIP7"].source_id
    ('ACCESS-CM2',)
    >>>
    >>> # If you want, you can specify the projects to translate to instead
    >>> sorted(translate_to_projects(query, projects=["CMIP5", "CMIP7"]))
    ['CMIP5', 'CMIP7']
    >>>
    >>> # You can even inject your own support for queries
    >>> # and projects we don't know about.
    >>> # A query of your own needs no registration
    >>> # and only needs to match `QueryProtocol`:
    >>> # annotate each facet to say what it is called in the canonical vocabulary,
    >>> # and it can be translated like our queries.
    >>>
    >>> from dataclasses import dataclass, field
    >>> from typing import Annotated
    >>>
    >>> from esmporium.query import (
    ...     QueryFacet,
    ...     SourceQuery,
    ...     facet_values_from_attributes,
    ... )
    >>>
    >>> @dataclass
    ... class QueryMIP1:
    ...     # map to a different name in the canonical vocabulary
    ...     mip: Annotated[tuple[str, ...], QueryFacet("project")] = ()
    ...     esm: Annotated[tuple[str, ...], QueryFacet("model")] = ()
    ...
    ...     # specific to this query class, so nothing to translate to
    ...     vintage: Annotated[tuple[str, ...], QueryFacet(None)] = ()
    ...
    ...     # always required
    ...     other_terms: dict[str, tuple[str, ...]] = field(default_factory=dict)
    ...     source_query: SourceQuery = None
    ...
    ...     def facet_values(self) -> dict[str, tuple[str, ...]]:
    ...         return facet_values_from_attributes(self)
    >>>
    >>> # Now we have everything we need to translate.
    >>> # Firstly, out of our query class, into other ones we know:
    >>> start = QueryMIP1(esm=("ACCESS-ESM1-5",))
    >>> mip1_translated = translate_to_projects(start, projects=["CMIP5", "CMIP6"])
    >>> mip1_translated["CMIP5"].model
    ('ACCESS-ESM1-5',)
    >>> mip1_translated["CMIP6"].source_id
    ('ACCESS-ESM1-5',)
    >>>
    >>> # Into our query class, by specifying that a given project uses our query class
    >>> mip1_translated_to = translate_to_projects(
    ...     query,
    ...     projects=["CMIP5", "MIP1"],
    ...     project_query_map={"CMIP5": QueryCMIP5, "MIP1": QueryMIP1},
    ... )
    >>> mip1_translated_to["MIP1"].esm
    ('ACCESS-CM2',)
    >>> mip1_translated_to["MIP1"].mip
    ('MIP1',)
    """
    canonical = to_canonical(query)

    if projects is not None:
        target_projects: Collection[str] = projects

    else:
        # Check on canonical so we know the name is always project
        if not canonical.project:
            # Name the facet in the query's own vocabulary,
            # so the fix names something the user can actually type.
            spec = facet_spec(type(query))
            project_facet = spec.canonical_to_native.get("project")
            if project_facet is None:
                fix = (
                    "Please supply `projects` "
                    f"({spec.name} has no facet equivalent to `project`)"
                )
            else:
                fix = (
                    "Please supply `projects` "
                    f"or set the query's `{project_facet}` facet"
                )

            msg = f"`projects` was not supplied and the query does not set a project. {fix}"  # noqa: E501
            raise NoTargetProjectError(msg)

        target_projects = canonical.project

    if project_query_map is None:
        project_query_map = PROJECT_QUERY_MAP_DEFAULT

    project_query_map_lower = {k.lower(): v for k, v in project_query_map.items()}

    res = {}
    for project in target_projects:
        try:
            to = project_query_map_lower[project.lower()]
        except KeyError:
            raise UnknownProjectError(project, project_query_map) from None

        # Set the project on the canonical form rather than on the result,
        # so we do not have to know what the target class calls it
        # (or assume it is a pydantic model we can `model_copy`).
        res[project] = from_canonical(
            to=to, canonical=canonical.model_copy(update={"project": (project,)})
        )

    return res
