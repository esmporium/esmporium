"""
Tests of [Covers][esmporium.requirements.Covers]
"""

from __future__ import annotations

import pytest

from esmporium.query import Query
from esmporium.requirements import (
    Ancestors,
    CatalogueEntry,
    ConstraintView,
    Covers,
    Degraded,
    Fail,
    InMemoryCatalogue,
    Lineage,
    Pass,
    leaf,
    requirement,
    solve,
)

from .catalogues import InMemoryCatalogueWithTimeInfo

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

TO_CONTROL = Ancestors(until=Query(experiment="piControl"), role="control")
"""Trace a dataset back to the control its simulation branched from"""


@pytest.fixture
def dataset(make_entry):
    """Get a factory for one stored dataset, with facet values a person can read."""

    def factory(entry_id, **facets):
        return make_entry(entry_id, **{**FACETS, **facets})

    return factory


def lineage_view(*members: CatalogueEntry, end_role: str = "control") -> ConstraintView:
    """Get what a constraint on a `tas` leaf sees, when its lineage is `members`"""
    return ConstraintView(
        prefix="",
        roles={},
        lineages={"tas": (Lineage(members=members, end_role=end_role),)},
    )


def child_and_control(dataset, *, child, control, branch=None):
    """
    Get a child, its control and a catalogue holding them

    `child` and `control` are their time ranges,
    and `branch` is where the child branched from the control, if given.
    """
    child_entry = dataset(1)
    control_entry = dataset(2, experiment="piControl")
    catalogue = InMemoryCatalogueWithTimeInfo(
        entries=(child_entry, control_entry),
        parents={1: 2, 2: None},
        time_ranges={1: child, 2: control},
        branch_times_in_parent={} if branch is None else {1: branch},
    )

    return child_entry, control_entry, catalogue


def test_covers_needs_a_lineage():
    outcome = Covers(role="control").evaluate(
        ConstraintView(prefix="", roles={}, lineages={}),
        InMemoryCatalogueWithTimeInfo(entries=()),
    )

    assert outcome == Fail("Covers needs a lineage, but none was resolved")


def test_covers_branch(dataset):
    # The child's 150 years start 100 years into the control,
    # so in the control's time axis it spans [100, 250).
    child, control, catalogue = child_and_control(
        dataset, child=(0.0, 150.0), control=(0.0, 200.0), branch=100.0
    )
    view = lineage_view(child, control)

    assert Covers(role="control").evaluate(view, catalogue) == Fail(
        "tas: #2 covers [0, 200), needed [100, 250) with padding (0.0, 0.0)"
    )

    _, _, longer = child_and_control(
        dataset, child=(0.0, 150.0), control=(0.0, 250.0), branch=100.0
    )
    assert Covers(role="control").evaluate(view, longer) == Pass()


def test_covers_calendar(dataset):
    child, control, catalogue = child_and_control(
        dataset, child=(1850.0, 2015.0), control=(1850.0, 1880.0)
    )

    outcome = Covers(role="control", align="calendar").evaluate(
        lineage_view(child, control), catalogue
    )

    assert isinstance(outcome, Fail)
    assert outcome.message.startswith(
        "tas: #2 covers [1850, 1880), needed [1850, 2015)"
    )


def test_covers_branch_needs_ancestor(dataset):
    child, control, catalogue = child_and_control(
        dataset, child=(0.0, 10.0), control=(0.0, 10.0), branch=0.0
    )

    with pytest.raises(ValueError, match="must be an ancestor"):
        Covers(role="self", target="control").evaluate(
            lineage_view(child, control), catalogue
        )

    with pytest.raises(ValueError, match=r"'chain\.0' is not in this lineage"):
        Covers(role="chain.0").evaluate(lineage_view(child, control), catalogue)

    with pytest.raises(ValueError, match="use 'self', 'historical'"):
        Covers(role="control").evaluate(
            lineage_view(child, control, end_role="historical"), catalogue
        )


def test_end_and_the_lineage_role_name_mean_the_same_thing(dataset):
    child, control, catalogue = child_and_control(
        dataset, child=(0.0, 10.0), control=(0.0, 5.0)
    )
    view = lineage_view(child, control)

    by_end = Covers(role="end", align="calendar").evaluate(view, catalogue)
    by_name = Covers(role="control", align="calendar").evaluate(view, catalogue)

    assert isinstance(by_end, Fail)
    assert by_end == by_name


def test_ideal_padding_not_met_is_degraded(dataset):
    # Needs [100, 250), so ideal padding of 20 years either side needs [80, 270)
    child, control, catalogue = child_and_control(
        dataset, child=(0.0, 150.0), control=(90.0, 1000.0), branch=100.0
    )

    outcome = Covers(role="control", ideal_pad_years=(20.0, 20.0)).evaluate(
        lineage_view(child, control), catalogue
    )

    assert outcome == Degraded(
        "tas: #2 covers [90, 1000), needed [100, 250), "
        "ideal padding (20.0, 20.0) not met"
    )


def test_no_required_padding_only_checks_the_ideal(dataset):
    # 'Ideally spans', for an analysis with workarounds when it does not
    child, control, catalogue = child_and_control(
        dataset, child=(0.0, 150.0), control=(0.0, 200.0), branch=100.0
    )
    ideally = Covers(role="control", pad_years=None, ideal_pad_years=(0.0, 0.0))

    outcome = ideally.evaluate(lineage_view(child, control), catalogue)

    assert isinstance(outcome, Degraded)


BRANCH_REQUIREMENT = requirement(
    name="zec",
    tree=leaf(Query(variable="tas", experiment="esm-1pct-brch-1000PgC"), "tas")
    .with_lineage(TO_CONTROL)
    .with_constraints(Covers(role="control")),
    group_by=GROUP_BY,
)
"""A dataset two hops from its control, which has to cover both of them"""


def branch_chain_catalogue(dataset, control_end: float):
    """
    Get a ZEC run, the 1pctCO2 run it branched from, and their control

    In piControl's time axis,
    1pctCO2 is [100, 250) and the ZEC run is [170, 270).
    """
    return InMemoryCatalogueWithTimeInfo(
        entries=(
            dataset(1, experiment="esm-1pct-brch-1000PgC"),
            dataset(2, experiment="1pctCO2"),
            # Different variant to the child, still linked via the parent link
            dataset(3, experiment="piControl", variant_label="r1i1p2f1"),
        ),
        parents={1: 2, 2: 3, 3: None},
        time_ranges={1: (0.0, 100.0), 2: (0.0, 150.0), 3: (0.0, control_end)},
        # The ZEC run branches 70 years into 1pctCO2,
        # which branches 100 years into piControl
        branch_times_in_parent={1: 70.0, 2: 100.0},
    )


def test_branch_chain_back_to_the_control(dataset):
    result = solve(BRANCH_REQUIREMENT, branch_chain_catalogue(dataset, 300.0))

    (group,) = result.satisfied.values()
    assert group.one("chain.0.tas").experiment == "1pctCO2"
    assert group.one("control.tas").variant_label == "r1i1p2f1"


def test_branch_chain_control_must_cover_child_and_parents(dataset):
    # Covering the ZEC run alone, [170, 270), would be enough at 270,
    # but the analysis needs the control over 1pctCO2 too.
    result = solve(BRANCH_REQUIREMENT, branch_chain_catalogue(dataset, 260.0))

    assert list(result.unsatisfied) == [ONLY_GROUP]
    assert "needed [100, 270)" in result.explain()


def test_degraded_is_kept_as_a_note(dataset):
    _, _, catalogue = child_and_control(
        dataset, child=(0.0, 150.0), control=(90.0, 1000.0), branch=100.0
    )
    ideally_padded = requirement(
        name="tcr",
        tree=leaf(Query(variable="tas", experiment="abrupt-4xCO2"), "tas")
        .with_lineage(TO_CONTROL)
        .with_constraints(Covers(role="control", ideal_pad_years=(20.0, 20.0))),
        group_by=GROUP_BY,
    )

    (solution,) = solve(ideally_padded, catalogue).satisfied.values()

    assert len(solution.notes) == 1
    assert "ideal padding (20.0, 20.0) not met" in solution.notes[0]


def test_a_catalogue_without_time_info_is_unsatisfied(dataset):
    # Nothing records time ranges yet, so this is what solving `Covers`
    # against the database looks like today
    result = solve(
        BRANCH_REQUIREMENT,
        InMemoryCatalogue(
            entries=(
                dataset(1, experiment="esm-1pct-brch-1000PgC"),
                dataset(2, experiment="piControl"),
            ),
            parents={1: 2, 2: None},
        ),
    )

    assert list(result.unsatisfied) == [ONLY_GROUP]
    rendered = result.explain()
    assert "Covers: could not be evaluated: AttributeError" in rendered
    assert "get_time_range" in rendered


def test_branch_times_can_be_corrected_by_wrapping_the_catalogue(dataset):
    # Recorded branch times often have bugs,
    # and the way to correct one is to override the method which provides it
    class Corrected(InMemoryCatalogueWithTimeInfo):
        def get_branch_time_in_parent(self, entry: CatalogueEntry) -> float:
            if entry.id == 1:
                return 0.0

            return super().get_branch_time_in_parent(entry)

    child, control, recorded = child_and_control(
        dataset, child=(0.0, 150.0), control=(0.0, 200.0), branch=100.0
    )
    corrected = Corrected(
        entries=recorded.entries,
        parents=recorded.parents,
        time_ranges=recorded.time_ranges,
        branch_times_in_parent=recorded.branch_times_in_parent,
    )
    view = lineage_view(child, control)

    assert isinstance(Covers(role="control").evaluate(view, recorded), Fail)
    assert Covers(role="control").evaluate(view, corrected) == Pass()
