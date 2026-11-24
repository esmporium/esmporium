"""
Tests of the time value types
"""

from __future__ import annotations

import pytest

from esmporium.datasets.time import (
    BranchPoint,
    EmptyTimeRangeError,
    MismatchedTimeAxisError,
    NegativeSpanError,
    Span,
    TimeAxis,
    TimeRange,
    TimeStep,
    TimeUnits,
    UnknownTimeUnitsError,
    spanned,
    total_months,
    year_and_month,
)

MONTHLY = TimeAxis(TimeUnits(TimeStep.MONTHS))
YEARLY = TimeAxis(TimeUnits(TimeStep.YEARS))
DAILY_NOLEAP = TimeAxis(TimeUnits(TimeStep.DAYS, "1850-01-01"), "noleap")


# ------------------------------------------------------------------ the off-by-ones


def test_a_single_step_dataset_has_an_extent_of_one():
    """
    One month of data is one month

    The case which catches a conversion that subtracted the published values: a file
    named `..._320001-320001.nc` holds January 3200 and nothing else, so 320001 minus
    320001 is the wrong answer.
    """
    january = total_months(3200, 1)

    assert TimeRange(january, january + 1, MONTHLY).extent == Span(1, TimeStep.MONTHS)


def test_yearly_data_from_1850_to_1860_is_eleven_years():
    """
    Both ends are in the data, so there are eleven years and not ten
    """
    assert TimeRange(1850, 1861, YEARLY).extent == Span(11, TimeStep.YEARS)


def test_the_first_month_of_year_one_is_month_one():
    """
    The ordinal is 1-based, which a udunits `since` epoch could not give

    `months since 0001-01` counts elapsed months, so it would make this zero.
    """
    assert total_months(1, 1) == 1


@pytest.mark.parametrize(
    ("year", "month", "expected"),
    [
        (1, 1, 1),
        (1, 12, 12),
        (2, 1, 13),
        (1850, 1, 22189),
        (1861, 1, 22321),
        (3200, 1, 38389),
        (3250, 12, 39000),
    ],
)
def test_total_months(year, month, expected):
    """The ordinal month number of a year and month"""
    assert total_months(year, month) == expected


@pytest.mark.parametrize(
    ("year", "month"), [(1, 1), (1850, 1), (1860, 12), (3250, 12), (0, 6)]
)
def test_year_and_month_round_trips(year, month):
    """Reading an ordinal back gives the year and month it was built from"""
    assert year_and_month(total_months(year, month)) == (year, month)


def test_fifty_one_years_of_monthly_data_is_six_hundred_and_twelve_months():
    """
    The worked example from the design note, start to finish

    A monthly file named `..._320001-325012.nc`.
    """
    published = TimeRange(total_months(3200, 1), total_months(3250, 12) + 1, MONTHLY)

    assert (published.start, published.end_exclusive) == (38389, 39001)
    assert published.extent == Span(612, TimeStep.MONTHS)
    assert published.extent.count // 12 == 51


# ----------------------------------------------------------------------- adjacency


def test_two_files_which_meet_are_adjacent():
    """
    One file's `end_exclusive` being the next one's `start` is what adjacency is
    """
    first = TimeRange(total_months(1850, 1), total_months(1860, 1), MONTHLY)
    second = TimeRange(total_months(1860, 1), total_months(1870, 1), MONTHLY)

    assert first.is_adjacent_to(second)


def test_adjacency_does_not_care_which_file_comes_first():
    """
    Symmetric, so a caller never has to sort first to ask
    """
    first = TimeRange(0, 120, MONTHLY)
    second = TimeRange(120, 240, MONTHLY)

    assert second.is_adjacent_to(first)


def test_one_month_missing_is_not_adjacency():
    """
    The gap this exists to find
    """
    first = TimeRange(0, 120, MONTHLY)
    after_a_gap = TimeRange(121, 240, MONTHLY)

    assert not first.is_adjacent_to(after_a_gap)


def test_an_overlap_is_not_adjacency():
    """
    Two files covering the same month are as wrong as a month nobody published
    """
    first = TimeRange(0, 120, MONTHLY)
    overlapping = TimeRange(119, 240, MONTHLY)

    assert not first.is_adjacent_to(overlapping)


# ---------------------------------------------------------------------- containment


@pytest.mark.parametrize(
    ("covering", "covered", "expected"),
    [
        ((0, 240), (0, 240), True),
        ((0, 240), (12, 228), True),
        ((0, 240), (0, 241), False),
        ((1, 240), (0, 240), False),
    ],
)
def test_contains(covering, covered, expected):
    """One range covering the whole of another"""
    assert (
        TimeRange(*covering, MONTHLY).contains(TimeRange(*covered, MONTHLY)) is expected
    )


# ---------------------------------------------------------------------------- axes


def test_ranges_on_different_units_cannot_be_compared():
    """
    Months and years are different numbers, so comparing them is refused out loud
    """
    monthly = TimeRange(0, 240, MONTHLY)
    yearly = TimeRange(0, 20, YEARLY)

    with pytest.raises(MismatchedTimeAxisError):
        monthly.contains(yearly)


def test_ranges_in_different_calendars_cannot_be_compared():
    """
    The same day count means different spans in different calendars
    """
    noleap = TimeRange(0, 3650, DAILY_NOLEAP)
    gregorian = TimeRange(
        0, 3650, TimeAxis(TimeUnits(TimeStep.DAYS, "1850-01-01"), "standard")
    )

    with pytest.raises(MismatchedTimeAxisError):
        noleap.contains(gregorian)


def test_a_dated_range_which_does_not_know_its_calendar_is_not_compared():
    """
    Not even with itself: a day count means nothing until its calendar is known
    """
    unknown = TimeRange(0, 3650, TimeAxis(TimeUnits(TimeStep.DAYS, "1850-01-01")))

    with pytest.raises(MismatchedTimeAxisError):
        unknown.contains(unknown)


def test_a_natural_range_is_not_compared_with_a_dated_one():
    """
    A month count and a day count are related only through a calendar, by a
    conversion
    """
    with pytest.raises(MismatchedTimeAxisError, match="convert_position"):
        TimeRange(0, 240, MONTHLY).contains(TimeRange(0, 3650, DAILY_NOLEAP))


def test_a_range_must_hold_at_least_one_step():
    """
    `end_exclusive` is the first step *not* in the data, so it comes after `start`
    """
    with pytest.raises(EmptyTimeRangeError):
        TimeRange(240, 240, MONTHLY)


# --------------------------------------------------------------------------- units


@pytest.mark.parametrize(
    ("units", "rendered"),
    [
        (TimeUnits(TimeStep.MONTHS), "months"),
        (TimeUnits(TimeStep.YEARS), "years"),
        (TimeUnits(TimeStep.DAYS, "1850-01-01"), "days since 1850-01-01"),
        (
            TimeUnits(TimeStep.HOURS, "0001-01-01 00:00:00"),
            "hours since 0001-01-01 00:00:00",
        ),
    ],
)
def test_units_round_trip_through_their_own_spelling(units, rendered):
    """What a `units` column stores, and reading it back"""
    assert str(units) == rendered
    assert TimeUnits.parse(rendered) == units


@pytest.mark.parametrize("text", ["month", "day since 1850-1-1", "  months  "])
def test_units_accept_the_spellings_files_use(text):
    """Singular names and stray whitespace, both of which udunits allows"""
    assert TimeUnits.parse(text).step in (TimeStep.MONTHS, TimeStep.DAYS)


@pytest.mark.parametrize(
    "text",
    [
        "fortnights since 1850-01-01",
        "seconds",
        "days since",
        "days since ",
        "since 1850-01-01",
        "",
    ],
)
def test_unreadable_units_say_so(text):
    """
    Anything else is refused rather than guessed at

    `"days since "` is in here on its own account: it names a step and announces an
    epoch, then does not give one, which is the one shape that could have been read
    as natural units by accident.
    """
    with pytest.raises(UnknownTimeUnitsError):
        TimeUnits.parse(text)


def test_natural_units_have_no_epoch():
    """
    Which is what makes them an ordinal, and why they need no calendar
    """
    assert TimeUnits(TimeStep.MONTHS).is_natural
    assert not TimeUnits(TimeStep.DAYS, "1850-01-01").is_natural


@pytest.mark.parametrize(
    ("step", "expected"),
    [
        (TimeStep.YEARS, True),
        (TimeStep.MONTHS, True),
        (TimeStep.DAYS, False),
        (TimeStep.HOURS, False),
        (TimeStep.MINUTES, False),
    ],
)
def test_which_steps_are_the_same_in_every_calendar(step, expected):
    """
    The line the whole enrichment step is organised around
    """
    assert step.is_calendar_free is expected


# --------------------------------------------------------------------------- spans


def test_a_span_is_a_length_so_it_cannot_be_negative():
    """
    Direction is said at the point of use, not with a negative length
    """
    with pytest.raises(NegativeSpanError):
        Span(-1, TimeStep.YEARS)


def test_a_span_of_zero_is_fine():
    """
    "No padding required" is a coherent thing to ask for
    """
    assert Span(0, TimeStep.YEARS).count == 0


@pytest.mark.parametrize(
    ("span", "rendered"),
    [
        (Span(20, TimeStep.YEARS), "20 years"),
        (Span(1, TimeStep.YEARS), "1 year"),
        (Span(3, TimeStep.HOURS), "3 hours"),
    ],
)
def test_spans_read_as_english(span, rendered):
    """Because they end up in messages a user reads"""
    assert str(span) == rendered


# -------------------------------------------------------------------------- spanned


def test_spanned_is_earliest_start_to_latest_end():
    """
    And says nothing about whether the ranges fill it
    """
    across = spanned([TimeRange(120, 240, MONTHLY), TimeRange(0, 60, MONTHLY)])

    assert (across.start, across.end_exclusive) == (0, 240)


def test_spanning_nothing_is_refused():
    """
    There is no such thing as the span of no ranges
    """
    with pytest.raises(ValueError, match="At least one time range"):
        spanned([])


def test_spanning_ranges_on_different_axes_is_refused():
    """
    Which is a fact about the publication, not about the question
    """
    with pytest.raises(MismatchedTimeAxisError):
        spanned([TimeRange(0, 240, MONTHLY), TimeRange(0, 20, YEARLY)])


# --------------------------------------------------------------------- branch point


def test_a_branch_point_keeps_a_published_float_as_published():
    """
    `branch_time_in_parent` on a dated axis is not rounded to a whole day
    """
    branch = BranchPoint(
        in_parent=438000.5,
        parent_axis=DAILY_NOLEAP,
        in_child=0.0,
        child_axis=DAILY_NOLEAP,
    )

    assert (branch.in_parent, branch.in_child) == (438000.5, 0.0)


def test_a_branch_point_need_not_know_where_it_is_in_the_child():
    """
    CMIP5 does not publish it
    """
    branch = BranchPoint(in_parent=0, parent_axis=MONTHLY)

    assert (branch.in_child, branch.child_axis) == (None, None)
