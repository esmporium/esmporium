"""
Tests of turning a requirement into the queries to search for
"""

from __future__ import annotations

import pytest

from esmporium.query import Query, QueryCMIP6, to_canonical
from esmporium.requirements import (
    ConflictingFacetsError,
    Requirement,
    all_of,
    leaf,
    requirement,
    set_facets,
    walk_leaves,
)
from esmporium.search import to_search_plan


def a_requirement(tree, **kwargs):
    """
    Build a requirement, defaulting everything the plan does not care about
    """
    kwargs.setdefault("group_by", ("model",))
    return requirement(name="an-analysis", tree=tree, **kwargs)


def test_one_leaf_gives_one_search():
    req = a_requirement(leaf(Query(project="CMIP7", variable="tas"), "tas"))

    (only,) = to_search_plan(req).leaves

    assert only.roles == ("tas",)
    assert set_facets(only.query) == {"project": ("CMIP7",), "variable": ("tas",)}


def test_where_is_added_to_every_leaf_query():
    req = a_requirement(
        all_of(
            leaf(Query(variable="tas"), "tas"),
            leaf(Query(variable="rlut"), "rlut"),
        ),
        where=Query(project="CMIP7", experiment="abrupt-4xCO2"),
    )

    plan = to_search_plan(req)

    assert [set_facets(each.query) for each in plan.leaves] == [
        {
            "experiment": ("abrupt-4xCO2",),
            "project": ("CMIP7",),
            "variable": ("tas",),
        },
        {
            "experiment": ("abrupt-4xCO2",),
            "project": ("CMIP7",),
            "variable": ("rlut",),
        },
    ]


def test_identical_leaves_share_one_search():
    """
    Two roles asking for the same dataset are searched for once

    Not a contrived case: pattern scaling wants the same `tas` as both the field it
    scales and the reference it scales against.
    """
    req = a_requirement(
        all_of(
            leaf(Query(project="CMIP7", variable="tas"), "field"),
            leaf(Query(project="CMIP7", variable="tas"), "reference"),
        )
    )

    (only,) = to_search_plan(req).leaves

    assert only.roles == ("field", "reference")


def test_the_roles_sharing_a_search_are_in_tree_order():
    req = a_requirement(
        all_of(
            leaf(Query(project="CMIP7", variable="tas"), "zebra"),
            leaf(Query(project="CMIP7", variable="tas"), "aardvark"),
        )
    )

    (only,) = to_search_plan(req).leaves

    # Tree order, not sorted: the plan's order is the order the tree was written in.
    assert only.roles == ("zebra", "aardvark")


def test_a_search_is_shared_across_query_styles():
    """
    The same question written two ways is one search

    A leaf stores its query canonically whatever style it was written in, so the
    style cannot be what decides whether two leaves are searched for separately.
    """
    req = a_requirement(
        all_of(
            leaf(Query(project="CMIP6", variable="tas"), "canonical"),
            leaf(QueryCMIP6(variable_id="tas"), "cmip6-style"),
        )
    )

    (only,) = to_search_plan(req).leaves

    assert only.roles == ("canonical", "cmip6-style")


def test_leaves_which_differ_only_in_home_are_not_merged():
    """
    A facet named directly and the same facet in `other_terms` are two searches

    They ask a catalogue the same question, and they build *different* requests: an
    `other_terms` key reaches the API spelled exactly as written, where a declared
    facet is translated. Merging them would silently drop one of the two searches.
    """
    req = a_requirement(
        all_of(
            leaf(Query(project="CMIP7", realm="atmos"), "declared"),
            leaf(
                Query(project="CMIP7", other_terms={"realm": ("atmos",)}),
                "escape-hatch",
            ),
        )
    )

    plan = to_search_plan(req)

    assert [each.roles for each in plan.leaves] == [("declared",), ("escape-hatch",)]


def test_searches_come_back_in_tree_order():
    req = a_requirement(
        all_of(
            leaf(Query(project="CMIP7", variable="tas"), "tas"),
            all_of(
                leaf(Query(project="CMIP7", variable="rsdt"), "rsdt"),
                leaf(Query(project="CMIP7", variable="rlut"), "rlut"),
            ),
            leaf(Query(project="CMIP7", variable="rsut"), "rsut"),
        )
    )

    plan = to_search_plan(req)

    walked = [f"{prefix}{each.role}" for prefix, each in walk_leaves(req.tree)]
    assert [each.roles[0] for each in plan.leaves] == walked


def test_a_facet_set_twice_differently_is_refused_with_the_role_named():
    """
    A `where` which contradicts a leaf raises, and says which leaf

    `Requirement` makes this unreachable through the public factories -- it runs the
    same check when it is built -- so the requirement is assembled field by field to
    get a tree the plan has to refuse. Pinned because the error naming the role is
    the whole value of raising it here rather than later.
    """
    req = a_requirement(leaf(Query(project="CMIP7", variable="tas"), "control"))
    # `model_construct` skips the validators, which is the only way to get a
    # requirement whose `where` contradicts a leaf.
    contradicting = Requirement.model_construct(
        name=req.name,
        tree=req.tree,
        where=to_canonical(Query(variable="pr")),
        group_by=req.group_by,
        prefer=req.prefer,
        cardinality=req.cardinality,
    )

    with pytest.raises(ConflictingFacetsError, match="control"):
        to_search_plan(contradicting)


def test_the_plan_is_project_agnostic():
    """
    A leaf naming no project is not this function's problem

    Refusing it is the caller's job, because which projects can be searched and how
    is a search-time choice. The plan just reports the query.
    """
    req = a_requirement(leaf(Query(variable="tas"), "tas"))

    (only,) = to_search_plan(req).leaves

    assert only.query.project == ()
