"""
Tests of the constraint protocol: judgements about the datasets a requirement selected

Every constraint in here is defined in this file. That is the point of the module
rather than a convenience: the claim the protocol makes is that the solver runs whatever
it is handed without knowing what it is, and a test which used a constraint esmporium
ships could not tell that apart from the solver knowing about that one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import pytest
from pydantic import BaseModel, ConfigDict
from pydantic_core import PydanticSerializationError

import esmporium.requirements
from esmporium.query import Query
from esmporium.requirements import (
    Ancestors,
    Catalogue,
    CatalogueEntry,
    ConstraintView,
    Degraded,
    ExplanationStatusOk,
    Fail,
    InMemoryCatalogue,
    NotAConstraintError,
    NotSerialisableConstraintError,
    Outcome,
    Pass,
    Requirement,
    UnresolvableConstraintError,
    all_of,
    check_constraint,
    leaf,
    requirement,
    solve,
)

GROUP_BY = ("model", "variant_label")
"""What these tests group by, as the other requirements tests do"""

FACETS = {
    "project": "CMIP6",
    "model": "ModelA",
    "experiment": "abrupt-4xCO2",
    "variant_label": "r1i1p1f1",
    "variable": "tas",
    "reporting_interval": "mon",
    "grid_label": "gn",
    "processing_id": "Amon",
}
"""Values for the facets these tests talk about"""

ONLY_GROUP = tuple((facet, FACETS[facet]) for facet in GROUP_BY)
"""The group every dataset below falls in"""


class CatalogueWithYears(Catalogue, Protocol):
    """
    A catalogue which can say how many years of data a dataset has

    Defined right beside the constraints which need it,
    which is the pattern for any information the
    [Catalogue][esmporium.requirements.Catalogue] protocol does not provide:
    see [esmporium.requirements.constraints][].
    """

    def get_years_on_record(self, entry: CatalogueEntry) -> float:
        """Get how many years of data a dataset has"""
        ...


@dataclass(frozen=True)
class InMemoryCatalogueWithYears(InMemoryCatalogue):
    """An in-memory catalogue which also knows how many years each dataset has"""

    years_on_record: Mapping[int, float] = field(default_factory=dict)
    """Entry ID -> how many years of data it has"""

    def get_years_on_record(self, entry: CatalogueEntry) -> float:
        """Get how many years of data a dataset has"""
        return self.years_on_record[entry.id]


class AtLeastNYears(BaseModel):
    """
    A role's datasets each have to be long enough

    Deliberately not `Covers`: nothing of ours ships yet, and this is what a user would
    write.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: str
    years: float
    ideal_years: float | None = None

    def evaluate(self, view: ConstraintView, catalogue: CatalogueWithYears) -> Outcome:
        for entry in view.roles[self.role]:
            have = catalogue.get_years_on_record(entry)
            if have < self.years:
                return Fail(f"#{entry.id} has {have:g} yr, needs {self.years:g}")

            if self.ideal_years is not None and have < self.ideal_years:
                return Degraded(
                    f"#{entry.id} has {have:g} yr, ideally {self.ideal_years:g}"
                )

        return Pass()


class SameYears(BaseModel):
    """
    Several roles have to be the same length as each other

    A check which compares roles, so it only means anything where it can see more than
    one: on the requirement.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    roles: tuple[str, ...]

    def evaluate(self, view: ConstraintView, catalogue: CatalogueWithYears) -> Outcome:
        found = {
            role: catalogue.get_years_on_record(entry)
            for role in self.roles
            for entry in view.roles.get(role, ())
        }
        if len(set(found.values())) > 1:
            listed = ", ".join(f"{role} {years:g} yr" for role, years in found.items())

            return Fail(f"roles disagree: {listed}")

        return Pass()


class ControlIsLonger(BaseModel):
    """
    A dataset's chain has to end somewhere longer than the dataset

    Reads [ConstraintView.lineages][esmporium.requirements.ConstraintView.lineages]
    rather than `roles`, which is what a check about a dataset's relatives needs.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    def evaluate(self, view: ConstraintView, catalogue: CatalogueWithYears) -> Outcome:
        for role, chains in view.lineages.items():
            for chain in chains:
                anchor = chain.members[0]
                end = chain.members[chain.index_of("end")]
                years = catalogue.get_years_on_record
                if years(end) <= years(anchor):
                    return Fail(f"{role}: #{end.id} is not longer than #{anchor.id}")

        return Pass()


class NotAModel:
    """A constraint which works and cannot be written down."""

    def evaluate(self, view, catalogue) -> Outcome:
        return Pass()


@pytest.fixture
def dataset(make_entry):
    """Get a factory for one stored dataset, with facet values a person can read."""

    def factory(entry_id, **facets):
        return make_entry(entry_id, **{**FACETS, **facets})

    return factory


def tas_requirement(*constraints, **overrides) -> Requirement:
    """Get a requirement for one dataset, carrying checks on its leaf."""
    settings = {
        "name": "tas",
        "tree": leaf(Query(variable="tas"), "tas").with_constraints(*constraints),
        "group_by": GROUP_BY,
        **overrides,
    }

    return requirement(**settings)


def catalogue_of(dataset, years: float | None = None) -> InMemoryCatalogueWithYears:
    """Get a catalogue of one dataset, with however many years recorded for it."""
    return InMemoryCatalogueWithYears(
        entries=(dataset(1),),
        years_on_record={1: years} if years is not None else {},
    )


def test_user_defined_constraint(dataset):
    # The whole claim of this step, in one test: a check esmporium has never heard of,
    # reading information no column records, decides whether the group is satisfied.
    long_enough = AtLeastNYears(role="tas", years=150)

    plenty = solve(tas_requirement(long_enough), catalogue_of(dataset, years=800.0))
    too_short = solve(tas_requirement(long_enough), catalogue_of(dataset, years=40.0))

    assert list(plenty.satisfied) == [ONLY_GROUP]
    assert list(too_short.unsatisfied) == [ONLY_GROUP]
    assert "needs 150" in too_short.explain()


def test_a_failed_check_still_says_what_was_found(dataset):
    # The dataset was found; a check ruled it out. An explanation which dropped the
    # first half would read as though nothing matched the query.
    result = solve(
        tas_requirement(AtLeastNYears(role="tas", years=150)),
        catalogue_of(dataset, years=40.0),
    )

    rendered = result.explain()
    assert "AtLeastNYears: #1 has 40 yr, needs 150" in rendered
    assert "[satisfied] tas: #1" in rendered


def test_a_catalogue_without_the_information_raises(dataset):
    # The constraint's type hint says it needs a catalogue
    # which can provide years of data.
    # A catalogue which cannot raises, and the solver lets that propagate.
    with pytest.raises(AttributeError, match="get_years_on_record"):
        solve(
            tas_requirement(AtLeastNYears(role="tas", years=150)),
            InMemoryCatalogue(entries=(dataset(1),)),
        )


def test_degraded_is_satisfied_with_a_note(dataset):
    # Usable, not ideal: the group is a solution, and what is wrong with it is both on
    # the solution and in the explanation.
    result = solve(
        tas_requirement(AtLeastNYears(role="tas", years=150, ideal_years=500)),
        catalogue_of(dataset, years=300.0),
    )

    (solution,) = result.satisfied.values()
    assert solution.notes == ("tas: AtLeastNYears: #1 has 300 yr, ideally 500",)
    assert f"[{ExplanationStatusOk.DEGRADED.value}] AtLeastNYears" in result.explain()


def test_a_met_check_leaves_no_note(dataset):
    (solution,) = solve(
        tas_requirement(AtLeastNYears(role="tas", years=150, ideal_years=200)),
        catalogue_of(dataset, years=800.0),
    ).satisfied.values()

    assert solution.notes == ()
    assert ExplanationStatusOk.DEGRADED.value not in solution.explanation.render()


def test_notes_from_several_leaves_are_kept(make_entry):
    # Notes merge upwards rather than the last one winning, so a group with two
    # complaints reports both.
    entries = tuple(
        make_entry(index, **{**FACETS, "variable": variable})
        for index, variable in enumerate(("tas", "rlut"), start=1)
    )
    needs_both = requirement(
        name="both",
        tree=all_of(
            leaf(Query(variable="tas"), "tas").with_constraints(
                AtLeastNYears(role="tas", years=1, ideal_years=500)
            ),
            leaf(Query(variable="rlut"), "rlut").with_constraints(
                AtLeastNYears(role="rlut", years=1, ideal_years=500)
            ),
        ),
        group_by=GROUP_BY,
    )

    (solution,) = solve(
        needs_both,
        InMemoryCatalogueWithYears(
            entries=entries, years_on_record={1: 300.0, 2: 200.0}
        ),
    ).satisfied.values()

    assert len(solution.notes) == 2
    assert {note.split(":")[0] for note in solution.notes} == {"tas", "rlut"}


def test_requirement_level_constraint_sees_every_role(make_entry):
    # A check which compares roles can only stand where it can see them both, which is
    # the requirement. The same check on either leaf would see one role and pass.
    entries = tuple(
        make_entry(index, **{**FACETS, "variable": variable})
        for index, variable in enumerate(("tas", "rlut"), start=1)
    )
    compared = requirement(
        name="compared",
        tree=all_of(
            leaf(Query(variable="tas"), "tas"), leaf(Query(variable="rlut"), "rlut")
        ),
        group_by=GROUP_BY,
        constraints=(SameYears(roles=("tas", "rlut")),),
    )

    def solved(tas_years, rlut_years):
        return solve(
            compared,
            InMemoryCatalogueWithYears(
                entries=entries,
                years_on_record={1: tas_years, 2: rlut_years},
            ),
        )

    assert list(solved(200.0, 200.0).satisfied) == [ONLY_GROUP]

    disagreeing = solved(200.0, 150.0)
    assert list(disagreeing.unsatisfied) == [ONLY_GROUP]
    assert "roles disagree" in disagreeing.explain()


def test_a_leaf_constraint_sees_the_chain(dataset):
    # A leaf's checks see the datasets its lineage found as well as the one which
    # filled its own role, which is what "the control covers this run" needs.
    traced = requirement(
        name="traced",
        tree=leaf(Query(variable="tas", experiment="abrupt-4xCO2"), "run")
        .with_lineage(Ancestors(until=Query(experiment="piControl"), role="control"))
        .with_constraints(ControlIsLonger()),
        group_by=GROUP_BY,
    )

    def solved(control_years):
        return solve(
            traced,
            InMemoryCatalogueWithYears(
                entries=(dataset(1), dataset(2, experiment="piControl")),
                parents={1: 2, 2: None},
                years_on_record={1: 150.0, 2: control_years},
            ),
        )

    assert list(solved(900.0).satisfied) == [ONLY_GROUP]

    short_control = solved(50.0)
    assert list(short_control.unsatisfied) == [ONLY_GROUP]
    assert "is not longer than" in short_control.explain()


def test_checks_run_in_the_order_they_were_given(dataset):
    # Which matters because the first failure ends it: a check whose message a user
    # will read first is the one they put first.
    result = solve(
        tas_requirement(
            AtLeastNYears(role="tas", years=100),
            AtLeastNYears(role="tas", years=1000),
        ),
        catalogue_of(dataset, years=500.0),
    )

    assert "needs 1000" in result.explain()


def test_not_a_constraint():
    with pytest.raises(NotAConstraintError, match="is not a constraint"):
        check_constraint(object())

    # The class rather than an instance is the mistake worth catching by name: it has
    # the method, as an unbound function, and nothing to check against.
    with pytest.raises(NotAConstraintError):
        leaf(Query(variable="tas"), "tas").with_constraints(42)


def test_a_constraint_class_is_refused():
    # The one a duck-typed check nearly lets through: a class *does* have an `evaluate`
    # attribute, the unbound function, so it is callable. Calling it would pass the view
    # as `self`, and the class carries nothing to check against either.
    with pytest.raises(NotAConstraintError, match="is a class, not a constraint"):
        check_constraint(AtLeastNYears)

    with pytest.raises(NotAConstraintError, match="is a class, not a constraint"):
        tas_requirement(AtLeastNYears)


def test_constraint_which_is_not_a_pydantic_model(dataset):
    # It solves, because solving only needs `evaluate`...
    works = tas_requirement(NotAModel())
    assert list(solve(works, catalogue_of(dataset)).satisfied) == [ONLY_GROUP]

    # ...and it cannot be stored, because storing needs fields which can be written
    # down. Pydantic wraps what a serialiser raises, so the type is its own and the
    # message is ours, which is the half that matters to whoever reads it.
    with pytest.raises(PydanticSerializationError) as raised:
        works.canonical_json()

    assert "NotAModel is not a pydantic model" in str(raised.value)
    assert NotSerialisableConstraintError.__name__ in str(raised.value)


def test_round_trip_user_defined_constraint():
    original = tas_requirement(AtLeastNYears(role="tas", years=150, ideal_years=500))

    reloaded = Requirement.model_validate_json(original.canonical_json())

    assert reloaded == original
    assert reloaded.requirement_hash() == original.requirement_hash()
    assert reloaded.tree.constraints == original.tree.constraints


def test_a_serialised_constraint_carries_its_import_path():
    serialised = tas_requirement(AtLeastNYears(role="tas", years=150)).canonical_json()

    assert f"{__name__}:AtLeastNYears" in serialised


def test_a_constraint_is_part_of_the_question():
    # Two requirements which differ only in a check are different questions, so change
    # tracking cannot confuse one for the other.
    plain = tas_requirement()
    checked = tas_requirement(AtLeastNYears(role="tas", years=150))
    stricter = tas_requirement(AtLeastNYears(role="tas", years=500))

    hashes = {each.requirement_hash() for each in (plain, checked, stricter)}
    assert len(hashes) == 3


def test_requirement_level_constraints_round_trip():
    original = requirement(
        name="compared",
        tree=all_of(
            leaf(Query(variable="tas"), "tas"), leaf(Query(variable="rlut"), "rlut")
        ),
        group_by=GROUP_BY,
        constraints=(SameYears(roles=("tas", "rlut")),),
    )

    assert Requirement.model_validate_json(original.canonical_json()) == original


def stored_with_constraints(*serialised) -> dict:
    """Get a stored requirement by hand, to load constraints which were never built."""
    return {
        "name": "bad",
        "tree": {
            "kind": "leaf",
            "role": "tas",
            "query": {"variable": ["tas"]},
            "constraints": list(serialised),
        },
        "group_by": list(GROUP_BY),
    }


def test_a_path_which_resolves_to_the_wrong_thing_is_refused():
    # The load path imports what the stored text names, and what it finds there may not
    # be a constraint at all. Naming the path is the point: that is what has to change.
    with pytest.raises(NotAConstraintError, match="does not name a constraint"):
        Requirement.model_validate(
            stored_with_constraints({"type": f"{__name__}:GROUP_BY"})
        )


def test_a_path_which_names_a_model_that_is_not_a_constraint_is_refused():
    # The near miss: it is a pydantic model, so it validates, and it has no `evaluate`
    # to call. Caught after building it rather than before, because whether the fields
    # fit is the first thing which tells you the path is wrong.
    with pytest.raises(NotAConstraintError, match="does not name a constraint"):
        Requirement.model_validate(
            stored_with_constraints({"type": "esmporium.query:QueryCanonical"})
        )


def test_a_path_which_resolves_to_nothing_says_so():
    # What a renamed or moved constraint class does to every requirement stored against
    # it, which is the cost of the path being the identity.
    with pytest.raises(UnresolvableConstraintError, match="Cannot load"):
        Requirement.model_validate(
            stored_with_constraints({"type": f"{__name__}:NoSuchCheck"})
        )

    with pytest.raises(UnresolvableConstraintError, match="Cannot load"):
        Requirement.model_validate(
            stored_with_constraints({"type": "no.such.module:Check"})
        )


def test_a_constraint_of_a_shape_we_do_not_know_is_refused():
    with pytest.raises(NotAConstraintError, match="is not a constraint"):
        Requirement.model_validate(
            stored_with_constraints({"not_a_type": "AtLeastNYears"})
        )


def test_constraints_have_to_arrive_as_a_sequence():
    # One constraint rather than a list of one: iterating it would give a confusing
    # per-entry error, so the sequence check comes first.
    with pytest.raises(NotAConstraintError, match="constraints are held as a sequence"):
        Requirement.model_validate(
            {
                "name": "bad",
                "tree": {
                    "kind": "leaf",
                    "role": "tas",
                    "query": {"variable": ["tas"]},
                    "constraints": AtLeastNYears(role="tas", years=1),
                },
                "group_by": list(GROUP_BY),
            }
        )


def test_nothing_in_the_package_names_a_constraint():
    # The claim this step exists to make, checked rather than argued for. Every
    # constraint in this module is defined in this module, so the package naming one of
    # them would mean something had been special-cased. `Covers` is in the list because
    # it is the one we are about to ship, and it must arrive as an implementation of
    # this protocol rather than as a case the solver knows about.
    package = Path(esmporium.requirements.__file__).parent
    sources = {path.name: path.read_text() for path in package.glob("*.py")}

    for name in (
        "AtLeastNYears",
        "SameYears",
        "ControlIsLonger",
        "NotAModel",
        "Covers",
    ):
        naming = sorted(file for file, text in sources.items() if name in text)
        assert not naming, f"{name} is named in {naming}"
