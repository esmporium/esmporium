"""
Tests of the solver: filling a requirement's roles from a catalogue, group by group
"""

from __future__ import annotations

import pytest

from esmporium.query import Query, QueryCMIP5
from esmporium.requirements import (
    Cardinality,
    ExplanationStatusNotOk,
    ExplanationStatusOk,
    InMemoryCatalogue,
    NotANodeError,
    Requirement,
    UnrecordedFacetError,
    all_of,
    leaf,
    requirement,
    solve,
)
from esmporium.requirements.solve import _Context, _eval_tree, _label

GROUP_BY = ("model", "variant_label")
"""What these tests use to group by, unless grouping is the point"""

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
Values for the facets these tests talk about, facet names in canonical language."""

ONLY_GROUP = tuple((facet, READABLE_FACETS[facet]) for facet in GROUP_BY)
"""The group every dataset below falls in, unless a test deliberately says otherwise"""

VARIABLES_TOGETHER = ("tas", "rsdt", "rlut")
"""
Variables an analysis needs at once, i.e. one leaf each under one `all_of`
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


def variables_together(variables=VARIABLES_TOGETHER, **overrides) -> Requirement:
    """
    Get a requirement whose leaves are all needed together, one per variable

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
    """

    def factory(variables=VARIABLES_TOGETHER, **facets):
        return InMemoryCatalogue(
            entries=tuple(
                dataset(index, variable=variable, **facets)
                for index, variable in enumerate(variables, start=1)
            )
        )

    return factory


# ------------------------------------------------------------------------- satisfied


def test_all_satisfied(variables_catalogue):
    result = solve(variables_together(), variables_catalogue())

    assert list(result.satisfied) == [ONLY_GROUP]
    assert not (result.unsatisfied or result.ambiguous)

    group = result.satisfied[ONLY_GROUP]
    assert {role: group.one(role).variable for role in group.roles} == {
        "tas": "tas",
        "rsdt": "rsdt",
        "rlut": "rlut",
    }
    assert group.explanation.status is ExplanationStatusOk.SATISFIED


def test_required_variable_missing(variables_catalogue):
    result = solve(variables_together(), variables_catalogue(("tas", "rsdt")))

    assert not result.satisfied

    (tree_explanation,) = result.unsatisfied[ONLY_GROUP].explanation.parts
    parts = {part.subject: part for part in tree_explanation.parts}
    assert parts["rlut"].status is ExplanationStatusNotOk.UNSATISFIED

    # Where this message is pinned in full. The group is named on the leaf's own
    # line as well as on the block above it, because the group is filtered on the
    # entry rather than in the query: without it this line reads as "there is no
    # `rlut` at all", which is a different and here untrue statement.
    assert parts["rlut"].message == (
        "no dataset matches reporting_interval=mon, variable=rlut "
        "for model=ModelA, variant_label=r1i1p1f1"
    )


def test_a_role_always_holds_a_tuple(variables_catalogue):
    # The promise `Solution.roles` makes: one shape to handle, whatever the
    # cardinality, so that a reader never has to ask which it got.
    group = solve(variables_together(), variables_catalogue()).satisfied[ONLY_GROUP]

    assert all(isinstance(entries, tuple) for entries in group.roles.values())


def test_the_result_carries_the_requirement_it_solved(variables_catalogue):
    # What makes "has this become satisfiable?" a comparison of two results: the
    # hashes agreeing is how a reader knows both solves asked the same question.
    asked = variables_together()

    result = solve(asked, variables_catalogue())

    assert result.requirement.requirement_hash() == asked.requirement_hash()


# ------------------------------------------------------- groups: discovery and order


def test_group_by_variable_fans_out(dataset):
    # One leaf offering four variables, with `variable` grouped on: one group per
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

    assert [dict(key)["variable"] for key in result.satisfied] == ["pr", "rsdt", "tas"]
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

    assert [dict(key)["experiment"] for key in result.satisfied] == [
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

    assert [dict(key)["experiment"] for key in result.satisfied] == ["historical"]
    assert [dict(key)["experiment"] for key in result.unsatisfied] == ["ssp126"]


def test_groups_come_out_in_a_stable_order(dataset):
    # Sorted, so that solving twice produces the same explanation and a
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

    assert [dict(key)["grid_label"] for key in result.satisfied] == [None, "gn", "gr"]
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

    buckets = (result.satisfied, result.unsatisfied, result.ambiguous)
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

    (explanation,) = ambiguous.ambiguous[ONLY_GROUP].explanation.parts
    assert (explanation.subject, explanation.status) == (
        "tas",
        ExplanationStatusNotOk.AMBIGUOUS,
    )

    # Where this message is pinned in full. All three ways out are offered,
    # because which one is right is the user's judgement and not something the
    # solver can work out. The entries are interpolated rather than written out,
    # so the pin is on the wording and not on `make_entry`'s labels.
    first, second = catalogue.entries
    assert explanation.message == (
        "2 candidates, differing in 'grid_label': "
        f"#{first.id} ({first.id_project_specific!r}) with grid_label='gn', "
        f"#{second.id} ({second.id_project_specific!r}) with grid_label='gr'. "
        "Nothing was given to choose between them: set `prefer` on the "
        "requirement (e.g. `prefer={'grid_label': (...)}`), narrow the query, or "
        "use `cardinality=Cardinality.ALL` to keep them all."
    )

    assert preferred.satisfied[ONLY_GROUP].one("tas").grid_label == "gn"


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

    # Where this message is pinned in full.
    (explanation,) = result.ambiguous[ONLY_GROUP].explanation.parts
    first, second = catalogue.entries
    assert explanation.message == (
        "2 candidates, differing in 'institution': "
        f"#{first.id} ({first.id_project_specific!r}) with institution='InstA', "
        f"#{second.id} ({second.id_project_specific!r}) with institution='InstB'. "
        "Preferring grid_label in the order 'gn' and 'gr' did not narrow this to "
        "one, since the candidates left over are equally preferred: add the facet "
        "they differ on to `prefer`, or narrow the query."
    )


def test_candidates_which_agree_on_every_facet(dataset):
    # Two rows our dataset model cannot tell apart on any facet. No `prefer` and no
    # query could choose between them, so the message says so instead of advising
    # something which cannot be done. Built by hand here: ingestion refuses this
    # pair, so a database-backed catalogue cannot produce it.
    catalogue = InMemoryCatalogue(
        entries=(dataset(1, label="one"), dataset(2, label="two"))
    )

    result = solve(tas_requirement(), catalogue)

    # Where this message is pinned in full. It points at the issue tracker rather
    # than at the user, because the facet which tells these apart exists and did not
    # reach the solver, and it does not advise picking one by ID, which is advice
    # `CatalogueEntry.facet` could not answer and so could not be followed.
    (explanation,) = result.ambiguous[ONLY_GROUP].explanation.parts
    first, second = catalogue.entries
    assert explanation.message == (
        "2 candidates which agree on every facet, so no facet can choose between "
        f"them: #{first.id} ({first.id_project_specific!r}), "
        f"#{second.id} ({second.id_project_specific!r}). They differ only in "
        "their project-specific ID, which cannot be used to break a tie."
        "This should not happen: datasets agreeing on every column are stored "
        "separately only when their project-specific IDs differ, so the facet which "
        "tells these apart exists but has not reached the solver. Please raise an "
        "issue at https://github.com/esmporium/esmporium/issues quoting the message "
        "above. `cardinality=Cardinality.ALL` keeps them all in the meantime."
    )


def test_a_value_not_in_prefer_ranks_behind_one_that_is(dataset):
    # `prefer` says which is better, not which is allowed, so an unlisted value is
    # ranked last rather than dropped -- and it still wins when nothing beats it.
    catalogue = InMemoryCatalogue(
        entries=(dataset(1, grid_label="gn"), dataset(2, grid_label="gr"))
    )

    listed_second = solve(tas_requirement(prefer={"grid_label": ("gr",)}), catalogue)
    nothing_listed = solve(tas_requirement(prefer={"grid_label": ("gm",)}), catalogue)

    assert listed_second.satisfied[ONLY_GROUP].one("tas").grid_label == "gr"
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

    assert result.satisfied[ONLY_GROUP].one("tas").id == 2


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


# ------------------------------------------------------- cardinality=Cardinality.ALL


def test_cardinality_all_keeps_every_candidate(dataset):
    # The same tie as `test_ambiguous_grids_and_prefer`, with the requirement
    # saying it is not a tie: the analysis wants the spread across candidates.
    catalogue = InMemoryCatalogue(
        entries=(dataset(1, grid_label="gn"), dataset(2, grid_label="gr"))
    )

    result = solve(tas_requirement(cardinality=Cardinality.ALL), catalogue)

    assert not result.ambiguous
    # In the order the catalogue gave them, so the result is reproducible.
    assert [entry.id for entry in result.satisfied[ONLY_GROUP].roles["tas"]] == [1, 2]


def test_one_refuses_a_role_holding_several(dataset):
    catalogue = InMemoryCatalogue(
        entries=(dataset(1, grid_label="gn"), dataset(2, grid_label="gr"))
    )
    solved = solve(tas_requirement(cardinality=Cardinality.ALL), catalogue)
    group = solved.satisfied[ONLY_GROUP]

    # The whole message, pinned once, here: `solve.py` raises it and nothing else
    # tests it, so this is the one place it is written out. `match=` is
    # `re.search`, so it could not notice text added to either end; this can.
    with pytest.raises(ValueError) as excinfo:
        group.one("tas")

    assert str(excinfo.value) == (
        "'tas' holds 2 datasets, so there is no single one to return. "
        "Read `roles` directly (solving with `cardinality=Cardinality.ONE` may also "
        "fix this)."
    )


def test_one_raises_for_an_unknown_role(dataset):
    # A role the requirement never had. `KeyError` rather than `None`, because a
    # missing role is a mistake in whatever asked, not an answer.
    result = solve(tas_requirement(), InMemoryCatalogue(entries=(dataset(1),)))
    solution = result.satisfied[ONLY_GROUP]

    with pytest.raises(KeyError, match="junk_key"):
        solution.one("junk_key")


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
    # A group missing a dataset cannot be satisfied however the ambiguity is settled,
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

    entry = dataset(1)

    rendered = solve(nested, InMemoryCatalogue(entries=(entry,))).explain()

    # The whole message, so the label can be read where a reader meets it. The
    # inner `all_of(rlut)` is indented beneath the outer `all_of(tas & rlut)`, so
    # the nesting shows in the shape of the block while neither label spells it
    # out. The entry is interpolated, so the pin is on the wording rather than on
    # `make_entry`'s labels.
    assert rendered == (
        "Requirement 'nested', grouped by 'model' and 'variant_label':\n"
        "\n"
        "[unsatisfied] model=ModelA, variant_label=r1i1p1f1\n"
        "  [unsatisfied] all_of(tas & rlut)\n"
        f"    [satisfied] tas: #{entry.id} ({entry.id_project_specific!r})\n"
        "    [unsatisfied] all_of(rlut)\n"
        "      [unsatisfied] rlut: no dataset matches variable=rlut "
        "for model=ModelA, variant_label=r1i1p1f1"
    )


@pytest.mark.parametrize(
    "dispatch",
    [
        pytest.param(_label, id="_label"),
        pytest.param(lambda node: _eval_tree(node, A_CONTEXT, ""), id="_eval"),
    ],
)
def test_the_dispatchers_refuse_a_non_node(dispatch):
    # The dispatch functions in this module end in a raise rather than falling
    # through, so a node type added later without a branch here fails loudly
    # instead of being skipped. Called directly, because a `Requirement` would
    # never hold such a tree.
    # A fragment, not the whole message: the error belongs to `tree.py` and
    # `test_tree.py` spells it out. What is tested here is that this module's
    # dispatchers raise it too.
    with pytest.raises(NotANodeError, match="Expected a node"):
        dispatch("some string")


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

    # Where this message is pinned in full: it is the whole of `explain()` in this
    # case, so there is no group block for the wording to sit beside.
    assert rendered == (
        "No groups were discovered, so nothing was solved: no dataset in the "
        "catalogue matched any leaf of requirement 'variables-together'. A group "
        "is discovered from the candidates the leaves find, grouped by 'model' "
        "and 'variant_label', so no candidates means no groups. The leaves asked "
        "for: tas (reporting_interval=mon, variable=tas); "
        "rsdt (reporting_interval=mon, variable=rsdt); "
        "rlut (reporting_interval=mon, variable=rlut)."
    )


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
    # Raised rather than reported as an unsolved group, wherever the facet was
    # named: it is a mistake in the requirement rather than a fact about the data,
    # and no amount of new data would make the facet answerable.
    # A fragment plus the attribute, not the whole message: `test_catalogue.py`
    # owns the wording, and `facets` is what a caller would catch and read.
    with pytest.raises(UnrecordedFacetError, match=facet) as excinfo:
        solve(build(), InMemoryCatalogue(entries=(dataset(1),)))

    assert excinfo.value.facets == (facet,)


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

    assert list(result.satisfied) == [(("model", "ModelA"), ("product", "output1"))]
    assert result.satisfied[(("model", "ModelA"), ("product", "output1"))].one(
        "tas"
    ) == (wanted)
