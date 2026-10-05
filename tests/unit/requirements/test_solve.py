"""
Tests of the solver: filling a requirement's roles from a catalogue, group by group

Two things are tested here which usually would not be. The *wording* of the
explanations, because they are the output rather than a debugging aid: most groups
in a real archive do not resolve, and a user decides whether to widen a query, set
`prefer`, or accept the loss by reading these lines. And the *order* groups come
out in, because an explanation which shuffled between runs could not be diffed.

Facet values are readable here (`model=ModelA`) rather than derived from the
column names, for the same reason: a test which pins a message has to pin one a
person could have read.
"""

from __future__ import annotations

import re

import pytest

from esmporium.query import Query, QueryCMIP5
from esmporium.requirements import (
    InMemoryCatalogue,
    NotANodeError,
    Requirement,
    UnrecordedFacetError,
    all_of,
    leaf,
    requirement,
    solve,
)
from esmporium.requirements.solve import _Context, _eval, _label

GROUP_BY = ("model", "variant_label")
"""What most analyses group by, and what these tests use unless grouping is the point"""

MONTHLY = Query(reporting_interval="mon")
"""Facets a requirement adds to every leaf, set once rather than per leaf"""

READABLE_FACETS = {
    "project": "CMIP6",
    "model": "ModelA",
    "experiment": "historical",
    "variant_label": "r1i1p1f1",
    "variable": "tas",
    "reporting_interval": "mon",
    "grid_label": "gn",
    "processing_id": "Amon",
}
"""
Values for the facets these tests talk about, in the projects' own language

Only the facets which appear in an assertion are named. Anything else keeps what
`get_dataset_kwargs` builds from the column, so a facet added to `Dataset` still
needs no change here.
"""

ONLY_GROUP = tuple((facet, READABLE_FACETS[facet]) for facet in GROUP_BY)
"""The group every dataset below falls in, unless a test deliberately says otherwise"""

VARIABLES_TOGETHER = ("tas", "rsdt", "rlut")
"""
Variables an analysis needs at once, i.e. one leaf each under one `all_of`

Three rather than two, because two is enough for a group to be missing one, and
three also shows `all_of`'s label joining more than two. One constant rather than
one default each, so that a requirement and the catalogue built for it cannot fall
out of step.
"""


def tas_requirement(**overrides) -> Requirement:
    """Get a requirement for one dataset, for when the tree is not the point."""
    settings = {
        "name": "tas",
        "tree": leaf(Query(variable="tas"), "tas"),
        "group_by": GROUP_BY,
        **overrides,
    }

    return requirement(**settings)


# @Claude, hesitant to call this Gregory because it's using historical data?
# Not abrupt-4xco2 and picontrol plus you don't even ask for all variables
# ...But it's good to test multiple variables for all_of
#
# Answered by the rename below: the fixture was never a Gregory regression (no
# control to regress against, and `rsut` missing), it was several leaves under one
# `all_of`, which is what it is now called. This block is yours to delete.
def variables_together(variables=VARIABLES_TOGETHER, **overrides) -> Requirement:
    """
    Get a requirement whose leaves are all needed together, one per variable

    The shape of any analysis which regresses or differences one field against
    another: the datasets are needed *at once*, so it is several leaves under one
    `all_of` rather than several requirements, and a group missing any one of them
    cannot be run.

    The role is the variable's own name, because when each leaf really is a
    different variable that is the clearest thing to call it.
    """
    settings = {
        "name": "variables-together",
        "tree": all_of(
            *(leaf(Query(variable=variable), variable) for variable in variables)
        ),
        "group_by": GROUP_BY,
        "where": MONTHLY,
        **overrides,
    }

    return requirement(**settings)


A_CONTEXT = _Context(
    requirement=tas_requirement(), catalogue=InMemoryCatalogue(entries=()), group={}
)
"""A context for calling the dispatch functions directly, when it does not matter"""


@pytest.fixture
def dataset(make_entry):
    """
    Get a factory for one stored dataset, with facet values a person can read

    Takes what `make_entry` takes: the entry's `id`, optionally `extra` and
    `label`, and any facet to override.
    """

    def factory(entry_id, **facets):
        return make_entry(entry_id, **{**READABLE_FACETS, **facets})

    return factory


@pytest.fixture
def variables_catalogue(dataset):
    """
    Get a factory for a catalogue holding one dataset per variable, for one model

    Takes the variables to publish, so that leaving one out is how a test says a
    dataset is missing, and any facet to override on every entry. Defaults to the
    variables [variables_together][(m).variables_together] asks for.
    """

    def factory(variables=VARIABLES_TOGETHER, **facets):
        return InMemoryCatalogue(
            entries=tuple(
                dataset(index, variable=variable, **facets)
                for index, variable in enumerate(variables, start=1)
            )
        )

    return factory


# ------------------------------------------------------------------------- resolving


def test_all_satisfied(variables_catalogue):
    result = solve(variables_together(), variables_catalogue())

    assert list(result.resolved) == [ONLY_GROUP]
    assert not (result.unsatisfied or result.ambiguous)

    group = result.resolved[ONLY_GROUP]
    assert {role: group.one(role).variable for role in group.roles} == {
        "tas": "tas",
        "rsdt": "rsdt",
        "rlut": "rlut",
    }
    assert group.explanation.status == "satisfied"


def test_required_variable_missing(variables_catalogue):
    result = solve(variables_together(), variables_catalogue(("tas", "rsdt")))

    assert not result.resolved
    rendered = result.unsatisfied[ONLY_GROUP].explanation.render()

    # The group is named on the leaf's own line as well as on the block above it,
    # because the group is filtered on the entry rather than in the query: without
    # it this line reads as "there is no `rlut` at all", which is a different and
    # here untrue statement.
    assert "[unsatisfied] rlut: no dataset matches" in rendered
    assert "variable=rlut" in rendered
    assert "for model=ModelA, variant_label=r1i1p1f1" in rendered


def test_a_role_always_holds_a_tuple(variables_catalogue):
    # The promise `ResolvedGroup.roles` makes: one shape to handle, whatever the
    # cardinality, so that a reader never has to ask which it got.
    group = solve(variables_together(), variables_catalogue()).resolved[ONLY_GROUP]

    assert all(isinstance(entries, tuple) for entries in group.roles.values())


def test_the_result_carries_the_requirement_it_solved(variables_catalogue):
    # What makes "has this become satisfiable?" a comparison of two results: the
    # hashes agreeing is how a reader knows both solves asked the same question.
    asked = variables_together()

    result = solve(asked, variables_catalogue())

    assert result.requirement.requirement_hash() == asked.requirement_hash()


# ------------------------------------------------------- groups: discovery and order


def test_group_by_variable_fans_out(dataset):
    # One leaf offering four variables, with `variable` grouped on: one run per
    # variable rather than one variable chosen. Only three were published, and the
    # fourth produces no group at all -- groups are discovered, never listed.
    per_field = requirement(
        name="per-field-analysis",
        tree=leaf(Query(variable=("tas", "pr", "rsdt", "rlut")), "field"),
        group_by=("model", "variable"),
        where=MONTHLY,
    )
    catalogue = InMemoryCatalogue(
        entries=tuple(
            dataset(index, variable=variable)
            for index, variable in enumerate(("tas", "pr", "rsdt"), start=1)
        )
    )

    result = solve(per_field, catalogue)

    assert [dict(key)["variable"] for key in result.resolved] == ["pr", "rsdt", "tas"]
    assert not (result.unsatisfied or result.ambiguous)


def test_group_by_experiment_fans_out(dataset):
    scenarios = requirement(
        name="per-experiment-analysis",
        tree=leaf(Query(variable="tas", experiment=("historical", "ssp126")), "field"),
        group_by=("model", "experiment"),
        where=MONTHLY,
    )
    catalogue = InMemoryCatalogue(
        entries=(
            dataset(1, experiment="historical"),
            dataset(2, experiment="ssp126"),
        )
    )

    result = solve(scenarios, catalogue)

    assert [dict(key)["experiment"] for key in result.resolved] == [
        "historical",
        "ssp126",
    ]


def test_a_group_is_discovered_from_any_leaf(dataset):
    # The scenario has `tas` but no `rlut`, so no leaf finds the whole group. It is
    # still discovered, and reported unsatisfied, rather than quietly dropped: a
    # group nobody mentions is indistinguishable from an analysis nobody asked for.
    catalogue = InMemoryCatalogue(
        entries=(
            dataset(1, experiment="historical", variable="tas"),
            dataset(2, experiment="historical", variable="rlut"),
            dataset(3, experiment="ssp126", variable="tas"),
        )
    )

    result = solve(
        variables_together(("tas", "rlut"), group_by=("model", "experiment")),
        catalogue,
    )

    assert [dict(key)["experiment"] for key in result.resolved] == ["historical"]
    assert [dict(key)["experiment"] for key in result.unsatisfied] == ["ssp126"]


def test_groups_come_out_in_a_stable_order(dataset):
    # Sorted, so that two runs of the same solve produce the same explanation and a
    # difference between them means the data changed. `None` is a real value here --
    # CMIP5 has no concept of a grid -- and sorts as though it were empty.
    grouped_on_grid = requirement(
        name="per-grid",
        tree=leaf(Query(variable="tas"), "tas"),
        group_by=("model", "grid_label"),
    )
    catalogue = InMemoryCatalogue(
        entries=(
            dataset(1, grid_label="gr"),
            dataset(2, grid_label=None),
            dataset(3, grid_label="gn"),
        )
    )

    result = solve(grouped_on_grid, catalogue)

    assert [dict(key)["grid_label"] for key in result.resolved] == [None, "gn", "gr"]
    assert "[satisfied] model=ModelA, grid_label=None" in result.explain()


def test_every_group_lands_in_exactly_one_bucket(dataset):
    # The invariant `SolveResult` states: every group which was discovered appears
    # in exactly one of the three mappings. One catalogue, one group of each kind.
    catalogue = InMemoryCatalogue(
        entries=(
            dataset(1, experiment="historical", variable="tas"),
            dataset(2, experiment="historical", variable="rlut"),
            dataset(3, experiment="ssp126", variable="tas"),
            dataset(4, experiment="ssp585", variable="tas", grid_label="gn"),
            dataset(5, experiment="ssp585", variable="tas", grid_label="gr"),
            dataset(6, experiment="ssp585", variable="rlut"),
        )
    )

    result = solve(
        variables_together(("tas", "rlut"), group_by=("model", "experiment")),
        catalogue,
    )

    buckets = (result.resolved, result.unsatisfied, result.ambiguous)
    keys = [key for bucket in buckets for key in bucket]
    assert len(keys) == len(set(keys)) == 3
    assert [[dict(key)["experiment"] for key in bucket] for bucket in buckets] == [
        ["historical"],
        ["ssp126"],
        ["ssp585"],
    ]


# ------------------------------------------------------------- ambiguity and prefer


def test_ambiguous_grids_and_prefer(dataset):
    # The same data published on two grids. Nothing in the requirement says which
    # to take, so the solver refuses to guess and says what would settle it.
    catalogue = InMemoryCatalogue(
        entries=(dataset(1, grid_label="gn"), dataset(2, grid_label="gr"))
    )

    ambiguous = solve(tas_requirement(), catalogue)
    preferred = solve(tas_requirement(prefer={"grid_label": ("gn", "gr")}), catalogue)

    assert list(ambiguous.ambiguous) == [ONLY_GROUP]
    rendered = ambiguous.explain()
    assert "2 candidates, differing in 'grid_label'" in rendered
    assert "#1 ('entry-1_ps') with grid_label='gn'" in rendered
    # All three ways out are offered, because which one is right is the user's
    # judgement and not something the solver can work out.
    assert "set `prefer` on the requirement" in rendered
    assert "narrow the query" in rendered
    assert "use `cardinality='all'` to keep them all" in rendered

    assert preferred.resolved[ONLY_GROUP].one("tas").grid_label == "gn"


def test_ambiguous_despite_prefer_says_what_was_preferred(dataset):
    # Both candidates are on the preferred grid, so `prefer` ranked them equally
    # and the tie survives. The message has to say that, rather than repeat the
    # advice to set a `prefer` which is already set.
    catalogue = InMemoryCatalogue(
        entries=(
            dataset(1, institution="InstA"),
            dataset(2, institution="InstB"),
        )
    )

    result = solve(tas_requirement(prefer={"grid_label": ("gn", "gr")}), catalogue)

    assert list(result.ambiguous) == [ONLY_GROUP]
    assert (
        "Preferring grid_label in the order 'gn' and 'gr' did not narrow this to "
        "one, since the candidates left over are equally preferred: add the facet "
        "they differ on to `prefer`, or narrow the query."
    ) in result.explain()


def test_candidates_which_agree_on_every_facet(dataset):
    # Two rows our dataset model cannot tell apart on any facet. No `prefer` and no
    # query could choose between them, so the message says so instead of advising
    # something which cannot be done.
    catalogue = InMemoryCatalogue(
        entries=(dataset(1, label="one"), dataset(2, label="two"))
    )

    result = solve(tas_requirement(), catalogue)

    rendered = result.explain()
    assert "2 candidates which agree on every facet" in rendered
    assert "#1 ('one_ps'), #2 ('two_ps')" in rendered
    assert "They differ only in their project-specific ID" in rendered


def test_a_value_not_in_prefer_ranks_behind_one_that_is(dataset):
    # `prefer` says which is better, not which is allowed, so an unlisted value is
    # ranked last rather than dropped -- and it still wins when nothing beats it.
    catalogue = InMemoryCatalogue(
        entries=(dataset(1, grid_label="gn"), dataset(2, grid_label="gr"))
    )

    listed_second = solve(tas_requirement(prefer={"grid_label": ("gr",)}), catalogue)
    nothing_listed = solve(tas_requirement(prefer={"grid_label": ("gm",)}), catalogue)

    assert listed_second.resolved[ONLY_GROUP].one("tas").grid_label == "gr"
    # Neither value is listed, so neither is preferred and the tie is untouched.
    assert list(nothing_listed.ambiguous) == [ONLY_GROUP]


def test_prefer_applies_every_facet_it_names(dataset):
    # Two facets, each doing part of the work: the grid rules one candidate out and
    # the table rules out another, which is only true if both are applied.
    catalogue = InMemoryCatalogue(
        entries=(
            dataset(1, grid_label="gn", processing_id="AERmon"),
            dataset(2, grid_label="gn", processing_id="Amon"),
            dataset(3, grid_label="gr", processing_id="Amon"),
        )
    )

    result = solve(
        tas_requirement(
            prefer={"grid_label": ("gn", "gr"), "processing_id": ("Amon", "AERmon")}
        ),
        catalogue,
    )

    assert result.resolved[ONLY_GROUP].one("tas").id == 2


def test_differing_facets_only_names_what_every_candidate_knows(dataset):
    # `realm` is in one candidate's `extra` and not the other's, so asking either
    # about it would raise. A message about an ambiguity must not fail with an
    # error of its own, so only the facets every candidate knows are compared.
    catalogue = InMemoryCatalogue(
        entries=(
            dataset(1, extra={"product": "output1", "realm": "atmos"}),
            dataset(2, extra={"product": "output2"}),
        )
    )

    rendered = solve(tas_requirement(), catalogue).explain()

    assert "differing in 'product'" in rendered
    assert "realm" not in rendered


# -------------------------------------------------------------- cardinality="all"


def test_cardinality_all_keeps_every_candidate(dataset):
    # The same tie as `test_ambiguous_grids_and_prefer`, with the requirement
    # saying it is not a tie: the analysis wants the spread across candidates.
    catalogue = InMemoryCatalogue(
        entries=(dataset(1, grid_label="gn"), dataset(2, grid_label="gr"))
    )

    result = solve(tas_requirement(cardinality="all"), catalogue)

    assert not result.ambiguous
    # In the order the catalogue gave them, so the result is reproducible.
    assert [entry.id for entry in result.resolved[ONLY_GROUP].roles["tas"]] == [1, 2]


def test_one_refuses_a_role_holding_several(dataset):
    catalogue = InMemoryCatalogue(
        entries=(dataset(1, grid_label="gn"), dataset(2, grid_label="gr"))
    )
    group = solve(tas_requirement(cardinality="all"), catalogue).resolved[ONLY_GROUP]

    with pytest.raises(
        ValueError,
        match=re.escape(
            "'tas' holds 2 datasets, so there is no single one to return. "
            "Read `roles` directly, or solve with `cardinality='one'`."
        ),
    ):
        group.one("tas")


def test_one_raises_for_a_role_which_was_not_resolved(dataset):
    # A role the requirement never had. `KeyError` rather than `None`, because a
    # missing role is a mistake in whatever asked, not an answer.
    group = solve(tas_requirement(), InMemoryCatalogue(entries=(dataset(1),))).resolved[
        ONLY_GROUP
    ]

    with pytest.raises(KeyError):
        group.one("rlut")


# -------------------------------------------------------------------------- all_of


def test_every_child_is_evaluated(variables_catalogue):
    # Nothing short-circuits: the explanation says everything which is wrong with
    # the group, rather than the first thing, because a user reading it wants to
    # know what to go and look for in one trip.
    result = solve(variables_together(), variables_catalogue(("tas",)))

    rendered = result.unsatisfied[ONLY_GROUP].explanation.render()
    assert "[satisfied] tas" in rendered
    assert "[unsatisfied] rsdt" in rendered
    assert "[unsatisfied] rlut" in rendered


def test_unsatisfied_wins_over_ambiguous(dataset):
    # A group missing a dataset cannot be run however the ambiguity is resolved,
    # so the missing dataset is the honest headline -- and the ambiguity is still
    # reported, because nothing was skipped to decide that.
    catalogue = InMemoryCatalogue(
        entries=(dataset(1, grid_label="gn"), dataset(2, grid_label="gr"))
    )

    result = solve(variables_together(("tas", "rlut")), catalogue)

    assert list(result.unsatisfied) == [ONLY_GROUP]
    assert not result.ambiguous
    rendered = result.unsatisfied[ONLY_GROUP].explanation.render()
    assert "[ambiguous] tas" in rendered
    assert "[unsatisfied] rlut" in rendered


def test_the_label_names_the_roles_below_it(dataset):
    # Which node this is, said in terms of the roles under it. Nesting does not
    # show in the name, because what a reader needs is what the node is for.
    nested = requirement(
        name="nested",
        tree=all_of(
            leaf(Query(variable="tas"), "tas"),
            all_of(leaf(Query(variable="rlut"), "rlut")),
        ),
        group_by=GROUP_BY,
    )

    rendered = solve(nested, InMemoryCatalogue(entries=(dataset(1),))).explain()

    assert "all_of(tas & rlut)" in rendered


@pytest.mark.parametrize(
    "dispatch",
    [
        pytest.param(_label, id="_label"),
        pytest.param(lambda node: _eval(node, A_CONTEXT, ""), id="_eval"),
    ],
)
def test_the_dispatchers_refuse_a_non_node(dispatch):
    # The dispatch functions in this module end in a raise rather than falling
    # through, so a node type added later without a branch here fails loudly
    # instead of being skipped. Called directly, because a `Requirement` would
    # never hold such a tree.
    with pytest.raises(
        NotANodeError,
        match=re.escape(
            "Expected a node, i.e. one of 'Leaf' and 'AllOf', got str: 'tas'. "
            "A leaf takes a query, everything else takes nodes."
        ),
    ):
        dispatch("tas")


# ------------------------------------------------------------------------ explain()


def test_explanation_is_readable(variables_catalogue):
    result = solve(variables_together(), variables_catalogue(("tas", "rsdt")))

    lines = result.explain().splitlines()

    # The title names the requirement, because the blocks name only their group:
    # two requirements solved against the same catalogue are otherwise
    # indistinguishable once the output is pasted somewhere else.
    assert lines[0] == (
        "Requirement 'variables-together', grouped by 'model' and 'variant_label':"
    )
    assert lines[2] == "[unsatisfied] model=ModelA, variant_label=r1i1p1f1"
    assert lines[3] == "  [unsatisfied] all_of(tas & rsdt & rlut)"
    assert lines[4].startswith("    [satisfied] tas: #1 ")


def test_explain_says_why_when_no_group_was_discovered():
    # An empty string would read as a bug, so the answer says why there is nothing
    # to show, and what was asked for, which is where the mistake usually is.
    rendered = solve(variables_together(), InMemoryCatalogue(entries=())).explain()

    assert rendered.startswith("No groups were discovered, so nothing was solved")
    assert "requirement 'variables-together'" in rendered
    assert "grouped by 'model' and 'variant_label'" in rendered
    assert "tas (reporting_interval=mon, variable=tas)" in rendered
    assert "rlut (reporting_interval=mon, variable=rlut)" in rendered


def test_explain_covers_every_group(dataset):
    # One block per group, whichever bucket it went into, in the same sorted order
    # the buckets themselves use.
    grouped_on_grid = requirement(
        name="per-grid",
        tree=leaf(Query(variable="tas"), "tas"),
        group_by=("model", "grid_label"),
    )
    catalogue = InMemoryCatalogue(
        entries=(
            dataset(1, grid_label="gr"),
            dataset(2, grid_label="gn", label="first-gn"),
            dataset(3, grid_label="gn", label="second-gn"),
        )
    )

    rendered = solve(grouped_on_grid, catalogue).explain()

    assert [line for line in rendered.splitlines() if line.startswith("[")] == [
        "[ambiguous] model=ModelA, grid_label=gn",
        "[satisfied] model=ModelA, grid_label=gr",
    ]


# -------------------------------------------------------------------------- errors


@pytest.mark.parametrize(
    ("build", "facet"),
    [
        pytest.param(
            lambda: tas_requirement(
                tree=leaf(Query(variable="tas", activity="CMIP"), "tas")
            ),
            "activity",
            id="leaf-query",
        ),
        pytest.param(
            lambda: tas_requirement(group_by=("model", "prodcut")),
            "prodcut",
            id="group_by",
        ),
        pytest.param(
            lambda: tas_requirement(prefer={"prodcut": ("output1",)}),
            "prodcut",
            id="prefer",
        ),
    ],
)
def test_facet_no_dataset_knows_is_an_error(build, facet, dataset):
    # Raised rather than reported as an unresolved group, wherever the facet was
    # named: it is a mistake in the requirement rather than a fact about the data,
    # and no amount of new data would make the facet answerable.
    with pytest.raises(UnrecordedFacetError, match=facet):
        solve(build(), InMemoryCatalogue(entries=(dataset(1),)))


# ------------------------------------------------------- project-specific facets


def test_project_specific_facets_come_from_the_catalogue(dataset):
    # CMIP5's `product` has no column, so the catalogue answers for it out of each
    # entry's `extra`. That is what lets a requirement match, group and prefer on
    # it, which is the whole contract between the solver and a catalogue.
    cmip5 = requirement(
        name="cmip5",
        tree=leaf(QueryCMIP5(variable="tas", product="output1"), "tas"),
        group_by=("model", "product"),
    )
    wanted = dataset(1, project="CMIP5", extra={"product": "output1"})
    other = dataset(2, project="CMIP5", extra={"product": "output2"})

    result = solve(cmip5, InMemoryCatalogue(entries=(wanted, other)))

    assert list(result.resolved) == [(("model", "ModelA"), ("product", "output1"))]
    assert result.resolved[(("model", "ModelA"), ("product", "output1"))].one(
        "tas"
    ) == (wanted)
