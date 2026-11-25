"""
Tests of the time value types
"""

from __future__ import annotations

import pytest

from esmporium.datasets.time import (
    BranchPoint,
    EmptyTimeRangeError,
    FractionalCalendarFreeDeltaError,
    MismatchedTimeAxisError,
    NeedsAnAnchorError,
    NotAWholeStepError,
    TimeAxis,
    TimeDelta,
    TimePoint,
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

    assert TimeRange.from_values(january, january + 1, MONTHLY).extent == TimeDelta(
        1, TimeStep.MONTHS
    )


def test_yearly_data_from_1850_to_1860_is_eleven_years():
    """
    Both ends are in the data, so there are eleven years and not ten
    """
    assert TimeRange.from_values(1850, 1861, YEARLY).extent == TimeDelta(
        11, TimeStep.YEARS
    )


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
    published = TimeRange.from_values(
        total_months(3200, 1), total_months(3250, 12) + 1, MONTHLY
    )

    assert (published.start.value, published.end_exclusive.value) == (38389, 39001)
    assert published.extent == TimeDelta(612, TimeStep.MONTHS)
    assert published.extent.count // 12 == 51


# ----------------------------------------------------------------------- adjacency


def test_two_files_which_meet_are_adjacent():
    """
    One file's `end_exclusive` being the next one's `start` is what adjacency is
    """
    first = TimeRange.from_values(total_months(1850, 1), total_months(1860, 1), MONTHLY)
    second = TimeRange.from_values(
        total_months(1860, 1), total_months(1870, 1), MONTHLY
    )

    assert first.is_adjacent_to(second)


def test_adjacency_does_not_care_which_file_comes_first():
    """
    Symmetric, so a caller never has to sort first to ask
    """
    first = TimeRange.from_values(0, 120, MONTHLY)
    second = TimeRange.from_values(120, 240, MONTHLY)

    assert second.is_adjacent_to(first)


def test_one_month_missing_is_not_adjacency():
    """
    The gap this exists to find
    """
    first = TimeRange.from_values(0, 120, MONTHLY)
    after_a_gap = TimeRange.from_values(121, 240, MONTHLY)

    assert not first.is_adjacent_to(after_a_gap)


def test_an_overlap_is_not_adjacency():
    """
    Two files covering the same month are as wrong as a month nobody published
    """
    first = TimeRange.from_values(0, 120, MONTHLY)
    overlapping = TimeRange.from_values(119, 240, MONTHLY)

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
        TimeRange.from_values(*covering, MONTHLY).contains(
            TimeRange.from_values(*covered, MONTHLY)
        )
        is expected
    )


# ---------------------------------------------------------------------------- axes


def test_ranges_on_different_units_cannot_be_compared():
    """
    Months and years are different numbers, so comparing them is refused out loud
    """
    monthly = TimeRange.from_values(0, 240, MONTHLY)
    yearly = TimeRange.from_values(0, 20, YEARLY)

    with pytest.raises(MismatchedTimeAxisError):
        monthly.contains(yearly)


def test_ranges_in_different_calendars_cannot_be_compared():
    """
    The same day count means different spans in different calendars
    """
    noleap = TimeRange.from_values(0, 3650, DAILY_NOLEAP)
    gregorian = TimeRange.from_values(
        0, 3650, TimeAxis(TimeUnits(TimeStep.DAYS, "1850-01-01"), "standard")
    )

    with pytest.raises(MismatchedTimeAxisError):
        noleap.contains(gregorian)


def test_a_dated_range_which_does_not_know_its_calendar_is_not_compared():
    """
    Not even with itself: a day count means nothing until its calendar is known
    """
    unknown = TimeRange.from_values(
        0, 3650, TimeAxis(TimeUnits(TimeStep.DAYS, "1850-01-01"))
    )

    with pytest.raises(MismatchedTimeAxisError):
        unknown.contains(unknown)


def test_a_natural_range_is_not_compared_with_a_dated_one():
    """
    A month count and a day count are related only through a calendar, by a
    conversion
    """
    with pytest.raises(MismatchedTimeAxisError, match="convert_time_point"):
        TimeRange.from_values(0, 240, MONTHLY).contains(
            TimeRange.from_values(0, 3650, DAILY_NOLEAP)
        )


def test_a_range_must_hold_at_least_one_step():
    """
    `end_exclusive` is the first step *not* in the data, so it comes after `start`
    """
    with pytest.raises(EmptyTimeRangeError):
        TimeRange.from_values(240, 240, MONTHLY)


def test_both_ends_of_a_range_are_on_one_axis():
    """
    A start in months and an end in years is not a range at all
    """
    with pytest.raises(MismatchedTimeAxisError):
        TimeRange(TimePoint(0, MONTHLY), TimePoint(20, YEARLY))


def test_a_range_on_an_axis_which_does_not_know_its_calendar_still_has_an_extent():
    """
    Which is what a daily range looks like before any header has been read

    Its two ends are on the same axis, even though that axis is comparable with
    nothing, so the range is built and its extent is a plain subtraction.
    """
    unknown = TimeAxis(TimeUnits(TimeStep.DAYS, "1850-01-01"))

    assert TimeRange.from_values(0, 3650, unknown).extent == TimeDelta(
        3650, TimeStep.DAYS
    )


def test_the_extent_of_a_dated_range_carries_its_calendar():
    """
    3650 days measured in `noleap` is exactly ten `noleap` years
    """
    extent = TimeRange.from_values(0, 3650, DAILY_NOLEAP).extent

    assert extent == TimeDelta(3650, TimeStep.DAYS, "noleap")
    assert extent.in_steps(TimeStep.YEARS) == TimeDelta(10, TimeStep.YEARS, "noleap")


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


# ---------------------------------------------------------------------- time deltas


def test_a_time_delta_can_be_negative():
    """
    The difference of two time points can go either way

    Where a length has to be positive, e.g. padding, the place which needs it says so.
    """
    assert TimeDelta(-1, TimeStep.YEARS).is_negative


def test_a_time_delta_of_zero_is_fine():
    """
    "No padding required" is a coherent thing to ask for
    """
    assert TimeDelta(0, TimeStep.YEARS).count == 0


def test_a_fractional_number_of_years_is_refused():
    """
    A part of a year is stated in months
    """
    with pytest.raises(FractionalCalendarFreeDeltaError, match="6 months"):
        TimeDelta(0.5, TimeStep.YEARS)


def test_a_fractional_number_of_days_is_fine():
    """
    Which is what the extent of sub-daily data on a `days since` axis comes to
    """
    assert TimeDelta(0.125, TimeStep.DAYS).count == 0.125


@pytest.mark.parametrize(
    ("delta", "rendered"),
    [
        (TimeDelta(20, TimeStep.YEARS), "20 years"),
        (TimeDelta(1, TimeStep.YEARS), "1 year"),
        (TimeDelta(3, TimeStep.HOURS), "3 hours"),
        (TimeDelta(730, TimeStep.DAYS, "noleap"), "730 days (noleap)"),
        (TimeDelta(-10, TimeStep.YEARS), "-10 years"),
    ],
)
def test_time_deltas_read_as_english(delta, rendered):
    """Because they end up in messages a user reads"""
    assert str(delta) == rendered


def test_negating_a_time_delta_keeps_its_step_and_calendar():
    assert TimeDelta(10, TimeStep.DAYS, "noleap").negated() == TimeDelta(
        -10, TimeStep.DAYS, "noleap"
    )


def test_a_time_delta_in_any_calendar_drops_only_the_calendar():
    assert TimeDelta(2, TimeStep.YEARS, "noleap").in_any_calendar() == TimeDelta(
        2, TimeStep.YEARS
    )


def test_equality_of_time_deltas_is_strict():
    """
    Twelve months and a year are the same length, and not the same value

    Comparing lengths is `in_steps`' job.
    """
    year = TimeDelta(1, TimeStep.YEARS)
    twelve_months = TimeDelta(12, TimeStep.MONTHS)

    assert year != twelve_months
    assert year.in_steps(TimeStep.MONTHS) == twelve_months


# ---------------------------------------------------- converting a time delta's step


@pytest.mark.parametrize(
    ("delta", "step", "expected"),
    [
        # Years and months, in any calendar.
        (TimeDelta(2, TimeStep.YEARS), TimeStep.MONTHS, TimeDelta(24, TimeStep.MONTHS)),
        (TimeDelta(36, TimeStep.MONTHS), TimeStep.YEARS, TimeDelta(3, TimeStep.YEARS)),
        (
            TimeDelta(1, TimeStep.YEARS, "standard"),
            TimeStep.MONTHS,
            TimeDelta(12, TimeStep.MONTHS, "standard"),
        ),
        # Days, hours and minutes, in any calendar.
        (TimeDelta(1, TimeStep.DAYS), TimeStep.HOURS, TimeDelta(24, TimeStep.HOURS)),
        (
            TimeDelta(90, TimeStep.MINUTES),
            TimeStep.HOURS,
            TimeDelta(1.5, TimeStep.HOURS),
        ),
        (
            TimeDelta(36, TimeStep.HOURS, "standard"),
            TimeStep.DAYS,
            TimeDelta(1.5, TimeStep.DAYS, "standard"),
        ),
        # Between the two, where the calendar fixes the length.
        (
            TimeDelta(2, TimeStep.YEARS, "noleap"),
            TimeStep.DAYS,
            TimeDelta(730, TimeStep.DAYS, "noleap"),
        ),
        (
            TimeDelta(1, TimeStep.YEARS, "365_day"),
            TimeStep.HOURS,
            TimeDelta(8760, TimeStep.HOURS, "365_day"),
        ),
        (
            TimeDelta(1, TimeStep.YEARS, "all_leap"),
            TimeStep.DAYS,
            TimeDelta(366, TimeStep.DAYS, "all_leap"),
        ),
        (
            TimeDelta(1, TimeStep.YEARS, "366_day"),
            TimeStep.DAYS,
            TimeDelta(366, TimeStep.DAYS, "366_day"),
        ),
        (
            TimeDelta(1, TimeStep.YEARS, "360_day"),
            TimeStep.DAYS,
            TimeDelta(360, TimeStep.DAYS, "360_day"),
        ),
        (
            TimeDelta(3, TimeStep.MONTHS, "360_day"),
            TimeStep.DAYS,
            TimeDelta(90, TimeStep.DAYS, "360_day"),
        ),
        (
            TimeDelta(73000, TimeStep.DAYS, "noleap"),
            TimeStep.YEARS,
            TimeDelta(200, TimeStep.YEARS, "noleap"),
        ),
        (
            TimeDelta(60, TimeStep.DAYS, "360_day"),
            TimeStep.MONTHS,
            TimeDelta(2, TimeStep.MONTHS, "360_day"),
        ),
    ],
)
def test_converting_a_time_delta(delta, step, expected):
    assert delta.in_steps(step) == expected


def test_converting_a_time_delta_to_its_own_step_changes_nothing():
    delta = TimeDelta(1, TimeStep.YEARS, "standard")

    assert delta.in_steps(TimeStep.YEARS) is delta


@pytest.mark.parametrize(
    ("delta", "step"),
    [
        # A year is 365 or 366 days, depending on which year.
        (TimeDelta(1, TimeStep.YEARS, "standard"), TimeStep.DAYS),
        (TimeDelta(1, TimeStep.YEARS, "gregorian"), TimeStep.DAYS),
        (TimeDelta(1, TimeStep.YEARS, "proleptic_gregorian"), TimeStep.DAYS),
        (TimeDelta(1, TimeStep.YEARS, "julian"), TimeStep.DAYS),
        (TimeDelta(365, TimeStep.DAYS, "standard"), TimeStep.YEARS),
        # With no calendar there is nothing to fix it.
        (TimeDelta(1, TimeStep.YEARS), TimeStep.DAYS),
        (TimeDelta(365, TimeStep.DAYS), TimeStep.YEARS),
        # A `noleap` month is 28 to 31 days, depending on which month.
        (TimeDelta(1, TimeStep.MONTHS, "noleap"), TimeStep.DAYS),
        (TimeDelta(31, TimeStep.DAYS, "noleap"), TimeStep.MONTHS),
    ],
)
def test_converting_a_time_delta_which_needs_an_anchor_is_refused(delta, step):
    with pytest.raises(NeedsAnAnchorError, match=r"TimePoint\.plus"):
        delta.in_steps(step)


@pytest.mark.parametrize(
    ("delta", "step"),
    [
        (TimeDelta(13, TimeStep.MONTHS), TimeStep.YEARS),
        (TimeDelta(400, TimeStep.DAYS, "noleap"), TimeStep.YEARS),
    ],
)
def test_converting_to_a_part_of_a_year_is_refused(delta, step):
    with pytest.raises(FractionalCalendarFreeDeltaError):
        delta.in_steps(step)


# -------------------------------------------------------------------------- spanned


def test_spanned_is_earliest_start_to_latest_end():
    """
    And says nothing about whether the ranges fill it
    """
    across = spanned(
        [
            TimeRange.from_values(120, 240, MONTHLY),
            TimeRange.from_values(0, 60, MONTHLY),
        ]
    )

    assert (across.start.value, across.end_exclusive.value) == (0, 240)


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
        spanned(
            [
                TimeRange.from_values(0, 240, MONTHLY),
                TimeRange.from_values(0, 20, YEARLY),
            ]
        )


# ---------------------------------------------------------------------- time points


def test_a_time_point_on_a_natural_axis_is_a_whole_number_of_steps():
    """
    A fraction of a month is not a month: placing one there is a conversion
    """
    with pytest.raises(NotAWholeStepError):
        TimePoint(1850.5, YEARLY)


def test_a_time_point_on_a_dated_axis_is_kept_as_published():
    assert TimePoint(438000.5, DAILY_NOLEAP).value == 438000.5


def test_a_time_point_says_which_ruler_it_was_read_off():
    """
    So that a number in a message is never a bare number
    """
    assert str(TimePoint(91615, DAILY_NOLEAP)) == (
        "91615 days since 1850-01-01, calendar 'noleap'"
    )


# ------------------------------------------------------ holding, and running up to


@pytest.mark.parametrize(
    ("position", "expected"),
    [
        (99, False),
        (100, True),
        (199, True),
        # Half-open: `end_exclusive` is the first instant after the data.
        (200, False),
    ],
)
def test_holds_is_half_open(position, expected):
    assert (
        TimeRange.from_values(100, 200, YEARLY).holds(TimePoint(position, YEARLY))
        is expected
    )


@pytest.mark.parametrize(
    ("position", "expected"),
    [
        (99, False),
        (100, True),
        (199, True),
        # Unlike `holds`: data which stops exactly here runs up to here.
        (200, True),
        # One step past the end is a gap.
        (201, False),
    ],
)
def test_runs_up_to_counts_end_exclusive(position, expected):
    assert (
        TimeRange.from_values(100, 200, YEARLY).runs_up_to(TimePoint(position, YEARLY))
        is expected
    )


@pytest.mark.parametrize("check", ["holds", "runs_up_to"])
def test_a_time_point_off_another_ruler_cannot_be_asked_about(check):
    """
    Year 150 and month 150 are not the same instant
    """
    with pytest.raises(MismatchedTimeAxisError):
        getattr(TimeRange.from_values(100, 200, YEARLY), check)(TimePoint(150, MONTHLY))


# --------------------------------------------------------------------- branch point


def test_a_branch_point_keeps_a_published_float_as_published():
    """
    `branch_time_in_parent` on a dated axis is not rounded to a whole day
    """
    branch = BranchPoint(
        in_parent=TimePoint(438000.5, DAILY_NOLEAP),
        in_child=TimePoint(0.0, DAILY_NOLEAP),
    )

    assert branch.in_child is not None
    assert (branch.in_parent.value, branch.in_child.value) == (438000.5, 0.0)


def test_a_branch_point_need_not_know_where_it_is_in_the_child():
    """
    CMIP5 does not publish it
    """
    branch = BranchPoint(in_parent=TimePoint(0, MONTHLY))

    assert branch.in_child is None


def test_a_branch_point_on_a_natural_axis_is_a_whole_number_of_steps():
    """
    Checked by the position, so no end of a branch point can get past it
    """
    with pytest.raises(NotAWholeStepError):
        BranchPoint(in_parent=TimePoint(100.5, YEARLY))
