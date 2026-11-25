"""
Tests of [Covers][esmporium.requirements.Covers]

Everything here is in **years** on a yearly axis unless the test says otherwise, so
that the numbers in an assertion are the numbers in the picture
`docs/further-background/enrichment.md` draws. The monthly axis -- years asked for,
months recorded, which is what real monthly data takes -- has its own section.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from esmporium.datasets import (
    BranchPoint,
    NegativeSpanError,
    Position,
    Span,
    TimeAxis,
    TimeStep,
    TimeUnits,
)
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
    recorded_range,
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
        "mode": "containment",
        "pad": NO_PAD,
        **overrides,
    }

    return Covers(**settings)


def overlap(count: int, **overrides) -> Covers:
    """Get an overlap check on the control asking for `count` years from the branch"""
    settings = {
        "role": "control",
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


def child_and_control(  # noqa: PLR0913 - one argument per fact a test can leave out
    dataset,
    *,
    child,
    control,
    branch=None,
    axis: TimeAxis = YEARLY_AXIS,
    child_axis: TimeAxis | None = None,
):
    """
    Get a child, its control and a catalogue holding them

    `child` and `control` are recorded ranges and `branch` where the child branched
    on the control's axis, or `None` for "nothing recorded it" -- which is how a test
    says a fact is missing rather than wrong. The child branched at 0 on its own axis,
    which is `axis` unless `child_axis` says otherwise.
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
        branch_points={}
        if branch is None
        else {1: branched_at(branch, axis, child_axis=child_axis)},
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
        child=recorded_range(0, 150, YEARLY_AXIS),
        control=recorded_range(0, 200, YEARLY_AXIS),
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
        child=recorded_range(0, 150, YEARLY_AXIS),
        control=recorded_range(150, 400, YEARLY_AXIS),
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
        child=recorded_range(0, 50, YEARLY_AXIS),
        control=recorded_range(0, 10_000, YEARLY_AXIS),
        branch=100,
    )

    outcome = overlap(100).evaluate(lineage_view(child, control), catalogue)

    assert isinstance(outcome, Fail)
    # The message names the run which fell short, and not the one which did not.
    assert outcome.message == (
        "tas: #1's data runs to 150 on #2's axis, short of the 100 years of overlap "
        "wanted from 100, which runs to 200, in years"
    )


def test_when_both_runs_fall_short_both_are_named(dataset):
    child, control, catalogue = child_and_control(
        dataset,
        child=recorded_range(0, 50, YEARLY_AXIS),
        control=recorded_range(0, 150, YEARLY_AXIS),
        branch=100,
    )

    outcome = overlap(100).evaluate(lineage_view(child, control), catalogue)

    assert isinstance(outcome, Fail)
    assert "#2 covers [0, 150) and needs [100, 200)" in outcome.message
    assert "#1's data runs to 150 on #2's axis" in outcome.message


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
                    1: recorded_range(0, 150, YEARLY_AXIS),
                    2: recorded_range(0, 250, YEARLY_AXIS),
                },
                branch_points=catalogue.branch_points,
            ),
        )
        == Pass()
    )


def test_containment_refuses_a_min_overlap_and_overlap_requires_one():
    # Two halves of one rule: the mode says which question is being asked, so the
    # thresholds which belong to the other one are a mistake rather than ignored.
    with pytest.raises(ValidationError, match="needs `min_overlap`"):
        Covers(role="control", mode="overlap", pad=NO_PAD)

    with pytest.raises(ValidationError, match="takes no `min_overlap`"):
        Covers(
            role="control",
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
    assert missing == {"mode", "pad"}
    assert Covers(role="control", mode="containment", pad=NO_PAD).target == ("self")


# ------------------------------------------------------------- naming the two runs


def test_covers_needs_a_lineage():
    outcome = containment().evaluate(
        ConstraintView(prefix="", roles={}, lineages={}),
        InMemoryCatalogueWithTimeInfo(entries=()),
    )

    assert outcome == Fail("Covers needs a lineage, but none was resolved")


def test_covers_branch_needs_ancestor(dataset):
    child, control, catalogue = child_and_control(
        dataset,
        child=recorded_range(0, 10, YEARLY_AXIS),
        control=recorded_range(0, 10, YEARLY_AXIS),
        branch=0,
    )

    with pytest.raises(ValueError, match="must be an ancestor"):
        Covers(
            role="self",
            target="control",
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
        child=recorded_range(0, 10, YEARLY_AXIS),
        control=recorded_range(0, 5, YEARLY_AXIS),
        branch=0,
    )
    view = lineage_view(child, control)

    by_end = containment(role="end").evaluate(view, catalogue)
    by_name = containment(role="control").evaluate(view, catalogue)

    assert isinstance(by_end, Fail)
    assert by_end == by_name


# --------------------------------------------------------------- the soft thresholds


def test_an_ideal_overlap_the_covered_run_cannot_supply_says_so(dataset):
    child, control, catalogue = child_and_control(
        dataset,
        child=recorded_range(0, 120, YEARLY_AXIS),
        control=recorded_range(0, 1000, YEARLY_AXIS),
        branch=100,
    )
    check = overlap(100, ideal_min_overlap=Span(150, YEARS))

    outcome = check.evaluate(lineage_view(child, control), catalogue)

    assert isinstance(outcome, Degraded)
    assert outcome.message == (
        "tas: #1's data runs to 220 on #2's axis, short of the 150 years of overlap "
        "ideally wanted from 100, which runs to 250, in years"
    )


def test_ideal_padding_not_met_is_degraded(dataset):
    # Needs [100, 250), so ideal padding of 20 years either side needs [80, 270).
    child, control, catalogue = child_and_control(
        dataset,
        child=recorded_range(0, 150, YEARLY_AXIS),
        control=recorded_range(90, 1000, YEARLY_AXIS),
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
        Covers(role="control", mode="containment", pad=None)


def test_degraded_is_kept_as_a_note(dataset):
    _, _, catalogue = child_and_control(
        dataset,
        child=recorded_range(0, 150, YEARLY_AXIS),
        control=recorded_range(90, 1000, YEARLY_AXIS),
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


# ----------------------------------------------------- years asked, months recorded


def test_a_requirement_in_years_against_a_monthly_axis(dataset):
    """
    The conversion real monthly data takes, and the one which has to stay exact

    Twelve months is a year in every CF calendar, so this needs no calendar and no
    `cftime`: 20 years of padding is 240 months, exactly, whichever calendar the data
    turns out to be in.
    """
    child, control, catalogue = child_and_control(
        dataset,
        child=recorded_range(0, 1800, MONTHLY_AXIS),
        control=recorded_range(960, 3240, MONTHLY_AXIS),
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
        EnrichmentInput(dataset_version_id=2, missing=MissingTimeFact.CALENDAR),
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
def test_an_unenriched_range_needs_enrichment(dataset, missing, expected):
    """
    The distinction the whole enrichment step exists for

    Nobody has recorded this, which is not the same as there being no data -- and
    before this it was reported in the same bucket as there being none.
    """
    child, control, catalogue = child_and_control(
        dataset,
        child=None
        if missing in ("child_range", "both_ranges")
        else recorded_range(0, 150, YEARLY_AXIS),
        control=None
        if missing in ("control_range", "both_ranges")
        else recorded_range(0, 250, YEARLY_AXIS),
        branch=100,
    )

    outcome = containment().evaluate(lineage_view(child, control), catalogue)

    assert isinstance(outcome, NeedsEnrichment)
    assert outcome.inputs == tuple(
        EnrichmentInput(dataset_version_id=entry_id, missing=MissingTimeFact.TIME_RANGE)
        for entry_id in expected
    )


def test_an_unrecorded_branch_point_needs_enrichment(dataset):
    # A CMIP5 branch time is published without units, so it cannot be interpreted as
    # it stands and the dataset answers `None` until a fix records the value.
    child, control, catalogue = child_and_control(
        dataset,
        child=recorded_range(0, 150, YEARLY_AXIS),
        control=recorded_range(0, 250, YEARLY_AXIS),
        branch=None,
    )

    outcome = containment().evaluate(lineage_view(child, control), catalogue)

    assert isinstance(outcome, NeedsEnrichment)
    assert outcome.inputs == (
        EnrichmentInput(dataset_version_id=1, missing=MissingTimeFact.BRANCH_POINT),
    )


def test_a_branch_point_on_a_different_axis_to_its_parent_fails(dataset):
    # The branch point and the parent's own data disagreeing about which axis the
    # parent is on is the data disagreeing with itself, not a question of alignment.
    child, control, catalogue = child_and_control(
        dataset,
        child=recorded_range(0, 150, YEARLY_AXIS),
        control=recorded_range(0, 250, YEARLY_AXIS),
        branch=100,
        axis=MONTHLY_AXIS,
    )

    outcome = containment().evaluate(lineage_view(child, control), catalogue)

    assert isinstance(outcome, Fail)
    assert "have to be the same axis" in outcome.message


def test_a_child_and_parent_at_different_frequencies_is_not_supported_yet(dataset):
    child, control, catalogue = child_and_control(
        dataset,
        child=recorded_range(0, 1800, MONTHLY_AXIS),
        control=recorded_range(0, 250, YEARLY_AXIS),
        branch=100,
        child_axis=MONTHLY_AXIS,
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
        child=recorded_range(0, 150, YEARLY_AXIS, contiguous=gapped != "child"),
        control=recorded_range(0, 10_000, YEARLY_AXIS, contiguous=gapped != "control"),
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
        child=recorded_range(0, 150, YEARLY_AXIS),
        control=recorded_range(0, 10_000, YEARLY_AXIS),
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
        child=recorded_range(0, 150, YEARLY_AXIS),
        control=recorded_range(0, 10_000, YEARLY_AXIS),
        branch=100,
    )

    result = solve(unsupported, catalogue)

    (group,) = result.couldnt_answer.values()
    assert group.status is ExplanationStatusNotOk.MISSING_FEATURE_PLEASE_RAISE_ISSUE
    assert not result.unsatisfied
    # Nothing to enrich: no amount of fetching makes a feature exist.
    assert result.enrichment_inputs == ()


# ------------------------------------------- where the child branched, on its axis


def branched_pair(dataset, *, child, control, point, child_project="CMIP6"):
    """
    Get a child, its control and a catalogue, with the branch point given whole

    For the tests which need to say something about the branch point that
    `branched_at` does not, or a child from another project.
    """
    child_entry = dataset(1, project=child_project)
    control_entry = dataset(2, experiment="piControl")
    catalogue = InMemoryCatalogueWithTimeInfo(
        entries=(child_entry, control_entry),
        parents={1: 2, 2: None},
        time_ranges={1: child, 2: control},
        branch_points={1: point},
    )

    return child_entry, control_entry, catalogue


def test_the_child_is_mapped_through_where_it_branched(dataset):
    """
    Not through where its data starts: those are different instants here

    The child holds [0, 150) but branched 10 years in, so its end is 140 years after
    the branch, and lands at 100 + 140 = 240 on the control's axis.
    """
    child, control, catalogue = child_and_control(
        dataset,
        child=recorded_range(0, 150, YEARLY_AXIS),
        control=recorded_range(0, 200, YEARLY_AXIS),
        branch=None,
    )
    catalogue = InMemoryCatalogueWithTimeInfo(
        entries=catalogue.entries,
        parents=catalogue.parents,
        time_ranges=catalogue.time_ranges,
        branch_points={1: branched_at(100, YEARLY_AXIS, in_child=10)},
    )

    outcome = containment().evaluate(lineage_view(child, control), catalogue)

    assert isinstance(outcome, Fail)
    assert "needs [100, 240)" in outcome.message


def test_a_missing_leading_file_fails_rather_than_shifting_everything(dataset):
    """
    The child branched at 0 and its data starts at 10, so its first files are missing

    Mapping through its first data step instead would quietly move every position
    above it by ten years, which is the worst way for a stitch to go wrong.
    """
    child, control, catalogue = child_and_control(
        dataset,
        child=recorded_range(10, 160, YEARLY_AXIS),
        control=recorded_range(0, 1000, YEARLY_AXIS),
        branch=100,
    )

    outcome = containment().evaluate(lineage_view(child, control), catalogue)

    assert isinstance(outcome, Fail)
    assert "#1 branched at 0 on its own axis" in outcome.message
    assert "outside the [10, 160) it holds" in outcome.message


def test_a_cmip5_child_is_assumed_to_start_at_its_branch_and_says_so(dataset):
    # CMIP5 publishes no `branch_time_in_child`, so the first time bound, 10, stands
    # in: the end, 160, is 150 years after it, and lands at 250.
    child, control, catalogue = branched_pair(
        dataset,
        child=recorded_range(10, 160, YEARLY_AXIS),
        control=recorded_range(0, 250, YEARLY_AXIS),
        point=BranchPoint(in_parent=Position(100, YEARLY_AXIS)),
        child_project="CMIP5",
    )

    outcome = containment().evaluate(lineage_view(child, control), catalogue)

    # A pass which rests on an assumption is still a pass, and names it.
    assert isinstance(outcome, Pass)
    assert "assumed to be #1's first time bound, 10 in years" in outcome.message
    assert "a fix which states `branch_time_in_child`" in outcome.message


def test_a_failure_which_rests_on_the_assumption_names_it_too(dataset):
    child, control, catalogue = branched_pair(
        dataset,
        child=recorded_range(10, 160, YEARLY_AXIS),
        control=recorded_range(0, 200, YEARLY_AXIS),
        point=BranchPoint(in_parent=Position(100, YEARLY_AXIS)),
        child_project="CMIP5",
    )

    outcome = containment().evaluate(lineage_view(child, control), catalogue)

    assert isinstance(outcome, Fail)
    assert "needs [100, 250)" in outcome.message
    assert "assumed to be #1's first time bound" in outcome.message


def test_the_assumption_reaches_the_solutions_notes(dataset):
    _, _, catalogue = branched_pair(
        dataset,
        child=recorded_range(10, 160, YEARLY_AXIS),
        control=recorded_range(0, 250, YEARLY_AXIS),
        point=BranchPoint(in_parent=Position(100, YEARLY_AXIS)),
        child_project="CMIP5",
    )
    contained = requirement(
        name="tcr",
        tree=leaf(Query(variable="tas", experiment="abrupt-4xCO2"), "tas")
        .with_lineage(TO_CONTROL)
        .with_constraints(containment()),
        group_by=GROUP_BY,
    )

    (solution,) = solve(contained, catalogue).satisfied.values()

    assert len(solution.notes) == 1
    assert "assumed to be #1's first time bound" in solution.notes[0]


def test_a_fix_which_states_where_a_cmip5_child_branched_replaces_the_assumption(
    dataset,
):
    # With `in_child` given there is nothing to assume, so nothing is said, and the
    # given value is what the mapping uses: 160 is 160 years after 0, so 260.
    child, control, catalogue = branched_pair(
        dataset,
        child=recorded_range(10, 160, YEARLY_AXIS),
        control=recorded_range(0, 260, YEARLY_AXIS),
        point=BranchPoint(
            in_parent=Position(100, YEARLY_AXIS), in_child=Position(0, YEARLY_AXIS)
        ),
        child_project="CMIP5",
    )

    outcome = containment().evaluate(lineage_view(child, control), catalogue)

    # The fix says the run branched at 0 and its data starts at 10, so its leading
    # files are missing, which the assumption would have hidden.
    assert isinstance(outcome, Fail)
    assert "#1 branched at 0 on its own axis" in outcome.message
    assert "assumed" not in outcome.message


def test_a_fix_which_agrees_with_the_assumption_passes_without_a_note(dataset):
    child, control, catalogue = branched_pair(
        dataset,
        child=recorded_range(0, 150, YEARLY_AXIS),
        control=recorded_range(0, 250, YEARLY_AXIS),
        point=BranchPoint(
            in_parent=Position(100, YEARLY_AXIS), in_child=Position(0, YEARLY_AXIS)
        ),
        child_project="CMIP5",
    )

    assert containment().evaluate(lineage_view(child, control), catalogue) == Pass()


def test_a_cmip6_child_without_where_it_branched_needs_enrichment(dataset):
    """
    CMIP6 publishes it, so a branch point without it has not been recorded yet
    """
    child, control, catalogue = branched_pair(
        dataset,
        child=recorded_range(0, 150, YEARLY_AXIS),
        control=recorded_range(0, 250, YEARLY_AXIS),
        point=BranchPoint(in_parent=Position(100, YEARLY_AXIS)),
    )

    outcome = containment().evaluate(lineage_view(child, control), catalogue)

    assert isinstance(outcome, NeedsEnrichment)
    assert outcome.inputs == (
        EnrichmentInput(dataset_version_id=1, missing=MissingTimeFact.BRANCH_POINT),
    )


def test_a_child_from_a_project_we_know_nothing_about_is_not_supported(dataset):
    child, control, catalogue = branched_pair(
        dataset,
        child=recorded_range(0, 150, YEARLY_AXIS),
        control=recorded_range(0, 250, YEARLY_AXIS),
        point=BranchPoint(in_parent=Position(100, YEARLY_AXIS)),
        child_project="MIP1",
    )

    outcome = containment().evaluate(lineage_view(child, control), catalogue)

    assert isinstance(outcome, MissingFeature)
    assert "BRANCH_TIME_IN_CHILD_BY_PROJECT" in outcome.message


def test_the_child_end_of_a_branch_point_off_the_childs_axis_fails(dataset):
    # The same disagreement as the parent end's, at the other end.
    child, control, catalogue = child_and_control(
        dataset,
        child=recorded_range(0, 150, YEARLY_AXIS),
        control=recorded_range(0, 250, YEARLY_AXIS),
        branch=100,
        child_axis=MONTHLY_AXIS,
    )

    outcome = containment().evaluate(lineage_view(child, control), catalogue)

    assert isinstance(outcome, Fail)
    assert "the child end of #1's branch point" in outcome.message
    assert "have to be the same axis" in outcome.message


NOLEAP_SINCE_1850 = TimeAxis(TimeUnits(TimeStep.DAYS, "1850-01-01"), "noleap")
"""A child's axis, as a CMIP6 header gives it"""

NOLEAP_SINCE_0001 = TimeAxis(TimeUnits(TimeStep.DAYS, "0001-01-01"), "noleap")
"""Its control's: the same calendar, counted from a different date"""


def test_a_child_and_parent_counted_from_different_dates_line_up(dataset):
    """
    The epochs cancel, because what crosses from one axis to the other is a length

    The child's end is 60225 days after it branched, so it lands 60225 days after
    the control's 182500, at 242725.
    """
    pytest.importorskip("cftime", reason="the optional `calendars` extra")
    point = BranchPoint(
        in_parent=Position(182500, NOLEAP_SINCE_0001),
        in_child=Position(0, NOLEAP_SINCE_1850),
    )
    child, control, enough = branched_pair(
        dataset,
        child=recorded_range(0, 60225, NOLEAP_SINCE_1850),
        control=recorded_range(0, 300_000, NOLEAP_SINCE_0001),
        point=point,
    )
    _, _, short = branched_pair(
        dataset,
        child=recorded_range(0, 60225, NOLEAP_SINCE_1850),
        control=recorded_range(0, 200_000, NOLEAP_SINCE_0001),
        point=point,
    )
    view = lineage_view(child, control)

    assert containment().evaluate(view, enough) == Pass()
    outcome = containment().evaluate(view, short)
    assert isinstance(outcome, Fail)
    assert "needs [182500, 242725), in days since 0001-01-01" in outcome.message


def test_a_child_and_parent_in_different_calendars_is_not_supported_yet(dataset):
    in_360_days = TimeAxis(TimeUnits(TimeStep.DAYS, "0001-01-01"), "360_day")
    child, control, catalogue = branched_pair(
        dataset,
        child=recorded_range(0, 60225, NOLEAP_SINCE_1850),
        control=recorded_range(0, 300_000, in_360_days),
        point=BranchPoint(
            in_parent=Position(180_000, in_360_days),
            in_child=Position(0, NOLEAP_SINCE_1850),
        ),
    )

    outcome = containment().evaluate(lineage_view(child, control), catalogue)

    assert isinstance(outcome, MissingFeature)
    assert "is in calendar 'noleap' and #2 in '360_day'" in outcome.message


def test_a_natural_child_and_a_dated_parent_is_not_supported_yet(dataset):
    """
    Both count months, one as a plain ordinal and one from a date

    The message says that, rather than that one of them has no calendar.
    """
    dated_months = TimeAxis(TimeUnits(TimeStep.MONTHS, "1850-01-01"), "noleap")
    child, control, catalogue = branched_pair(
        dataset,
        child=recorded_range(0, 1200, MONTHLY_AXIS),
        control=recorded_range(0, 12_000, dated_months),
        point=BranchPoint(
            in_parent=Position(1200, dated_months),
            in_child=Position(0, MONTHLY_AXIS),
        ),
    )

    outcome = containment().evaluate(lineage_view(child, control), catalogue)

    assert isinstance(outcome, MissingFeature)
    assert "#1 counts months as a plain ordinal" in outcome.message
    assert "calendar None" not in outcome.message


# ------------------------------------------------------------------- along a chain


BRANCH_REQUIREMENT = requirement(
    name="zec",
    tree=leaf(Query(variable="tas", experiment="esm-1pct-brch-1000PgC"), "tas")
    .with_lineage(TO_CONTROL)
    .with_constraints(containment()),
    group_by=GROUP_BY,
)
"""A dataset two hops from its control"""


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
            1: recorded_range(0, 100, YEARLY_AXIS),
            2: recorded_range(0, 150, YEARLY_AXIS),
            3: recorded_range(0, control_end, YEARLY_AXIS),
        },
        # The ZEC run branches 70 years into 1pctCO2, which branches 100 years into
        # piControl
        branch_points={
            1: branched_at(70, YEARLY_AXIS),
            2: branched_at(100, YEARLY_AXIS),
        },
    )


def test_branch_chain_back_to_the_control(dataset):
    result = solve(BRANCH_REQUIREMENT, branch_chain_catalogue(dataset, 300))

    (group,) = result.satisfied.values()
    assert group.one("chain.0.tas").experiment == "1pctCO2"
    assert group.one("control.tas").variant_label == "r1i1p2f1"


def test_branch_chain_control_must_cover_child_and_parents(dataset):
    # Covering the ZEC run alone, [170, 270), would be enough at 270, but the analysis
    # needs the control over 1pctCO2 too, so the window starts where 1pctCO2 branched.
    result = solve(BRANCH_REQUIREMENT, branch_chain_catalogue(dataset, 260))

    assert list(result.unsatisfied) == [ONLY_GROUP]
    assert "needs [100, 270)" in result.explain()


def test_a_hop_of_the_chain_can_be_asked_about_on_its_own(dataset):
    # The chain is what `role` and `target` name, not the whole lineage: naming the
    # direct parent asks about that hop alone.
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


def scenario_chain(dataset, scenario_branch: int):
    """
    Get a scenario, the historical run it branched from, their control, and what a
    check on all three sees

    In years: historical holds [0, 165) and branched 100 years into the control; the
    scenario holds [0, 86) and branched `scenario_branch` years into historical.
    """
    scenario = dataset(1, experiment="ssp585")
    historical = dataset(2, experiment="historical")
    control = dataset(3, experiment="piControl")
    catalogue = InMemoryCatalogueWithTimeInfo(
        entries=(scenario, historical, control),
        parents={1: 2, 2: 3, 3: None},
        time_ranges={
            1: recorded_range(0, 86, YEARLY_AXIS),
            2: recorded_range(0, 165, YEARLY_AXIS),
            3: recorded_range(0, 1000, YEARLY_AXIS),
        },
        branch_points={
            1: branched_at(scenario_branch, YEARLY_AXIS),
            2: branched_at(100, YEARLY_AXIS),
        },
    )

    return lineage_view(scenario, historical, control), catalogue


def test_a_run_which_branches_where_its_parent_stops_joins_up(dataset):
    """
    ssp585 branches off historical on the first instant after historical's data

    165 is historical's `end_exclusive`, which it does not *hold* -- but its data
    runs right up to it, which is all a parent has to do at a join.
    """
    view, catalogue = scenario_chain(dataset, 165)

    assert containment().evaluate(view, catalogue) == Pass()


def test_a_run_which_branches_after_its_parent_stops_breaks_the_series(dataset):
    # One year past historical's end is a year nobody has data for.
    view, catalogue = scenario_chain(dataset, 166)

    outcome = containment().evaluate(view, catalogue)

    assert isinstance(outcome, Fail)
    assert "#1 branched from #2 at 166, outside #2's data" in outcome.message


def test_a_broken_series_fails_before_an_unsupported_calendar_is_reported(dataset):
    """
    `Fail` outranks `MissingFeature`, as it does when the outcomes are combined

    The scenario's data starts after it branched, which no feature would fix, and
    historical and the control are in different calendars, which one would.
    """
    in_360_days = TimeAxis(TimeUnits(TimeStep.DAYS, "0001-01-01"), "360_day")
    noleap_since_2015 = TimeAxis(TimeUnits(TimeStep.DAYS, "2015-01-01"), "noleap")
    scenario = dataset(1, experiment="ssp585")
    historical = dataset(2, experiment="historical")
    control = dataset(3, experiment="piControl")
    catalogue = InMemoryCatalogueWithTimeInfo(
        entries=(scenario, historical, control),
        parents={1: 2, 2: 3, 3: None},
        time_ranges={
            1: recorded_range(3650, 31390, noleap_since_2015),
            2: recorded_range(0, 60225, NOLEAP_SINCE_1850),
            3: recorded_range(0, 300_000, in_360_days),
        },
        branch_points={
            1: BranchPoint(
                in_parent=Position(60225, NOLEAP_SINCE_1850),
                in_child=Position(0, noleap_since_2015),
            ),
            2: BranchPoint(
                in_parent=Position(180_000, in_360_days),
                in_child=Position(0, NOLEAP_SINCE_1850),
            ),
        },
    )

    outcome = containment().evaluate(
        lineage_view(scenario, historical, control), catalogue
    )

    assert isinstance(outcome, Fail)
    assert "#1 branched at 0 on its own axis" in outcome.message


# ------------------------------------------------------ what the solve hands back


def test_a_group_which_needs_enrichment_is_not_unsatisfied(dataset):
    """
    The one assertion this whole step exists to make

    *"Nobody has enriched this yet"* used to be reported in the same bucket as
    *"there is no such data"*, so a reader went looking for data which was there all
    along. It is now a bucket of its own, with what to run to clear it.
    """
    unenriched = requirement(
        name="tcr",
        tree=leaf(Query(variable="tas", experiment="abrupt-4xCO2"), "tas")
        .with_lineage(TO_CONTROL)
        .with_constraints(containment()),
        group_by=GROUP_BY,
    )
    _, _, catalogue = child_and_control(dataset, child=None, control=None, branch=100)

    result = solve(unenriched, catalogue)

    assert not result.unsatisfied
    assert not result.satisfied
    (group,) = result.couldnt_answer.values()
    assert group.status is ExplanationStatusNotOk.NEEDS_ENRICHMENT
    assert result.enrichment_inputs == (
        EnrichmentInput(dataset_version_id=1, missing=MissingTimeFact.TIME_RANGE),
        EnrichmentInput(dataset_version_id=2, missing=MissingTimeFact.TIME_RANGE),
    )
    assert ExplanationStatusNotOk.NEEDS_ENRICHMENT.value in result.explain()


def test_enrichment_inputs_are_what_the_enricher_takes_back(dataset):
    # The round trip, in one test: enrich what the first solve asked for and the
    # second solve answers. "Enriching" here is recording the ranges, which is all the
    # enricher does to a catalogue.
    unenriched = requirement(
        name="tcr",
        tree=leaf(Query(variable="tas", experiment="abrupt-4xCO2"), "tas")
        .with_lineage(TO_CONTROL)
        .with_constraints(containment()),
        group_by=GROUP_BY,
    )
    _, _, before = child_and_control(dataset, child=None, control=None, branch=100)

    first = solve(unenriched, before)
    # What the enricher would have found: the forced run is 150 years long and the
    # control is a long one. Measured off the filenames of exactly the datasets the
    # first solve named, and nothing else.
    enrichments = {
        1: recorded_range(0, 150, YEARLY_AXIS),
        2: recorded_range(0, 10_000, YEARLY_AXIS),
    }
    enriched = InMemoryCatalogueWithTimeInfo(
        entries=before.entries,
        parents=before.parents,
        time_ranges={
            each.dataset_version_id: enrichments[each.dataset_version_id]
            for each in first.enrichment_inputs
        },
        branch_points=before.branch_points,
    )

    second = solve(unenriched, enriched)

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
        def get_branch_point(self, entry: CatalogueEntry):
            if entry.id == 1:
                return branched_at(0, YEARLY_AXIS)

            return super().get_branch_point(entry)

    child, control, recorded = child_and_control(
        dataset,
        child=recorded_range(0, 150, YEARLY_AXIS),
        control=recorded_range(0, 200, YEARLY_AXIS),
        branch=100,
    )
    corrected = Corrected(
        entries=recorded.entries,
        parents=recorded.parents,
        time_ranges=recorded.time_ranges,
        branch_points=recorded.branch_points,
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
        Covers.model_validate({"role": "control", "mode": "overlap", "pad": None})
