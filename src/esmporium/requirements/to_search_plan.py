"""
Turning a requirement into the queries which have to be searched for

A requirement says what an analysis needs. Searching is a separate step, and a
narrower one: a search API answers "what exists that matches these facets?", one set
of facets at a time. This module is the join between the two -- it reads the tree and
says which queries have to be fired to have any chance of satisfying it.

It deliberately stops there. It does not contact anything, it does not know which
projects esmporium can search, and it does not decide what to do when a leaf names no
project at all. Those are the caller's business; see
[`esmporium.search.search`][esmporium.search.search].
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from esmporium.query import QueryCanonical
from esmporium.requirements.tree import Requirement, effective_query, walk_leaves

# TODO for future: a search plan is currently the leaf queries and nothing else,
# because a leaf query is all a requirement can express today. Three things join it
# as the tree grows, and each is another field on `SearchPlan` rather than a change
# to the ones here: sibling queries (R10, from `Sibling`), auxiliary queries (R11,
# from `Aux`) and `ancestry_until` (R5, which says how far back up a lineage to
# follow parents). That is why this returns a class rather than a tuple of queries.


@dataclass(frozen=True)
class LeafSearch:
    """
    One query to search for, and the roles which asked for it
    """

    query: QueryCanonical
    """
    The query to search for

    This is the leaf's *effective* query: its own query with the requirement's
    `where` added, which is the same query
    [`solve`][esmporium.requirements.solve] matches the catalogue against. So what is
    searched for and what is later solved for cannot drift apart.
    """

    roles: tuple[str, ...]
    """
    The role paths which asked for this query, in tree order

    Usually one. It is several when two leaves ask for exactly the same thing under
    different roles, which is a real thing to write -- a pattern-scaling requirement
    might want the same `tas` as both the field and the reference -- and is searched
    for once.

    Never empty.
    """


@dataclass(frozen=True)
class SearchPlan:
    """
    Everything a requirement needs searched for, before any of it is sent
    """

    leaves: tuple[LeafSearch, ...]
    """
    One entry per distinct query the tree asks for, in tree order

    Empty is not possible: a tree has at least one leaf, and a leaf's query has to
    set at least one facet
    (see [`EmptyLeafQueryError`][esmporium.requirements.tree.EmptyLeafQueryError]).
    """


def _dedupe_key(query: QueryCanonical) -> str:
    """
    Get a key which is equal for two queries exactly when they would search the same

    Not `hash(query)`: [`QueryCanonical`][esmporium.query.QueryCanonical] is frozen
    but carries mappings, so it is unhashable.

    Not [`set_facets`][esmporium.requirements.set_facets] either, which is the
    tempting one. That flattens a query's three *homes* into one mapping, so it
    cannot tell `Query(realm="atmos")` from
    `Query(other_terms={"realm": ("atmos",)})`. Those two ask the same question of a
    catalogue and build different requests of a search API -- the second reaches the
    API spelled exactly as written -- so merging them here would silently drop one of
    the two searches.

    Parameters
    ----------
    query
        Query to key

    Returns
    -------
    :
        A key which is equal exactly for queries which would build the same request
    """
    # `sort_keys` so the key does not depend on the order the mappings happen to be
    # in, matching `Requirement.canonical_json`.
    return json.dumps(query.model_dump(mode="json"), sort_keys=True)


def to_search_plan(requirement: Requirement) -> SearchPlan:
    """
    Work out which queries have to be searched for to satisfy a requirement

    One query per leaf, with the requirement's `where` added, deduplicated so that
    two roles asking for the same dataset are searched for once.

    The plan says nothing about projects. A query may name several, or none; turning
    that into the searches to actually run is the caller's job, because which query
    class each project uses is a choice made at search time. See
    [`esmporium.search.search`][esmporium.search.search].

    Parameters
    ----------
    requirement
        Requirement to plan the searches for

    Returns
    -------
    :
        The queries to search for, in tree order

    Raises
    ------
    ConflictingFacetsError
        The requirement's `where` and one of its leaves set the same facet to
        different values

        Near-unreachable in practice:
        [`Requirement`][esmporium.requirements.Requirement] runs
        [check_where_agrees_with_leaves][esmporium.requirements.tree.check_where_agrees_with_leaves]
        when it is built, so a requirement which would raise here cannot be
        constructed. It is documented because this is where it would surface if that
        check were ever relaxed, and because the error names the role.

    ClashingFacetsError
        The requirement's `where` and one of its leaves agree on a facet's value but
        keep it in different homes. Near-unreachable, as above.

    Examples
    --------
    >>> from esmporium.query import Query
    >>> from esmporium.requirements import all_of, leaf, requirement
    >>>
    >>> ecs = requirement(
    ...     name="ecs",
    ...     tree=all_of(
    ...         leaf(Query(variable="tas"), "tas"),
    ...         leaf(Query(variable="rlut"), "rlut"),
    ...     ),
    ...     group_by=("model",),
    ...     where=Query(project="CMIP7", experiment="abrupt-4xCO2"),
    ... )
    >>> plan = to_search_plan(ecs)
    >>> for leaf_search in plan.leaves:
    ...     print(leaf_search.roles, leaf_search.query.variable)
    ('tas',) ('tas',)
    ('rlut',) ('rlut',)

    The requirement's `where` is part of every query, so a search is no broader than
    the requirement is:

    >>> plan.leaves[0].query.experiment
    ('abrupt-4xCO2',)

    Two roles which want the same dataset are searched for once:

    >>> scaling = requirement(
    ...     name="pattern-scaling",
    ...     tree=all_of(
    ...         leaf(Query(variable="tas"), "field"),
    ...         leaf(Query(variable="tas"), "reference"),
    ...     ),
    ...     group_by=("model",),
    ...     where=Query(project="CMIP7"),
    ... )
    >>> [leaf_search.roles for leaf_search in to_search_plan(scaling).leaves]
    [('field', 'reference')]
    """
    # Keyed by `_dedupe_key`, so that the second role to ask for a query joins the
    # first entry rather than adding another. `dict` keeps insertion order, which is
    # tree order, which is what the plan promises.
    found: dict[str, tuple[QueryCanonical, list[str]]] = {}

    for role_prefix, leaf_node in walk_leaves(requirement.tree):
        path = f"{role_prefix}{leaf_node.role}"
        query = effective_query(leaf_node, requirement.where)

        key = _dedupe_key(query)
        if key in found:
            found[key][1].append(path)
        else:
            found[key] = (query, [path])

    return SearchPlan(
        leaves=tuple(
            LeafSearch(query=query, roles=tuple(roles))
            for query, roles in found.values()
        )
    )
