"""
Tests of [Covers][esmporium.requirements.Covers]

Everything here is in **years** on a yearly axis unless the test says otherwise, so
that the numbers in an assertion are the numbers in the picture
`docs/further-background/enrichment.md` draws. The monthly axis -- years asked for,
months measured, which is what real monthly data takes -- has its own section.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from esmporium.datasets import NegativeSpanError, Span, TimeAxis, TimeStep
from esmporium.query import Query
from esmporium.requirements import (
    Ancestors,
    CatalogueEntry,
    ConstraintView,
    Covers,
    CoversModeError,
    CoversNeedsATimeAxisError,
    Degraded,
    EnrichmentInput,
    ExplanationStatusNotOk,
    Fail,
    InMemoryCatalogue,
    Lineage,
    MissingFeature,
    MissingTimeFact,
    NeedsEnrichment,
    Pass,
    leaf,
    requirement,
    solve,
)

from .catalogues import (
    MONTHLY_AXIS,
    YEARLY_AXIS,
    InMemoryCatalogueWithTimeInfo,
    branched_at,
    measured,
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

TO_CONTROL = Ancestors(until=Query(experiment="piControl"), role="control")
"""Trace a dataset back to the control its simulation branched from"""

YEARS = TimeStep.YEARS
"""Shorthand, because almost every span below is a number of years"""


def years(count: int) -> tuple[Span, Span]:
    """Get the same number of years of padding either side"""
    return (Span(count, YEARS), Span(count, YEARS))


NO_PAD = years(0)
"""No padding, i.e. the covered span and not one year more"""


def containment(**overrides) -> Covers:
    """Get a containment check on the control, with nothing else stated"""
    settings = {
        "role": "control",
        "align": "branch",
        "mode": "containment",
        "pad": NO_PAD,
        **overrides,
    }

    return Covers(**settings)


def overlap(count: int, **overrides) -> Covers:
    """Get an overlap check on the control asking for `count` years from the branch"""
    settings = {
        "role": "control",
        "align": "branch",
        "mode": "overlap",
        "min_overlap": Span(count, YEARS),
        "pad": NO_PAD,
        **overrides,
    }

    return Covers(**settings)


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


def child_and_control(
    dataset, *, child, control, branch=None, axis: TimeAxis = YEARLY_AXIS
):
    """
    Get a child, its control and a catalogue holding them

    `child` and `control` are measured ranges and `branch` a branch point, or `None`
    for "nothing recorded it" -- which is how a test says a fact is missing rather
    than wrong.
    """
    child_entry = dataset(1)
    control_entry = dataset(2, experiment="piControl")
    catalogue = InMemoryCatalogueWithTimeInfo(
        entries=(child_entry, control_entry),
        parents={1: 2, 2: None},
        time_ranges={
            entry_id: each
            for entry_id, each in ((1, child), (2, control))
            if each is not None
        },
        branch_times_in_parent={} if branch is None else {1: branched_at(branch, axis)},
    )

    return child_entry, control_entry, catalogue


# ------------------------------------------------------------ the worked examples


@pytest.fixture
def worked_example(dataset):
    """
    Get the pair the design note works through, and what a check sees of them

    Control `[0, 200)`, child `[0, 150)` branching 100 years in, so the child maps to
    `[100, 250)` in the control's axis and its extent is 150. Both rows of the note's
    verification table are read off exactly this.
    """
    child, control, catalogue = child_and_control(
        dataset,
        child=measured(0, 150, YEARLY_AXIS),
        control=measured(0, 200, YEARLY_AXIS),
        branch=100,
    )

    return lineage_view(child, control), catalogue


@pytest.mark.parametrize(
    ("min_overlap", "expected"),
    [
        # Control reaches back (0 <= 90) but not forward (200 < 210).
        (100, Fail),
        # 200 >= 120, so there is room for ten years of overlap and the padding.
        (10, Pass),
    ],
)
def test_the_notes_verification_table(worked_example, min_overlap, expected):
    view, catalogue = worked_example

    outcome = overlap(min_overlap, pad=years(10)).evaluate(view, catalogue)

    assert isinstance(outcome, expected)


def test_the_window_is_anchored_at_the_branch_point(dataset):
    """
    A sliding window would pass this, and would be wrong to

    There is 90 years of overlap further right, but no control data *at* the branch
    point, so there is nothing to take an anomaly against. Anchoring is both stricter
    and easier to explain.
    """
    child, control, catalogue = child_and_control(
        dataset,
        child=measured(0, 150, YEARLY_AXIS),
        control=measured(150, 400, YEARLY_AXIS),
        branch=100,
    )

    outcome = overlap(90, pad=years(10)).evaluate(
        lineage_view(child, control), catalogue
    )

    assert isinstance(outcome, Fail)
    # The control starts at 150 and the window needs to begin at 90.
    assert "covers [150, 400) and needs [90, 200)" in outcome.message


def test_overlap_mode_also_needs_the_covered_run_to_be_long_enough(dataset):
    # The control is enormous, so the only thing which can fall short is the child:
    # 50 years of data cannot supply 100 years of overlap.
    child, control, catalogue = child_and_control(
        dataset,
        child=measured(0, 50, YEARLY_AXIS),
        control=measured(0, 10_000, YEARLY_AXIS),
        branch=100,
    )

    outcome = overlap(100).evaluate(lineage_view(child, control), catalogue)

    assert isinstance(outcome, Fail)


def test_containment_is_the_covered_runs_own_extent(worked_example):
    # Containment needs [100, 250), which 200 does not reach, and 250 exactly does:
    # `end_exclusive` is the first year *not* in the data, so they meet with nothing
    # over.
    view, catalogue = worked_example

    assert isinstance(containment().evaluate(view, catalogue), Fail)
    assert (
        containment().evaluate(
            view,
            InMemoryCatalogueWithTimeInfo(
                entries=catalogue.entries,
                parents=catalogue.parents,
                time_ranges={
                    1: measured(0, 150, YEARLY_AXIS),
                    2: measured(0, 250, YEARLY_AXIS),
                },
                branch_times_in_parent=catalogue.branch_times_in_parent,
            ),
        )
        == Pass()
    )


def test_containment_refuses_a_min_overlap_and_overlap_requires_one():
    # Two halves of one rule: the mode says which question is being asked, so the
    # thresholds which belong to the other one are a mistake rather than ignored.
    with pytest.raises(ValidationError, match="needs `min_overlap`"):
        Covers(role="control", align="branch", mode="overlap", pad=NO_PAD)

    with pytest.raises(ValidationError, match="takes no `min_overlap`"):
        Covers(
            role="control",
            align="branch",
            mode="containment",
            pad=NO_PAD,
            min_overlap=Span(1, YEARS),
        )


def test_nothing_is_defaulted():
    # Settled decision 9. `target` is the one exception, because it names a role
    # rather than stating part of the coverage question.
    with pytest.raises(ValidationError) as caught:
        Covers(role="control")

    missing = {error["loc"][0] for error in caught.value.errors()}
    assert missing == {"align", "mode", "pad"}
    assert Covers(
        role="control", align="branch", mode="containment", pad=NO_PAD
    ).target == ("self")


# ------------------------------------------------------------------- the two aligns


def test_covers_needs_a_lineage():
    outcome = containment().evaluate(
        ConstraintView(prefix="", roles={}, lineages={}),
        InMemoryCatalogueWithTimeInfo(entries=()),
    )

    assert outcome == Fail("Covers needs a lineage, but none was resolved")


def test_covers_calendar(dataset):
    child, control, catalogue = child_and_control(
        dataset,
        child=measured(1850, 2015, YEARLY_AXIS),
        control=measured(1850, 1880, YEARLY_AXIS),
    )

    outcome = Covers(
        role="control", align="calendar", mode="containment", pad=NO_PAD
    ).evaluate(lineage_view(child, control), catalogue)

    assert isinstance(outcome, Fail)
    assert "covers [1850, 1880) and needs [1850, 2015)" in outcome.message


def test_calendar_alignment_never_asks_for_a_branch_point(dataset):
    # Which is why it works against a catalogue with no branch point recorded at all,
    # and is the distinction a future `SameTimeRange` must not optimise away.
    child, control, catalogue = child_and_control(
        dataset,
        child=measured(1850, 1900, YEARLY_AXIS),
        control=measured(1850, 2000, YEARLY_AXIS),
        branch=None,
    )

    outcome = Covers(
        role="control", align="calendar", mode="containment", pad=NO_PAD
    ).evaluate(lineage_view(child, control), catalogue)

    assert outcome == Pass()


def test_calendar_alignment_refuses_axes_which_do_not_line_up(dataset):
    # Settled decision 11: with no branch point to resolve it, a misaligned axis is a
    # question we can answer and the answer is no.
    child, control, catalogue = child_and_control(
        dataset,
        child=measured(22189, 24000, MONTHLY_AXIS),
        control=measured(1850, 2000, YEARLY_AXIS),
    )

    outcome = Covers(
        role="control", align="calendar", mode="containment", pad=NO_PAD
    ).evaluate(lineage_view(child, control), catalogue)

    assert isinstance(outcome, Fail)
    assert "not on comparable axes" in outcome.message


def test_covers_branch_needs_ancestor(dataset):
    child, control, catalogue = child_and_control(
        dataset,
        child=measured(0, 10, YEARLY_AXIS),
        control=measured(0, 10, YEARLY_AXIS),
        branch=0,
    )

    with pytest.raises(ValueError, match="must be an ancestor"):
        Covers(
            role="self",
            target="control",
            align="branch",
            mode="containment",
            pad=NO_PAD,
        ).evaluate(lineage_view(child, control), catalogue)

    with pytest.raises(ValueError, match=r"'chain\.0' is not in this lineage"):
        containment(role="chain.0").evaluate(lineage_view(child, control), catalogue)

    with pytest.raises(ValueError, match="use 'self', 'historical'"):
        containment().evaluate(
            lineage_view(child, control, end_role="historical"), catalogue
        )


def test_end_and_the_lineage_role_name_mean_the_same_thing(dataset):
    child, control, catalogue = child_and_control(
        dataset,
        child=measured(0, 10, YEARLY_AXIS),
        control=measured(0, 5, YEARLY_AXIS),
    )
    view = lineage_view(child, control)

    by_end = Covers(
        role="end", align="calendar", mode="containment", pad=NO_PAD
    ).evaluate(view, catalogue)
    by_name = Covers(
        role="control", align="calendar", mode="containment", pad=NO_PAD
    ).evaluate(view, catalogue)

    assert isinstance(by_end, Fail)
    assert by_end == by_name


# --------------------------------------------------------------- the soft thresholds


def test_ideal_padding_not_met_is_degraded(dataset):
    # Needs [100, 250), so ideal padding of 20 years either side needs [80, 270).
    child, control, catalogue = child_and_control(
        dataset,
        child=measured(0, 150, YEARLY_AXIS),
        control=measured(90, 1000, YEARLY_AXIS),
        branch=100,
    )

    outcome = containment(ideal_pad=years(20)).evaluate(
        lineage_view(child, control), catalogue
    )

    assert outcome == Degraded(
        "tas: #2 covers [90, 1000) and needs [80, 270), in years, "
        "ideal padding (20 years, 20 years) not met"
    )


def test_an_ideal_overlap_not_met_is_degraded(worked_example):
    # The hard 10 years are there and the ideal 100 are not, which is `Degraded`'s
    # other producer: dropping `ideal_min_overlap` would leave overlap mode unable to
    # report anything short of a failure.
    view, catalogue = worked_example

    # Needs [100, 110) hard, and [100, 250) ideally, which 200 does not reach.
    outcome = overlap(10, ideal_min_overlap=Span(150, YEARS)).evaluate(view, catalogue)

    assert isinstance(outcome, Degraded)


def test_an_unstated_ideal_falls_back_to_the_hard_requirement(worked_example):
    """
    So that the soft check is never looser than the hard one

    An `ideal_min_overlap` on its own must not quietly drop the padding which *is*
    required, or a constraint could pass its ideal while failing its requirement.
    """
    view, catalogue = worked_example

    outcome = overlap(10, pad=years(10), ideal_min_overlap=Span(100, YEARS)).evaluate(
        view, catalogue
    )

    assert isinstance(outcome, Degraded)
    # 10 years of padding, carried over rather than reset to zero.
    assert "ideal padding (10 years, 10 years)" in outcome.message


def test_no_required_padding_only_checks_the_ideal(worked_example):
    # 'Ideally spans', for an analysis with workarounds when it does not. The same
    # data is a `Fail` when the padding is required and a `Degraded` when it is not.
    view, catalogue = worked_example

    assert isinstance(containment().evaluate(view, catalogue), Fail)
    assert isinstance(
        containment(pad=None, ideal_pad=NO_PAD).evaluate(view, catalogue), Degraded
    )


def test_a_check_which_requires_nothing_is_refused():
    # `pad=None` with no ideal would pass whatever the data, which is not a check.
    with pytest.raises(ValidationError, match="requires nothing at all"):
        Covers(role="control", align="branch", mode="containment", pad=None)


def test_degraded_is_kept_as_a_note(dataset):
    _, _, catalogue = child_and_control(
        dataset,
        child=measured(0, 150, YEARLY_AXIS),
        control=measured(90, 1000, YEARLY_AXIS),
        branch=100,
    )
    ideally_padded = requirement(
        name="tcr",
        tree=leaf(Query(variable="tas", experiment="abrupt-4xCO2"), "tas")
        .with_lineage(TO_CONTROL)
        .with_constraints(containment(ideal_pad=years(20))),
        group_by=GROUP_BY,
    )

    (solution,) = solve(ideally_padded, catalogue).satisfied.values()

    assert len(solution.notes) == 1
    assert "ideal padding (20 years, 20 years) not met" in solution.notes[0]


# ----------------------------------------------------- years asked, months measured


def test_a_requirement_in_years_against_a_monthly_axis(dataset):
    """
    The conversion real monthly data takes, and the one which has to stay exact

    Twelve months is a year in every CF calendar, so this needs no calendar and no
    `cftime`: 20 years of padding is 240 months, exactly, whichever calendar the data
    turns out to be in.
    """
    child, control, catalogue = child_and_control(
        dataset,
        child=measured(0, 1800, MONTHLY_AXIS),
        control=measured(960, 3240, MONTHLY_AXIS),
        branch=1200,
        axis=MONTHLY_AXIS,
    )
    view = lineage_view(child, control)

    # Needs [1200, 3000); 20 years either side is [960, 3240), which is exactly what
    # the control holds.
    assert containment(pad=years(20)).evaluate(view, catalogue) == Pass()
    # One year more either side is not there.
    assert isinstance(containment(pad=years(21)).evaluate(view, catalogue), Fail)


def test_months_of_padding_against_a_yearly_axis_needs_a_finer_axis(worked_example):
    # Six months is not a position on a yearly axis, and it is not a limit of the
    # requirement either: a header read puts the data on a dated axis, where it is.
    view, catalogue = worked_example

    outcome = containment(pad=(Span(6, TimeStep.MONTHS), Span(0, YEARS))).evaluate(
        view, catalogue
    )

    assert isinstance(outcome, NeedsEnrichment)
    assert outcome.inputs == (
        EnrichmentInput(dataset_id=2, missing=MissingTimeFact.CALENDAR),
    )


# ------------------------------------------------------------- the facts, or not


@pytest.mark.parametrize(
    ("missing", "expected"),
    [
        ("child_range", (1,)),
        ("control_range", (2,)),
        ("both_ranges", (1, 2)),
    ],
)
def test_an_unmeasured_range_needs_enrichment(dataset, missing, expected):
    """
    The distinction the whole enrichment step exists for

    Nobody has measured this, which is not the same as there being no data -- and
    before this it was reported in the same bucket as there being none.
    """
    child, control, catalogue = child_and_control(
        dataset,
        child=None
        if missing in ("child_range", "both_ranges")
        else measured(0, 150, YEARLY_AXIS),
        control=None
        if missing in ("control_range", "both_ranges")
        else measured(0, 250, YEARLY_AXIS),
        branch=100,
    )

    outcome = containment().evaluate(lineage_view(child, control), catalogue)

    assert isinstance(outcome, NeedsEnrichment)
    assert outcome.inputs == tuple(
        EnrichmentInput(dataset_id=entry_id, missing=MissingTimeFact.TIME_RANGE)
        for entry_id in expected
    )


def test_an_unrecorded_branch_point_needs_enrichment(dataset):
    # A CMIP5 branch time is published without units, so it cannot be interpreted as
    # it stands and the dataset answers `None` until a fix records the value.
    child, control, catalogue = child_and_control(
        dataset,
        child=measured(0, 150, YEARLY_AXIS),
        control=measured(0, 250, YEARLY_AXIS),
        branch=None,
    )

    outcome = containment().evaluate(lineage_view(child, control), catalogue)

    assert isinstance(outcome, NeedsEnrichment)
    assert outcome.inputs == (
        EnrichmentInput(dataset_id=1, missing=MissingTimeFact.BRANCH_POINT),
    )


def test_a_branch_point_on_a_different_axis_to_its_parent_fails(dataset):
    # The branch point and the parent's own data disagreeing about which axis the
    # parent is on is the data disagreeing with itself, not a question of alignment.
    child, control, catalogue = child_and_control(
        dataset,
        child=measured(0, 150, YEARLY_AXIS),
        control=measured(0, 250, YEARLY_AXIS),
        branch=100,
        axis=MONTHLY_AXIS,
    )

    outcome = containment().evaluate(lineage_view(child, control), catalogue)

    assert isinstance(outcome, Fail)
    assert "have to be the same axis" in outcome.message


def test_a_child_and_parent_at_different_frequencies_is_not_supported_yet(dataset):
    child, control, catalogue = child_and_control(
        dataset,
        child=measured(0, 1800, MONTHLY_AXIS),
        control=measured(0, 250, YEARLY_AXIS),
        branch=100,
    )

    outcome = containment().evaluate(lineage_view(child, control), catalogue)

    assert isinstance(outcome, MissingFeature)
    assert "github.com/esmporium/esmporium/issues" in outcome.message


@pytest.mark.parametrize("gapped", ["child", "control"])
def test_a_gapped_range_is_refused(dataset, gapped):
    """
    Settled decision 8, at both ends of the comparison

    A version whose files leave a gap still has a first and a last step, so treating
    it as continuous is how a missing decade gets reported as covered. The flag is
    what is refused, not a missing range -- so this is a `Fail` rather than something
    to go and look at.
    """
    child, control, catalogue = child_and_control(
        dataset,
        child=measured(0, 150, YEARLY_AXIS, contiguous=gapped != "child"),
        control=measured(0, 10_000, YEARLY_AXIS, contiguous=gapped != "control"),
        branch=100,
    )

    outcome = containment().evaluate(lineage_view(child, control), catalogue)

    assert isinstance(outcome, Fail)
    assert "has a gap in it" in outcome.message


def test_a_catalogue_without_time_info_cannot_be_asked(dataset):
    # `InMemoryCatalogue` does not provide `CatalogueWithTimeInfo`, which the check
    # says for itself rather than leaving to an `AttributeError`: the solver no longer
    # turns every exception into a failure.
    child = dataset(1)
    control = dataset(2, experiment="piControl")

    outcome = containment().evaluate(
        lineage_view(child, control),
        InMemoryCatalogue(entries=(child, control), parents={1: 2, 2: None}),
    )

    assert isinstance(outcome, Fail)
    assert "get_time_range" in outcome.message
    assert "CatalogueWithTimeInfo" in outcome.message


# --------------------------------------------------------------------- no time axis


def test_fx_among_other_candidates_fails_when_solved(dataset):
    # Tier two of settled decision 15: a dataset with no time axis found among others
    # is a fact about the data, so it is reported rather than refusing the requirement.
    _, control, catalogue = child_and_control(
        dataset,
        child=measured(0, 150, YEARLY_AXIS),
        control=measured(0, 10_000, YEARLY_AXIS),
        branch=100,
    )
    fixed_field = dataset(3, variable="areacella", reporting_interval="fx")

    outcome = containment().evaluate(lineage_view(fixed_field, control), catalogue)

    assert isinstance(outcome, Fail)
    assert "has no time axis" in outcome.message


def test_a_leaf_which_can_only_select_fx_is_refused_when_built():
    # Tier one: a mistake in the requirement, so it does not wait for data. The same
    # two tiers `LineageStartsAtTargetError` draws.
    with pytest.raises(CoversNeedsATimeAxisError, match="has no time axis"):
        requirement(
            name="areas",
            tree=leaf(
                Query(project="CMIP6", variable="areacella", reporting_interval="fx"),
                "areas",
            )
            .with_lineage(TO_CONTROL)
            .with_constraints(containment()),
            group_by=GROUP_BY,
        )


def test_a_leaf_which_could_select_fx_among_others_is_allowed():
    # The other side of tier one, and the reason it tests what the query can *only*
    # select: naming no frequency at all is the ordinary case.
    built = requirement(
        name="tas",
        tree=leaf(Query(project="CMIP6", variable="tas"), "tas")
        .with_lineage(TO_CONTROL)
        .with_constraints(containment()),
        group_by=GROUP_BY,
    )

    assert built.name == "tas"


def test_a_requirements_where_is_part_of_what_a_leaf_selects():
    # The same mistake written one level up, which is why the check runs when the
    # requirement is built as well as when a leaf is. Wrapped by pydantic here,
    # because this is the half which only the requirement's own validator can see --
    # the same shape `check_lineages_do_not_start_at_their_target` has.
    with pytest.raises(ValidationError, match="has no time axis"):
        requirement(
            name="areas",
            tree=leaf(Query(variable="areacella"), "areas")
            .with_lineage(TO_CONTROL)
            .with_constraints(containment()),
            where=Query(project="CMIP6", reporting_interval="fx"),
            group_by=GROUP_BY,
        )


# -------------------------------------------------------------- the unsupported unit


@pytest.mark.parametrize("step", [TimeStep.DAYS, TimeStep.HOURS, TimeStep.MINUTES])
def test_padding_finer_than_a_month_is_a_missing_feature(worked_example, step):
    """
    Settled decisions 10a and 17 together

    A limit of how the requirement can be *written*, not of which data we judge --
    daily data is judged fine, in years or months. And a returned outcome rather than
    a raise, so one unsupported group does not stop the others being answered.
    """
    view, catalogue = worked_example

    outcome = containment(pad=(Span(10, step), Span(10, step))).evaluate(
        view, catalogue
    )

    assert isinstance(outcome, MissingFeature)
    assert "github.com/esmporium/esmporium/issues" in outcome.message


def test_an_unsupported_unit_does_not_stop_the_other_groups(dataset):
    # Which is the whole reason it is an outcome: a second group, in another model,
    # is answered rather than lost to one raise.
    unsupported = requirement(
        name="tcr",
        tree=leaf(Query(variable="tas", experiment="abrupt-4xCO2"), "tas")
        .with_lineage(TO_CONTROL)
        .with_constraints(
            containment(pad=(Span(1, TimeStep.DAYS), Span(1, TimeStep.DAYS)))
        ),
        group_by=GROUP_BY,
    )
    _, _, catalogue = child_and_control(
        dataset,
        child=measured(0, 150, YEARLY_AXIS),
        control=measured(0, 10_000, YEARLY_AXIS),
        branch=100,
    )

    result = solve(unsupported, catalogue)

    (group,) = result.couldnt_answer.values()
    assert group.status is ExplanationStatusNotOk.MISSING_FEATURE_PLEASE_RAISE_ISSUE
    assert not result.unsatisfied
    # Nothing to enrich: no amount of fetching makes a feature exist.
    assert result.enrichment_inputs == ()


# ------------------------------------------------------------ one hop, and no more


BRANCH_REQUIREMENT = requirement(
    name="zec",
    tree=leaf(Query(variable="tas", experiment="esm-1pct-brch-1000PgC"), "tas")
    .with_lineage(TO_CONTROL)
    .with_constraints(containment()),
    group_by=GROUP_BY,
)
"""A dataset two hops from its control, which `Covers` cannot judge yet"""


def branch_chain_catalogue(dataset, control_end: int):
    """
    Get a ZEC run, the 1pctCO2 run it branched from, and their control

    In piControl's time axis, 1pctCO2 is [100, 250) and the ZEC run is [170, 270).
    """
    return InMemoryCatalogueWithTimeInfo(
        entries=(
            dataset(1, experiment="esm-1pct-brch-1000PgC"),
            dataset(2, experiment="1pctCO2"),
            # Different variant to the child, still linked via the parent link
            dataset(3, experiment="piControl", variant_label="r1i1p2f1"),
        ),
        parents={1: 2, 2: 3, 3: None},
        time_ranges={
            1: measured(0, 100, YEARLY_AXIS),
            2: measured(0, 150, YEARLY_AXIS),
            3: measured(0, control_end, YEARLY_AXIS),
        },
        # The ZEC run branches 70 years into 1pctCO2, which branches 100 years into
        # piControl
        branch_times_in_parent={
            1: branched_at(70, YEARLY_AXIS),
            2: branched_at(100, YEARLY_AXIS),
        },
    )


# Note:
# Settled decision 22, and the two tests PR B2 restores


# These used to assert that the control covered the ZEC run *and* the 1pctCO2 run it
# branched through. The arithmetic for that is easy; the rule is not -- which branch
# point an overlap is measured from, what an intermediate run outlasting its child
# means, what a gapped intermediate does -- and none of it has been written down. So
# this raises until it has been, rather than answering a question nobody has agreed
# the shape of.
@pytest.mark.parametrize("control_end", [300, 260])
def test_a_longer_chain_says_so_rather_than_guessing(dataset, control_end):
    with pytest.raises(NotImplementedError, match="coverage along a chain"):
        solve(BRANCH_REQUIREMENT, branch_chain_catalogue(dataset, control_end))


def test_the_direct_parent_of_a_longer_chain_can_still_be_asked(dataset):
    # Which is what makes the refusal above a limit rather than a wall: the hop which
    # *is* a direct parent is judged as usual.
    one_hop = requirement(
        name="zec",
        tree=leaf(Query(variable="tas", experiment="esm-1pct-brch-1000PgC"), "tas")
        .with_lineage(TO_CONTROL)
        .with_constraints(containment(role="chain.0")),
        group_by=GROUP_BY,
    )

    # The ZEC run is [0, 100) and branches 70 years into 1pctCO2, which runs to 150:
    # it needs [70, 170) and so falls 20 years short.
    result = solve(one_hop, branch_chain_catalogue(dataset, 300))

    assert list(result.unsatisfied) == [ONLY_GROUP]
    assert "needs [70, 170)" in result.explain()


# ------------------------------------------------------ what the solve hands back


def test_a_group_which_needs_enrichment_is_not_unsatisfied(dataset):
    """
    The one assertion this whole step exists to make

    *"Nobody has enriched this yet"* used to be reported in the same bucket as
    *"there is no such data"*, so a reader went looking for data which was there all
    along. It is now a bucket of its own, with what to run to clear it.
    """
    unmeasured = requirement(
        name="tcr",
        tree=leaf(Query(variable="tas", experiment="abrupt-4xCO2"), "tas")
        .with_lineage(TO_CONTROL)
        .with_constraints(containment()),
        group_by=GROUP_BY,
    )
    _, _, catalogue = child_and_control(dataset, child=None, control=None, branch=100)

    result = solve(unmeasured, catalogue)

    assert not result.unsatisfied
    assert not result.satisfied
    (group,) = result.couldnt_answer.values()
    assert group.status is ExplanationStatusNotOk.NEEDS_ENRICHMENT
    assert result.enrichment_inputs == (
        EnrichmentInput(dataset_id=1, missing=MissingTimeFact.TIME_RANGE),
        EnrichmentInput(dataset_id=2, missing=MissingTimeFact.TIME_RANGE),
    )
    assert ExplanationStatusNotOk.NEEDS_ENRICHMENT.value in result.explain()


def test_enrichment_inputs_are_what_the_enricher_takes_back(dataset):
    # The round trip, in one test: enrich what the first solve asked for and the
    # second solve answers. "Enriching" here is recording the ranges, which is all the
    # enricher does to a catalogue.
    unmeasured = requirement(
        name="tcr",
        tree=leaf(Query(variable="tas", experiment="abrupt-4xCO2"), "tas")
        .with_lineage(TO_CONTROL)
        .with_constraints(containment()),
        group_by=GROUP_BY,
    )
    _, _, before = child_and_control(dataset, child=None, control=None, branch=100)

    first = solve(unmeasured, before)
    # What the enricher would have found: the forced run is 150 years long and the
    # control is a long one. Measured off the filenames of exactly the datasets the
    # first solve named, and nothing else.
    measurements = {
        1: measured(0, 150, YEARLY_AXIS),
        2: measured(0, 10_000, YEARLY_AXIS),
    }
    enriched = InMemoryCatalogueWithTimeInfo(
        entries=before.entries,
        parents=before.parents,
        time_ranges={
            each.dataset_id: measurements[each.dataset_id]
            for each in first.enrichment_inputs
        },
        branch_times_in_parent=before.branch_times_in_parent,
    )

    second = solve(unmeasured, enriched)

    assert list(second.satisfied) == [ONLY_GROUP]
    assert not second.couldnt_answer


def test_branch_points_can_still_be_corrected_by_wrapping_the_catalogue(dataset):
    """
    Which a `FIX`-sourced row replaces, and does not yet

    Recorded branch times often have bugs, and until enrichment can store a correction
    the way to apply one is still to override the method which provides it. The
    settled plan is that this becomes a recorded row with a reason and evidence, so
    that a correction is audited and shared rather than re-applied every run.
    """

    class Corrected(InMemoryCatalogueWithTimeInfo):
        def get_branch_time_in_parent(self, entry: CatalogueEntry):
            if entry.id == 1:
                return branched_at(0, YEARLY_AXIS)

            return super().get_branch_time_in_parent(entry)

    child, control, recorded = child_and_control(
        dataset,
        child=measured(0, 150, YEARLY_AXIS),
        control=measured(0, 200, YEARLY_AXIS),
        branch=100,
    )
    corrected = Corrected(
        entries=recorded.entries,
        parents=recorded.parents,
        time_ranges=recorded.time_ranges,
        branch_times_in_parent=recorded.branch_times_in_parent,
    )
    view = lineage_view(child, control)

    assert isinstance(containment().evaluate(view, recorded), Fail)
    assert containment().evaluate(view, corrected) == Pass()


# ----------------------------------------------------------------- written down


def test_a_covers_survives_being_stored_and_read_back():
    """
    Spans have to round trip, because the requirement hash depends on them

    A `Span` is a plain frozen dataclass rather than a pydantic model, so how pydantic
    writes one down is worth pinning: the hash covers the constraint, and a change to
    this JSON is a change to the identity of every requirement using it.
    """
    check = overlap(100, pad=years(10), ideal_pad=years(20))

    assert check.model_dump(mode="json") == {
        "role": "control",
        "target": "self",
        "align": "branch",
        "mode": "overlap",
        "min_overlap": {"count": 100, "step": "years"},
        "pad": [{"count": 10, "step": "years"}, {"count": 10, "step": "years"}],
        "ideal_min_overlap": None,
        "ideal_pad": [{"count": 20, "step": "years"}, {"count": 20, "step": "years"}],
    }

    stored = requirement(
        name="tcr",
        tree=leaf(Query(variable="tas"), "tas")
        .with_lineage(TO_CONTROL)
        .with_constraints(check),
        group_by=GROUP_BY,
    )
    loaded = type(stored).model_validate_json(stored.canonical_json())

    assert loaded.requirement_hash() == stored.requirement_hash()
    assert loaded.tree.constraints[0] == check


def test_a_negative_span_is_refused():
    # A length cannot be negative, and asking for data *before* a point is said at the
    # point of use -- `pad[0]` -- rather than with a negative number. Refused by the
    # span itself, before a check is even built, so this is `NegativeSpanError` rather
    # than something pydantic wrapped.
    with pytest.raises(NegativeSpanError, match="cannot be negative"):
        containment(pad=(Span(-10, YEARS), Span(0, YEARS)))


def test_the_mode_errors_message_survives_pydantic():
    """
    The type does not survive, and the message does

    Our validator raises `CoversModeError`, which pydantic wraps in its own
    `ValidationError` -- the same bargain
    [NotSerialisableConstraintError][esmporium.requirements.NotSerialisableConstraintError]
    already documents. So the thing to rely on, and the thing worth pinning, is that
    what we wrote is still readable in what the user sees.
    """
    assert issubclass(CoversModeError, ValueError)

    with pytest.raises(ValidationError, match="needs `min_overlap`"):
        Covers.model_validate(
            {"role": "control", "align": "branch", "mode": "overlap", "pad": None}
        )
