"""
Tests of the requirement tree
"""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from esmporium.query import (
    ClashingFacetsError,
    Query,
    QueryCanonical,
    QueryCMIP5,
    QueryCMIP6,
)
from esmporium.requirements import (
    AllOf,
    ConflictingFacetsError,
    DuplicateRoleError,
    EmptyLeafQueryError,
    Leaf,
    NotANodeError,
    Requirement,
    all_of,
    leaf,
    requirement,
    set_facets,
)
from esmporium.requirements.tree import (
    apply_to_leaves,
    effective_query,
    role_paths,
    walk_leaves,
)


def a_leaf(role: str = "field", **facets) -> Leaf:
    """Get a leaf which asks for something, when what it asks is not the point."""
    return leaf(Query(variable="tas", **facets), role)


# ------------------------------------------------------------------ the stored query


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        pytest.param(Query(variable="tas"), {"variable": ("tas",)}, id="canonical"),
        pytest.param(
            QueryCMIP6(variable_id="tas"),
            {"project": ("CMIP6",), "variable": ("tas",)},
            id="cmip6",
        ),
        pytest.param(
            QueryCMIP5(variable="tas"),
            {"project": ("CMIP5",), "variable": ("tas",)},
            id="cmip5",
        ),
    ],
)
def test_any_query_style_is_accepted_and_stored_canonical(query, expected):
    stored = leaf(query, "field")

    # Translated on the way in, so what comes back out is not what went in.
    assert isinstance(stored.query, QueryCanonical)
    assert set_facets(stored.query) == expected


def test_a_project_specific_facet_keeps_its_own_home():
    # CMIP5's `product` is modelled, just not under a canonical name, so it has a
    # home of its own and does not belong in the escape hatch.
    stored = leaf(QueryCMIP5(variable="tas", product="output1"), "field")

    assert stored.query.query_specific_facets == {"product": ("output1",)}
    assert stored.query.other_terms == {}


def test_other_terms_is_not_relocated():
    # `variable` is a facet we model, but it was written in the escape hatch, so it
    # stays in the escape hatch. An escape hatch which rewrites what you put in it
    # is not one.
    stored = leaf(Query(other_terms={"variable": ("tas",)}), "field")

    assert stored.query.other_terms == {"variable": ("tas",)}
    assert stored.query.variable == ()


def test_an_already_canonical_query_passes_through_untouched():
    # Canonicalising is idempotent, which matters because `.where()` feeds its own
    # output back in on every call.
    query = QueryCanonical(variable=("tas",))
    stored = leaf(query, "field")

    assert stored.query == query
    assert stored.query.source_query is None


def test_the_stored_query_is_frozen():
    # A leaf is frozen so its hash cannot go stale; that is only true if what it
    # holds is frozen too.
    stored = a_leaf()

    with pytest.raises(ValidationError, match=r"variable\s*\n\s*Instance is frozen"):
        stored.query.variable = ("pr",)


def test_a_query_with_no_facets_is_refused():
    with pytest.raises(
        EmptyLeafQueryError,
        match=re.escape(
            "sets no facets, so it identifies no particular dataset "
            "and would claim every dataset in the catalogue"
        ),
    ) as excinfo:
        leaf(Query(), "field")

    assert excinfo.value.role == "field"
    # Carrying the role is only worth anything if it reaches the message: a tree has
    # many leaves and the reader has to be told which one.
    assert "leaf 'field'" in str(excinfo.value)


def test_an_empty_query_is_still_refused_when_loading():
    # Written by hand the error arrives as itself; loaded, pydantic wraps it, so that
    # one bad leaf is reported alongside everything else wrong with the document.
    # Both paths have to refuse it.
    with pytest.raises(
        ValidationError,
        match=re.escape(
            "The query on leaf 'field' sets no facets, so it identifies no "
            "particular dataset and would claim every dataset in the catalogue"
        ),
    ):
        Leaf.model_validate_json('{"kind":"leaf","query":{},"role":"field"}')


# -------------------------------------------------------------------------- the role


@pytest.mark.parametrize(
    "role",
    [pytest.param("", id="empty"), pytest.param("   ", id="whitespace")],
)
def test_role_must_say_something(role):
    with pytest.raises(
        ValidationError,
        match=re.escape(
            "Roles must have some non-whitespace content and contain no '.'"
        ),
    ) as excinfo:
        leaf(Query(variable="tas"), role)

    # Our own message echoes the role back, which for whitespace is the only way the
    # reader sees what they wrote. `got ...` rather than a bare `repr`, because
    # pydantic appends `input_value=` itself and a bare repr would pass on that alone.
    assert f"got {role!r}" in str(excinfo.value)


def test_role_cannot_contain_a_dot():
    # `.` separates the nested paths which arrive with lineage and scopes.
    with pytest.raises(
        ValidationError,
        match=re.escape(
            "Roles must have some non-whitespace content and contain no '.', "
            "got 'control.field'"
        ),
    ):
        leaf(Query(variable="tas"), "control.field")


def test_a_role_is_a_label_and_not_policed_further():
    # Capitals, spaces within and length are the user's business, not ours.
    assert a_leaf(role="Land area").role == "Land area"


# -------------------------------------------------------------------------- the tree


def test_all_of_needs_nodes_not_facet_values():
    # Writing a facet value where a leaf was meant is the likely mistake, so the
    # message names what arrived and what a node is, and `all_of` raises rather than
    # letting pydantic reject it against the discriminated union.
    with pytest.raises(
        NotANodeError,
        match=re.escape(
            "Expected a node, i.e. one of 'Leaf' and 'AllOf', got str: 'tas'. "
            "A leaf takes a query, everything else takes nodes."
        ),
    ) as excinfo:
        all_of("tas")

    assert excinfo.value.value == "tas"


def test_all_of_needs_at_least_one_child():
    with pytest.raises(
        ValidationError,
        match=(
            r"children\s*\n\s*"
            r"Tuple should have at least 1 item after validation, not 0"
        ),
    ):
        all_of()


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(all_of, id="siblings"),
        pytest.param(
            lambda one, two: all_of(one, all_of(two)),
            id="nested",
        ),
    ],
)
def test_duplicate_roles_are_refused(build):
    # Raised by `all_of` before pydantic sees the children, so it arrives as itself
    # rather than buried in a `ValidationError`.
    with pytest.raises(
        DuplicateRoleError,
        match=re.escape(
            "are used more than once. A role names one dataset, so a repeat "
            "leaves no way to say which dataset is meant"
        ),
    ) as excinfo:
        build(a_leaf(role="field"), a_leaf(role="field"))

    assert excinfo.value.roles == ("field",)
    assert "Roles 'field' are used" in str(excinfo.value)


def test_role_paths_are_flat_for_now():
    # One path per leaf. R4 and R8 are what give them prefixes (`control.field`),
    # and this is the test which records that they do not have any yet.
    tree = all_of(a_leaf(role="field"), all_of(a_leaf(role="land_area")))

    assert role_paths(tree) == {"field", "land_area"}


@pytest.mark.parametrize(
    "walker",
    [
        pytest.param(role_paths, id="role_paths"),
        pytest.param(lambda node: list(walk_leaves(node)), id="walk_leaves"),
        pytest.param(
            lambda node: apply_to_leaves(node, lambda each_leaf: each_leaf),
            id="apply_to_leaves",
        ),
    ],
)
def test_the_walkers_refuse_a_non_node(walker):
    # Each walker ends in a raise rather than falling through, so that a node type
    # added later without a branch fails loudly instead of being skipped.
    with pytest.raises(
        NotANodeError,
        match=re.escape(
            "Expected a node, i.e. one of 'Leaf' and 'AllOf', got str: 'tas'. "
            "A leaf takes a query, everything else takes nodes"
        ),
    ):
        walker("tas")


# ------------------------------------------------------------------------- .where()


def test_where_adds_facets_to_every_leaf():
    tree = all_of(
        a_leaf(role="field"),
        all_of(leaf(Query(variable="sftlf"), "land_fraction")),
    )

    updated = tree.where(Query(experiment="1pctCO2", reporting_interval="mon"))

    for _, each_leaf in walk_leaves(updated):
        facets = set_facets(each_leaf.query)
        assert facets["experiment"] == ("1pctCO2",)
        assert facets["reporting_interval"] == ("mon",)


def test_where_can_add_a_project_specific_facet():
    # The reason `.where()` takes a query rather than keyword facets. Keywords could
    # only name what `Query` declares, so CMIP5's `product` -- which a leaf holds
    # perfectly well -- was a facet no node above it could add.
    tree = all_of(a_leaf(role="field"))

    updated = tree.where(QueryCMIP5(product="output1"))

    # And it lands in its own home, not shoved into the escape hatch, which matters
    # because home is part of the question: the two hash differently.
    query = updated.children[0].query
    assert query.query_specific_facets == {"product": ("output1",)}
    assert query.other_terms == {}


def test_where_contradicting_a_leaf_is_an_error():
    tree = all_of(a_leaf(role="field", reporting_interval="day"))

    with pytest.raises(
        ConflictingFacetsError,
        match=(
            r"where sets 'reporting_interval' differently to leaf .*\. "
            r"Set each facet once: on the leaf or in where, not both"
        ),
    ) as excinfo:
        tree.where(Query(reporting_interval="mon"))

    # Which facet disagrees is in the `match` above; this is which leaf it was on.
    assert excinfo.value.role == "field"
    assert "leaf 'field'" in str(excinfo.value)

    # The half most likely to regress: `where` exists to say something about every
    # leaf, so a leaf which already says the same thing is fine, not a clash.
    agreed = tree.where(Query(reporting_interval="day"))
    assert set_facets(agreed.children[0].query)["reporting_interval"] == ("day",)


def test_requirement_where_contradicting_a_leaf_is_an_error():
    # Checked when the requirement is built, rather than when the facets are used.
    with pytest.raises(
        ConflictingFacetsError,
        match=re.escape(
            "where sets 'reporting_interval' differently to leaf 'field'. "
            "Set each facet once: on the leaf or in where, not both"
        ),
    ):
        requirement(
            tree=all_of(a_leaf(role="field", reporting_interval="day")),
            name="clash",
            group_by=("model",),
            where=Query(reporting_interval="mon"),
        )


def test_a_leaf_with_nothing_above_it_keeps_its_own_query():
    # `effective_query` takes `where` as optional, so it has to answer for a leaf
    # which has no requirement above it.
    only_leaf = a_leaf(role="field")

    assert effective_query(only_leaf, None) == only_leaf.query


def test_agreeing_in_a_different_home_is_a_clash():
    # Same facet, same value, two different homes. Nothing disagrees about the value,
    # so it is not a conflict -- it is that where the facet belongs is ambiguous.
    tree = all_of(leaf(Query(other_terms={"experiment": ("historical",)}), "field"))

    with pytest.raises(
        ClashingFacetsError,
        match=re.escape(
            "`other_terms` facet 'experiment' clashes with the query's facet "
            "names. Set each facet either as a query facet or in `other_terms`, "
            "not both"
        ),
    ):
        tree.where(Query(experiment="historical"))


def test_a_value_disagreement_is_reported_before_a_home_one():
    # Both are wrong here. The value disagreement is the more useful complaint, so it
    # is the one raised.
    tree = all_of(leaf(Query(other_terms={"experiment": ("historical",)}), "field"))

    with pytest.raises(
        ConflictingFacetsError,
        match=re.escape(
            "where sets 'experiment' differently to leaf 'field'. "
            "Set each facet once: on the leaf or in where, not both"
        ),
    ):
        tree.where(Query(experiment="1pctCO2"))


# ---------------------------------------------------------- Requirement: storing it


def a_requirement(**overrides) -> Requirement:
    """Get a requirement which uses every field, so round-trips mean something."""
    settings = {
        "name": "an-analysis",
        "where": Query(reporting_interval="mon"),
        "group_by": ("model", "variant_label", "experiment"),
        "prefer": {"grid_label": ("gn", "gr")},
        "cardinality": "one",
        **overrides,
    }

    return requirement(tree=all_of(a_leaf(role="field")), **settings)


def test_round_trip():
    # Also what covers `prefer` and `cardinality` being stored and hashed. Neither
    # does anything until the solver reads them, so neither gets a test of its own.
    requirement = a_requirement()

    loaded = Requirement.model_validate_json(requirement.model_dump_json())

    assert loaded == requirement
    assert loaded.requirement_hash() == requirement.requirement_hash()


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"name": "another-analysis"}, id="name"),
        pytest.param({"group_by": ("model",)}, id="group_by"),
        pytest.param({"prefer": {"grid_label": ("gr",)}}, id="prefer"),
        pytest.param({"cardinality": "all"}, id="cardinality"),
        pytest.param({"where": Query(reporting_interval="day")}, id="where"),
    ],
)
def test_hash_changes_with_content(change):
    assert (
        a_requirement(**change).requirement_hash() != a_requirement().requirement_hash()
    )


def test_hash_ignores_query_style_but_not_home():
    def with_query(query):
        return requirement(
            tree=all_of(leaf(query, "field")),
            name="x",
            group_by=("model",),
        )

    # Style is normalised away: these are one question written two ways.
    in_cmip6_names = with_query(QueryCMIP6(variable_id="tas"))
    in_our_names = with_query(Query(variable="tas", project="CMIP6"))
    assert in_cmip6_names.requirement_hash() == in_our_names.requirement_hash()

    # Home is not, because a home is chosen rather than guessed at, so choosing
    # another one asks a different question.
    declared = with_query(Query(variable="tas"))
    escaped = with_query(Query(other_terms={"variable": ("tas",)}))
    assert declared.requirement_hash() != escaped.requirement_hash()


def test_project_specific_facets_are_allowed_when_building():
    # Whether a facet can be answered is the catalogue's business, so it is not
    # checked here -- only when the requirement is solved.
    requirement = a_requirement(
        group_by=("model", "product"), prefer={"product": ("output1", "output2")}
    )

    assert requirement.group_by == ("model", "product")


def test_a_requirement_is_a_root_and_not_a_node():
    # `Requirement` holds a tree rather than being part of one, which is why it has
    # no `kind` and cannot be nested.
    assert not isinstance(a_requirement(), (Leaf, AllOf))
