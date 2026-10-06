"""
Working out which searches a requirement asks for

A requirement is a tree of leaves, each carrying a query, and a query may name
several projects. One search goes to one project. So between a requirement and the
requests which get fired there is a flattening step, and this is it.

Shared by [search][esmporium.search.search] and
[check_query_values][esmporium.search.check_query_values] because they ask the same
question of the same requirement -- one sends it to the search endpoints, the other
asks whether its values exist -- and the error raised for a leaf which names no
project should read the same either way.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from esmporium.formatting import readable_list
from esmporium.query import (
    NoTargetProjectError,
    QueryProtocol,
    translate_canonical_to_projects,
)
from esmporium.requirements import to_search_plan

if TYPE_CHECKING:
    from collections.abc import Mapping

    from esmporium.requirements import Requirement


@dataclass(frozen=True)
class SubSearch:
    """
    One query, rendered for one project, and what in the requirement asked for it
    """

    roles: tuple[str, ...]
    """
    The role paths which asked for this, in tree order

    Several when two leaves ask for exactly the same dataset under different roles,
    which is searched for once. Never empty. See
    [`LeafSearch.roles`][esmporium.requirements.LeafSearch.roles].
    """

    project: str
    """The project this was rendered for"""

    query: QueryProtocol
    """
    The query to send, in the style this project uses

    A leaf stores its query canonically. This is that query translated into the query
    class `project` uses, which is the form a facade can build a request from.
    """

    def label(self) -> str:
        """
        Name this sub-search for a message

        Returns
        -------
        :
            The roles which asked for it and the project it went to

        Examples
        --------
        >>> from esmporium.query import Query
        >>> print(SubSearch(roles=("tas",), project="CMIP7", query=Query()).label())
        'tas' (CMIP7)
        >>> print(
        ...     SubSearch(
        ...         roles=("field", "reference"), project="CMIP6", query=Query()
        ...     ).label()
        ... )
        'field' and 'reference' (CMIP6)
        """
        return f"{readable_list(self.roles)} ({self.project})"


def plan_sub_searches(
    requirement: Requirement,
    *,
    project_query_map: Mapping[str, type[QueryProtocol]] | None = None,
) -> tuple[SubSearch, ...]:
    """
    Work out every search a requirement asks for, one project at a time

    Two steps: [to_search_plan][esmporium.requirements.to_search_plan] reads the tree
    and gives one query per distinct leaf, then each of those is split into one query
    per project it names.

    Parameters
    ----------
    requirement
        Requirement to plan the searches for

    project_query_map
        Passed to
        [translate_canonical_to_projects][esmporium.query.translate_canonical_to_projects],
        to control which query class each project uses.
        If `None`, the default mapping is used.

    Returns
    -------
    :
        The searches to run, leaves in tree order and, within a leaf, projects in the
        order its query names them

    Raises
    ------
    NoTargetProjectError
        A leaf's query names no project, so there is nothing to search

        Re-raised naming the roles which asked, because a requirement can have many
        leaves and the message is no use if it does not say which one to go and fix.

    ConflictingFacetsError
        The requirement's `where` contradicts one of its leaves. Near-unreachable;
        see [to_search_plan][esmporium.requirements.to_search_plan].

    UnknownProjectError
        A leaf names a project we have no query class for

    FacetNotExpressibleError
        A project's query class cannot express a facet one of the leaves names
    """
    planned: list[SubSearch] = []

    for leaf_search in to_search_plan(requirement).leaves:
        try:
            by_project = translate_canonical_to_projects(
                leaf_search.query, project_query_map=project_query_map
            )
        except NoTargetProjectError as exc:
            # Deliberately not folding in `exc`'s own message: it offers
            # `projects`, which is an argument of `translate_canonical_to_projects`
            # and not something the caller of a requirement-driven search has.
            # The only fix here is to put the facet on the requirement.
            roles = readable_list(leaf_search.roles)
            msg = (
                f"{roles} of requirement {requirement.name!r} names no project, "
                "so there is nothing to search. "
                "Set the `project` facet on that leaf's query, "
                "or on the requirement's `where` if every leaf should have it."
            )
            raise NoTargetProjectError(msg) from exc

        planned.extend(
            SubSearch(roles=leaf_search.roles, project=project, query=query)
            for project, query in by_project.items()
        )

    return tuple(planned)
