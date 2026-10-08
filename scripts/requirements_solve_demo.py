"""
A runnable example of the solver: a requirement + a catalogue -> a verdict per group

Sits beside the search demos (`search_cmipx.py`, `check_query_values_demo.py`) and
answers a different question. Those ask "what exists that matches these facets?". This
asks "do I have everything one analysis needs, *together*, and for which models?".

Six scenes, each a use case the solver is built for:

1. everything is there, for both models
2. one leaf, several variables, `variable` in `group_by` -- one group per variable
3. a model missing one dataset: unsatisfied, and the explanation names what is missing
4. two datasets fit one role and nothing chooses between them: ambiguous...
5. ...and the same requirement with `prefer` set, which settles it...
6. ...or with `cardinality=Cardinality.ALL`, which keeps every candidate instead

Then two short epilogues:
reading the datasets back out of a solution,
and one requirement written in two query styles giving one hash.

Needs no network and no database: the catalogue here is
[InMemoryCatalogue][esmporium.requirements.InMemoryCatalogue], which is what we
use for demos like this so we don't need a full database to be spun up.
Everything else -- the requirement, the solving, the
explanations -- is exactly what a real catalogue would get.

To solve against real data instead, search for the requirement and then hand `solve`
an [esmporium.db.DatabaseCatalogue][] over the same database;
`scripts/search_multi_project_demo.py` shows the searching half.
"""

from __future__ import annotations

from esmporium.query import Query, QueryCMIP6
from esmporium.requirements import (
    Cardinality,
    CatalogueEntry,
    InMemoryCatalogue,
    SolveResult,
    all_of,
    leaf,
    requirement,
    solve,
)


def catalogue_entry(
    entry_id: int,
    model: str,
    variable: str,
    experiment: str = "1pctCO2",
    grid_label: str = "gn",
) -> CatalogueEntry:
    """Get one stored dataset, as the solver sees it."""
    return CatalogueEntry(
        id=entry_id,
        id_project_specific=f"CMIP6.{model}.{experiment}.{variable}.{grid_label}",
        project="CMIP6",
        model=model,
        institution="ExampleInstitute",
        experiment=experiment,
        variant_label="r1i1p1f1",
        variable=variable,
        reporting_interval="mon",
        grid_label=grid_label,
        processing_id="Amon",
    )


def show(title: str, result: SolveResult) -> None:
    """Print one scene: the headline counts, then the solver's own explanation."""
    print("=" * 78)
    print(title)
    print("=" * 78)
    print(
        f"satisfied: {len(result.satisfied)}   "
        f"unsatisfied: {len(result.unsatisfied)}   "
        f"ambiguous: {len(result.ambiguous)}"
    )
    print()
    # The explanation is the point of the solver, not a debugging aid: a greedy
    # solver which does not backtrack has to say *why*, so a user can tell whether
    # to widen a query, set `prefer`, or accept the loss.
    print(result.explain())
    print()


# ------------------------------------------------------------------ 1. all satisfied

# Three datasets needed *together*, to regress one against the others, so three
# leaves. Roles named after their variables here only because that is the clearest
# thing to call them when each leaf really is a different variable.
GREGORY = requirement(
    name="gregory-regression",
    tree=all_of(
        leaf(Query(variable="tas"), "tas"),
        leaf(Query(variable="rsdt"), "rsdt"),
        leaf(Query(variable="rlut"), "rlut"),
    ),
    group_by=("model", "variant_label"),
    where=Query(reporting_interval="mon"),
)

BOTH_MODELS_COMPLETE = InMemoryCatalogue(
    entries=tuple(
        catalogue_entry(index, model, variable)
        for index, (model, variable) in enumerate(
            (model, variable)
            for model in ("CanESM5", "ACCESS-CM2")
            for variable in ("tas", "rsdt", "rlut")
        )
    )
)


# ----------------------------------------------------------- 2. one group per variable

# The other shape entirely. This analysis works on *a* field and repeats itself for
# each one, so it needs one dataset at a time, not four together: one leaf offering
# four variables, a role saying what the dataset is *for*, and `variable` in
# `group_by` to split the datasets into groups.
PER_FIELD = requirement(
    name="per-field-analysis",
    tree=all_of(leaf(Query(variable=("tas", "pr", "rsdt", "rlut")), "field")),
    group_by=("model", "variable"),
    where=Query(reporting_interval="mon"),
)


# ------------------------------------------------------------ 3. one gap, one model

MISSING_RLUT_FOR_ACCESS = InMemoryCatalogue(
    entries=tuple(
        stored
        for stored in BOTH_MODELS_COMPLETE.entries
        if not (stored.model == "ACCESS-CM2" and stored.variable == "rlut")
    )
)


# -------------------------------------------------- 4 and 5. a tie, and breaking it

# The same data published on two grids. Nothing in the requirement says which to take,
# so the role has two candidates and the solver refuses to guess.
TWO_GRIDS = InMemoryCatalogue(
    entries=(
        *BOTH_MODELS_COMPLETE.entries,
        *(
            catalogue_entry(100 + index, "CanESM5", variable, grid_label="gr")
            for index, variable in enumerate(("tas", "rsdt", "rlut"))
        ),
    )
)

PREFERS_NATIVE_GRID = requirement(
    name="gregory-regression",
    tree=GREGORY.tree,
    group_by=("model", "variant_label"),
    where=Query(reporting_interval="mon"),
    # Order within a facet matters: the first value present wins.
    prefer={"grid_label": ("gn", "gr")},
)

KEEPS_EVERY_GRID = requirement(
    name="gregory-regression",
    tree=GREGORY.tree,
    group_by=("model", "variant_label"),
    where=Query(reporting_interval="mon"),
    # The other half of the same rule: `prefer` ranks, and `cardinality` says what to
    # do with whatever survives the ranking. Nothing ranks `grid_label` here, so with
    # `Cardinality.ALL` both grids are kept rather than reported as a tie.
    cardinality=Cardinality.ALL,
)


def main() -> None:
    """Run each scene in turn."""
    show(
        "1. Everything found, for both models",
        solve(GREGORY, BOTH_MODELS_COMPLETE),
    )

    # The groups are never listed up front -- they are discovered from what the
    # catalogue holds. Two models x four variables, minus the pairs nobody published.
    show(
        "2. One leaf, four variables, `variable` in group_by -> one group per variable",
        solve(PER_FIELD, BOTH_MODELS_COMPLETE),
    )

    show(
        "3. ACCESS-CM2 is missing rlut: that group alone is unsatisfied",
        solve(GREGORY, MISSING_RLUT_FOR_ACCESS),
    )

    show(
        "4. CanESM5 published on two grids, and nothing says which: ambiguous",
        solve(GREGORY, TWO_GRIDS),
    )

    show(
        "5. The same data, with `prefer={'grid_label': ('gn', 'gr')}`: satisfied",
        solve(PREFERS_NATIVE_GRID, TWO_GRIDS),
    )

    # Ambiguity is a question nobody answered, so there are two ways to answer it:
    # rank the candidates, or say that all of them are wanted.
    show(
        "6. The same tie, with `cardinality=Cardinality.ALL`: both grids are kept",
        solve(KEEPS_EVERY_GRID, TWO_GRIDS),
    )

    # What a solution is actually *for*: the datasets to do the analysis with,
    # reachable by the role names the requirement gave them.
    print("=" * 78)
    print("Reading the answer back out")
    print("=" * 78)
    result = solve(PREFERS_NATIVE_GRID, TWO_GRIDS)
    for key, solution in sorted(result.satisfied.items()):
        model = dict(key)["model"]
        print(f"{model}:")
        for role in sorted(solution.roles):
            print(f"    {role:6} -> {solution.one(role).id_project_specific}")
    print()

    # A requirement may be written in any query style; it is translated as it is
    # stored, so the same question asked two ways is one requirement.
    in_cmip6_names = requirement(
        name="same",
        tree=all_of(leaf(QueryCMIP6(variable_id="tas"), "tas")),
        group_by=("model", "variant_label"),
    )
    in_our_names = requirement(
        name="same",
        tree=all_of(leaf(Query(variable="tas", project="CMIP6"), "tas")),
        group_by=("model", "variant_label"),
    )
    print("=" * 78)
    print("One question, two query styles, one hash")
    print("=" * 78)
    written_in_cmip6 = in_cmip6_names.requirement_hash()
    written_in_ours = in_our_names.requirement_hash()
    print(f"QueryCMIP6(variable_id='tas') -> {written_in_cmip6[:16]}...")
    print(f"Query(variable='tas', ...)    -> {written_in_ours[:16]}...")
    print(f"same requirement: {written_in_cmip6 == written_in_ours}")


if __name__ == "__main__":
    main()
