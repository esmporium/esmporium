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
    MixedProjectFacetError,
    NotANodeError,
    Requirement,
    UngroupableFacetError,
    UnmodelledFacetError,
    UnrankableFacetError,
    VersionFilter,
    all_of,
    asked_facets,
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


A_CONTEXT = _Context(requirement=tas_requirement(), found={}, group={})
"""A context for calling the dispatch functions directly, when it does not matter"""


@pytest.fixture
def dataset(make_entry):
    """
    Get a factory for one stored dataset, with facet values a person can read

    Takes what `make_entry` takes: the entry's `id`, optionally `project_facets`,
    `other_terms` and `label`, and any facet to override.
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


def test_candidates_which_agree_on_every_facet(dataset, reads_everything):
    # Two rows our dataset model cannot tell apart on any facet. No `prefer` and no
    # query could choose between them, so the message says so instead of advising
    # something which cannot be done. Built by hand here: ingestion refuses this
    # pair, so a database-backed catalogue cannot produce it.
    #
    # Solved against a catalogue which reads everything, because that is the premise
    # this message rests on: nothing was left unread, so the facet which tells these
    # apart really is missing rather than merely unlooked-at. The other case is
    # `test_an_ambiguity_among_lean_candidates_says_what_was_read`.
    catalogue = InMemoryCatalogue(
        entries=(dataset(1, label="one"), dataset(2, label="two"))
    )

    result = solve(tas_requirement(), reads_everything(catalogue))

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


def test_differing_facets_only_names_what_every_candidate_knows(
    dataset, reads_everything
):
    # `realm` is in one candidate's `project_facets` and not the other's, so the two
    # would read as differing on it when really one of them just does not record it.
    # Only the facets every candidate knows are compared, so that the message names
    # something `prefer` could actually act on.
    #
    # Against a catalogue which reads everything, because this is about which of the
    # facets in hand get named, and `tas_requirement` names neither of these two -- a
    # solve which was told what to read would have neither in hand to choose between.
    catalogue = InMemoryCatalogue(
        entries=(
            dataset(1, project_facets={"product": "output1", "realm": "atmos"}),
            dataset(2, project_facets={"product": "output2"}),
        )
    )

    rendered = solve(tas_requirement(), reads_everything(catalogue)).explain()

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


@pytest.mark.parametrize("field_name", ["group_by", "prefer"])
def test_a_facet_we_do_not_model_is_refused_when_the_requirement_is_built(field_name):
    # A typo needs no catalogue to spot, so it is refused where it was written rather
    # than carried into somebody else's solve. A fragment plus the attributes, not the
    # whole message: `test_tree.py` owns the wording.
    named = {"group_by": ("model", "prodcut"), "prefer": {"prodcut": ("output1",)}}

    with pytest.raises(UnmodelledFacetError, match="prodcut") as excinfo:
        tas_requirement(**{field_name: named[field_name]})

    assert excinfo.value.facets == ("prodcut",)
    assert excinfo.value.field_name == field_name


def test_a_modelled_facet_nothing_records_cannot_be_grouped_on(dataset):
    # `activity` is a facet we model and store only in the raw search documents, so a
    # catalogue whose documents never carried it cannot be grouped on it. No group can
    # be formed, so this raises rather than reporting a group of `None`.
    with pytest.raises(UngroupableFacetError, match="activity") as excinfo:
        solve(
            tas_requirement(group_by=("model", "activity")),
            InMemoryCatalogue(entries=(dataset(1),)),
        )

    assert excinfo.value.facets == ("activity",)
    assert excinfo.value.candidates == 1


def test_a_modelled_facet_nothing_records_cannot_break_a_tie(dataset):
    # `prefer` is only read when there is a tie, so this is raised lazily: two
    # candidates and nothing able to answer for the facet meant to separate them.
    with pytest.raises(UnrankableFacetError, match="activity") as excinfo:
        solve(
            tas_requirement(prefer={"activity": ("CMIP",)}),
            InMemoryCatalogue(
                entries=(dataset(1, grid_label="gn"), dataset(2, grid_label="gr"))
            ),
        )

    assert excinfo.value.facets == ("activity",)
    assert excinfo.value.candidates == 2


def test_a_facet_nothing_records_in_a_leaf_query_finds_nothing(dataset):
    # Not an error, unlike the two above, and the asymmetry is the point. A query
    # naming a facet nothing records is a question whose answer is "nothing", which is
    # a fact about the data; a grouping on one cannot produce groups at all.
    asked = tas_requirement(tree=leaf(Query(variable="tas", activity="CMIP"), "tas"))

    result = solve(asked, InMemoryCatalogue(entries=(dataset(1),)))

    assert not result.satisfied
    assert not result.unsatisfied
    assert not result.ambiguous


# ------------------------------------- project-specific facets across projects


def test_a_project_specific_group_facet_across_projects_stops(dataset):
    # `product` is CMIP5's, so over a catalogue holding CMIP5 and CMIP6 every CMIP6
    # dataset would land in one group keyed `product=None`. That is either exactly
    # what was meant or a silent accident, so we stop rather than guess.
    mixed = InMemoryCatalogue(
        entries=(
            dataset(1, project="CMIP5", project_facets={"product": "output1"}),
            dataset(2, project="CMIP6", model="ModelB"),
        )
    )

    with pytest.raises(MixedProjectFacetError, match="product") as excinfo:
        solve(tas_requirement(group_by=("product",)), mixed)

    # A `NotImplementedError`, because the requirement is a fair thing to want and the
    # answer is "not yet" rather than "no".
    assert isinstance(excinfo.value, NotImplementedError)
    assert excinfo.value.facets == ("product",)
    assert (excinfo.value.knowing, excinfo.value.total) == (1, 2)
    assert excinfo.value.field_name == "group_by"


def test_grouping_by_project_too_allows_a_project_specific_facet(dataset):
    # The way through, and the reason it works: each group then holds one project, so
    # the facet is answerable throughout it and the `None` group is the group for the
    # projects which do not have the facet rather than an accident.
    mixed = InMemoryCatalogue(
        entries=(
            dataset(1, project="CMIP5", project_facets={"product": "output1"}),
            dataset(2, project="CMIP6", model="ModelB"),
        )
    )

    result = solve(tas_requirement(group_by=("project", "product")), mixed)

    assert sorted(result.satisfied) == [
        (("project", "CMIP5"), ("product", "output1")),
        (("project", "CMIP6"), ("product", None)),
    ]


def test_a_project_specific_facet_every_candidate_knows_is_allowed(dataset):
    """
    Two projects which both declare the facet group fine

    The case a "do the datasets span more than one project?" test would wrongly refuse:
    two projects can share a query class, and then they declare the same
    project-specific facets. CMIP6 and a CMIP6Plus mapped to `QueryCMIP6` -- which is
    what searching CMIP6Plus takes -- both answer for `sub_experiment_id`, so there is
    nothing undecided. What matters is what the candidates know, not where they came
    from.
    """
    both = InMemoryCatalogue(
        entries=(
            dataset(1, project="CMIP6", project_facets={"sub_experiment_id": "none"}),
            dataset(
                2,
                project="CMIP6Plus",
                model="ModelB",
                project_facets={"sub_experiment_id": "none"},
            ),
        )
    )

    result = solve(tas_requirement(group_by=("sub_experiment_id",)), both)

    # One group, which is then ambiguous because two datasets fill the one role --
    # an ordinary outcome, and the point is that it was reached rather than refused.
    assert list(result.ambiguous) == [(("sub_experiment_id", "none"),)]


def test_a_project_specific_prefer_facet_across_projects_stops(dataset):
    # Worse here than for grouping, which is why `prefer` gets the same check. An
    # unlisted value ranks behind every listed one, so a `product` preference would
    # rank every CMIP6 dataset last purely for not having `product` -- quietly turning
    # `prefer={"product": ...}` into "prefer CMIP5", with nothing in the result to show
    # it. Lazy like the rest of `prefer`: only reached because there is a tie.
    tie = InMemoryCatalogue(
        entries=(
            dataset(1, project="CMIP6"),
            dataset(2, project="CMIP5", project_facets={"product": "output2"}),
        )
    )

    with pytest.raises(MixedProjectFacetError, match="product") as excinfo:
        solve(tas_requirement(prefer={"product": ("output2",)}), tie)

    assert excinfo.value.field_name == "prefer"


def test_partial_coverage_of_a_canonical_facet_is_still_allowed(dataset):
    # The restriction to *project-specific* facets, guarded. `realm` is canonical and
    # has no column, so partial coverage is ordinary -- two mirrors of one project
    # return different fields, and a multi-valued facet is recorded as nothing at all.
    # One `None` group is the honest answer there, so this must not be refused.
    partial = InMemoryCatalogue(
        entries=(
            dataset(1, project_facets={"realm": "atmos"}),
            dataset(2, model="ModelB"),
        )
    )

    result = solve(tas_requirement(group_by=("realm",)), partial)

    assert sorted(result.satisfied, key=lambda key: [v or "" for _, v in key]) == [
        (("realm", None),),
        (("realm", "atmos"),),
    ]


def test_an_unmodelled_group_facet_nothing_records_is_one_group(dataset):
    # The escape hatch is never checked, which is what being an escape hatch means. So
    # unlike `group_by`, a name here which nothing records does not raise: every
    # dataset answers `None` and they all land in the one group.
    asked = tas_requirement(
        group_by=("model",), group_by_other_terms=("cordex-cmip6:driving_source_id",)
    )

    result = solve(asked, InMemoryCatalogue(entries=(dataset(1),)))

    assert list(result.satisfied) == [
        (("model", "ModelA"), ("cordex-cmip6:driving_source_id", None))
    ]


# ------------------------------------------------------- project-specific facets


def test_project_specific_facets_come_from_the_catalogue(dataset):
    # CMIP5's `product` has no column, so the catalogue answers for it out of each
    # entry's `project_facets`. That is what lets a requirement match, group and
    # prefer on it, which is the whole contract between the solver and a catalogue.
    cmip5 = requirement(
        name="cmip5",
        tree=leaf(QueryCMIP5(variable="tas", product="output1"), "tas"),
        group_by=("model", "product"),
    )
    wanted = dataset(1, project="CMIP5", project_facets={"product": "output1"})
    other = dataset(2, project="CMIP5", project_facets={"product": "output2"})

    result = solve(cmip5, InMemoryCatalogue(entries=(wanted, other)))

    assert list(result.satisfied) == [(("model", "ModelA"), ("product", "output1"))]
    chosen = result.satisfied[(("model", "ModelA"), ("product", "output1"))].one("tas")
    assert chosen.id == wanted.id
    # And the facet itself came through, which is the half that matters here: the
    # requirement names `product`, so a solve loads it.
    assert chosen.facet("product") == "output1"


# ---------------------------------------------- asking the catalogue, and what it reads


class SpyCatalogue:
    """
    A catalogue which records every lookup and then answers like any other

    The only call-recording double in these tests, and it exists because what is
    worth asserting here is not the answer but how often it was asked for.
    """

    def __init__(self, catalogue):
        self.catalogue = catalogue
        self.calls = []

    def find(self, query, *, versions=VersionFilter.NOT_RETRACTED, needs_facets=None):
        """Find every dataset which matches a query, recording what was asked"""
        asked = asked_facets(query)
        self.calls.append(
            (
                tuple(sorted(asked.modelled.items())),
                tuple(sorted(asked.literal.items())),
                versions,
                None if needs_facets is None else frozenset(needs_facets),
            )
        )

        return self.catalogue.find(query, versions=versions, needs_facets=needs_facets)


def test_each_leaf_is_looked_up_once_however_many_groups_there_are(dataset):
    """
    A solve asks one lookup per leaf, not one per leaf per group

    A leaf's candidates do not depend on the group, so the lookups which used to be
    repeated once per group were the same question every time. Four models here, so
    the old shape would have been two leaves times five.
    """
    spy = SpyCatalogue(
        InMemoryCatalogue(
            entries=tuple(
                dataset(
                    index + 1,
                    model=model,
                    variable=variable,
                    label=f"{model}-{variable}",
                )
                for index, (model, variable) in enumerate(
                    (model, variable)
                    for model in ("A", "B", "C", "D")
                    for variable in ("tas", "rlut")
                )
            )
        )
    )
    req = requirement(
        name="pair",
        tree=all_of(
            leaf(Query(variable="tas"), "tas"), leaf(Query(variable="rlut"), "rlut")
        ),
        group_by=("model",),
    )

    result = solve(req, spy)

    assert len(result.satisfied) == 4
    assert len(spy.calls) == 2


def test_two_leaves_asking_the_same_question_share_one_lookup(dataset):
    """One question, one lookup, however many roles it fills"""
    spy = SpyCatalogue(InMemoryCatalogue(entries=(dataset(1),)))
    req = requirement(
        name="twice",
        tree=all_of(
            leaf(Query(variable="tas"), "first"), leaf(Query(variable="tas"), "second")
        ),
        group_by=GROUP_BY,
    )

    solve(req, spy)

    assert len(spy.calls) == 1


def test_the_same_facet_in_other_terms_is_a_different_question(dataset):
    """
    A facet we model and the same name in `other_terms` do not share a lookup

    They are different questions -- one is matched against the facets we model, the
    other against a document's own spelling -- so sharing an answer would be wrong
    rather than merely surprising.
    """
    spy = SpyCatalogue(InMemoryCatalogue(entries=(dataset(1),)))
    req = requirement(
        name="both-spellings",
        tree=all_of(
            leaf(Query(variable="tas"), "modelled"),
            leaf(Query(other_terms={"variable": ("tas",)}), "literal"),
        ),
        group_by=GROUP_BY,
    )

    solve(req, spy)

    assert len(spy.calls) == 2


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        pytest.param({}, set(), id="nothing-extra"),
        # Added to the usual grouping rather than replacing it, so that one baseline
        # holds for every case here.
        pytest.param(
            {"group_by": (*GROUP_BY, "experiment")}, {"experiment"}, id="group_by"
        ),
        pytest.param(
            {"group_by_other_terms": ("cordex-cmip6:driving_source_id",)},
            {"cordex-cmip6:driving_source_id"},
            id="group_by_other_terms",
        ),
        pytest.param({"prefer": {"grid_label": ("gn",)}}, {"grid_label"}, id="prefer"),
        pytest.param(
            {"prefer_other_terms": {"mip_era": ("CMIP6",)}},
            {"mip_era"},
            id="prefer_other_terms",
        ),
        pytest.param(
            {"versions": VersionFilter.ANY}, set(), id="versions-names-no-facet"
        ),
    ],
)
def test_a_solve_asks_for_every_facet_the_requirement_names(
    dataset, overrides, expected
):
    """
    Each place a requirement can name a facet reaches the lookup

    The baseline is what `tas_requirement` already names -- its leaf's `variable` and
    its `group_by` -- and each case adds one facet in one place. `versions` adds
    nothing, which is the one entry here that is an assertion about something *not*
    happening.
    """
    spy = SpyCatalogue(InMemoryCatalogue(entries=(dataset(1),)))
    baseline = {"variable", *GROUP_BY}

    solve(tas_requirement(**overrides), spy)

    (call,) = spy.calls
    assert call[-1] == frozenset(baseline | expected)


def test_a_solve_gives_the_same_answer_with_the_documents_read(
    dataset, reads_everything
):
    """
    Naming what will be read does not change what a solve decides

    The in-memory twin of
    `test_skipping_the_documents_does_not_change_a_solve` in
    `tests/unit/db/test_database_catalogue.py`, and the cheaper of the two: the
    in-memory catalogue honours `needs_facets`, so the two shapes can be compared with
    no database at all.
    """
    catalogue = InMemoryCatalogue(
        entries=(
            dataset(1, grid_label="gn", project_facets={"realm": "atmos"}),
            dataset(2, grid_label="gr", project_facets={"realm": "ocean"}),
        )
    )
    req = tas_requirement(prefer={"grid_label": ("gn", "gr")})

    narrowed = solve(req, catalogue)
    everything = solve(req, reads_everything(catalogue))

    assert set(narrowed.satisfied) == set(everything.satisfied)
    assert narrowed.satisfied[ONLY_GROUP].one("tas").id == (
        everything.satisfied[ONLY_GROUP].one("tas").id
    )
    assert narrowed.explain() == everything.explain()
    # Not vacuous: the two solves really did read different amounts.
    assert narrowed.satisfied[ONLY_GROUP].one("tas").facets_loaded is not None
    assert everything.satisfied[ONLY_GROUP].one("tas").facets_loaded is None


def test_an_ambiguity_among_lean_candidates_says_what_was_read(dataset):
    """
    A tie nothing in hand can break says what was read rather than blaming us

    The candidates differ only in a facet no column holds and the requirement names
    none, so it was never loaded. The honest report is that nothing was looked at
    which could tell them apart, not the "this should not happen" of
    `test_candidates_which_agree_on_every_facet`, which rests on everything having
    been read.
    """
    catalogue = InMemoryCatalogue(
        entries=(
            dataset(1, label="one", project_facets={"realm": "atmos"}),
            dataset(2, label="two", project_facets={"realm": "ocean"}),
        )
    )

    result = solve(tas_requirement(), catalogue)

    (explanation,) = result.ambiguous[ONLY_GROUP].explanation.parts
    assert "agree on every facet this solve read" in explanation.message
    assert "may exist and simply not have been looked at" in explanation.message
    assert "`cardinality=Cardinality.ALL`" in explanation.message
    # The facets it did read are named, so the reader can see what to add to.
    assert "'variant_label'" in explanation.message
    # And it does not send them to the issue tracker for an optimisation working.
    assert "should not happen" not in explanation.message
    assert "raise an issue" not in explanation.message
