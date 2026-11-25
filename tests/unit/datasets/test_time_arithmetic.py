"""
Tests of arithmetic on time points: shifting, subtracting and converting

Arranged by what [TimePoint.plus][esmporium.datasets.TimePoint.plus] does
for each kind of axis and each kind of step,
one test per case so that a failure names the case.
Then [TimePoint.delta_from][esmporium.datasets.TimePoint.delta_from],
then conversions between axes,
then what needs nothing installed and what says so when `cftime` is missing.
"""

from __future__ import annotations

import builtins
import importlib

import pytest

from esmporium.datasets.time import (
    CalendarOnANaturalAxisError,
    DeltaCalendarMismatchError,
    MismatchedTimeAxisError,
    MissingCalendarsExtraError,
    NoEpochError,
    NotAStepOnThisAxisError,
    NotAWholeStepError,
    TimeAxis,
    TimeDelta,
    TimePoint,
    TimeStep,
    TimeUnits,
    UnknownCalendarError,
    convert_time_point,
    total_months,
)

cftime = pytest.importorskip("cftime", reason="the optional `calendars` extra")

MONTHLY = TimeAxis(TimeUnits(TimeStep.MONTHS))
YEARLY = TimeAxis(TimeUnits(TimeStep.YEARS))
NATURAL_DAILY = TimeAxis(TimeUnits(TimeStep.DAYS))

YEARS = TimeStep.YEARS
MONTHS = TimeStep.MONTHS
DAYS = TimeStep.DAYS
HOURS = TimeStep.HOURS
MINUTES = TimeStep.MINUTES


def daily(calendar: str | None, epoch: str = "1850-01-01") -> TimeAxis:
    """
    Get a daily axis in a calendar

    Parameters
    ----------
    calendar
        The CF calendar name, or `None` for one which is not known

    epoch
        The date the axis counts from

    Returns
    -------
    :
        A daily axis counting from `epoch` in that calendar
    """
    return TimeAxis(TimeUnits(DAYS, epoch), calendar)


def on(axis: TimeAxis, year: int, month: int, day: int) -> TimePoint:
    """
    Get a date as a time point on a dated axis

    Parameters
    ----------
    axis
        The dated axis, which knows its calendar

    year
        Calendar year

    month
        Month of that year

    day
        Day of that month

    Returns
    -------
    :
        The date, as a time point on `axis`
    """
    value = cftime.date2num(
        cftime.datetime(year, month, day, calendar=axis.calendar),
        str(axis.units),
        calendar=axis.calendar,
    )

    return TimePoint(value, axis)


def date_at(point: TimePoint) -> str:
    """
    Render a time point on a dated axis as a date

    Parameters
    ----------
    point
        The time point

    Returns
    -------
    :
        The date, as `YYYY-MM-DD`
    """
    at = cftime.num2date(
        point.value, str(point.axis.units), calendar=point.axis.calendar
    )

    return f"{at.year:04d}-{at.month:02d}-{at.day:02d}"


# ------------------------------------------------ plus: a natural axis, years or months


@pytest.mark.parametrize(
    ("axis", "value", "by", "expected"),
    [
        (MONTHLY, 22189, TimeDelta(20, YEARS), 22429),
        (MONTHLY, 22189, TimeDelta(240, MONTHS), 22429),
        (MONTHLY, 0, TimeDelta(0, YEARS), 0),
        (MONTHLY, 22429, TimeDelta(-20, YEARS), 22189),
        (YEARLY, 1850, TimeDelta(20, YEARS), 1870),
        (YEARLY, 1850, TimeDelta(240, MONTHS), 1870),
        (YEARLY, 1870, TimeDelta(-24, MONTHS), 1868),
    ],
)
def test_plus_natural_by_nominal(axis, value, by, expected):
    """
    Exact integer arithmetic, because twelve months is a year everywhere
    """
    assert TimePoint(value, axis).plus(by) == TimePoint(expected, axis)


def test_plus_natural_by_nominal_ignores_calendar_tag():
    """
    An axis which counts months has no days in it for a calendar to decide
    """
    assert TimePoint(22189, MONTHLY).plus(TimeDelta(20, YEARS, "360_day")) == TimePoint(
        22429, MONTHLY
    )


def test_a_year_and_twelve_months_are_the_same_move():
    """
    Which is the whole reason monthly data never needs a header
    """
    start = TimePoint(0, MONTHLY)

    assert start.plus(TimeDelta(1, YEARS)) == start.plus(TimeDelta(12, MONTHS))


def test_shifting_back_undoes_shifting_forward():
    """On an axis where both are exact"""
    twenty_years = TimeDelta(20, YEARS)
    start = TimePoint(22189, MONTHLY)

    assert start.plus(twenty_years).plus(twenty_years.negated()) == start


def test_plus_natural_by_a_part_year_refused():
    """
    Six months is not a time point on a yearly axis; twelve months is
    """
    with pytest.raises(NotAStepOnThisAxisError, match="no step smaller than one year"):
        TimePoint(1850, YEARLY).plus(TimeDelta(6, MONTHS))

    assert TimePoint(1850, YEARLY).plus(TimeDelta(12, MONTHS)).value == 1851


# ----------------------------------------- plus: a natural axis, days, hours or minutes


@pytest.mark.parametrize("by", [TimeDelta(5, DAYS), TimeDelta(5, DAYS, "360_day")])
def test_plus_natural_by_exact_refused(by):
    """
    A monthly axis has no step smaller than a month, so days do not name a point

    Even when the days are measured in a calendar where they would make whole months:
    that is a conversion, and it is said out loud with `in_steps`.
    """
    with pytest.raises(NotAStepOnThisAxisError, match="no step smaller than one month"):
        TimePoint(0, MONTHLY).plus(by)

    with pytest.raises(NotAStepOnThisAxisError, match="in_steps"):
        TimePoint(0, MONTHLY).plus(by)


def test_plus_natural_by_exact_after_converting():
    """
    The way round the refusal, where the calendar fixes the length
    """
    sixty_days = TimeDelta(60, DAYS, "360_day")

    assert TimePoint(0, MONTHLY).plus(sixty_days.in_steps(MONTHS)).value == 2


@pytest.mark.parametrize(
    ("by", "expected"),
    [(TimeDelta(3, DAYS), 3), (TimeDelta(48, HOURS), 2), (TimeDelta(-1, DAYS), -1)],
)
def test_plus_natural_sub_monthly_by_exact(by, expected):
    """
    A count of days with no epoch is still a count, and counting is exact
    """
    assert TimePoint(0, NATURAL_DAILY).plus(by).value == expected


def test_plus_natural_sub_monthly_by_part_of_a_step_refused():
    """
    Thirty-six hours is not a whole number of steps on an axis which counts days
    """
    with pytest.raises(NotAWholeStepError):
        TimePoint(0, NATURAL_DAILY).plus(TimeDelta(36, HOURS))


def test_plus_natural_sub_monthly_by_nominal_refused():
    """
    A different complaint from not knowing the calendar

    "Days" on its own is not a scale: there is no date to count from, so a year after
    one of them is not a question with an answer.
    """
    with pytest.raises(NoEpochError):
        TimePoint(0, NATURAL_DAILY).plus(TimeDelta(1, YEARS))


# -------------------------------------------------- plus: a dated axis, years or months


@pytest.mark.parametrize(
    ("calendar", "expected"),
    [("standard", "1850-02-28"), ("noleap", "1850-02-28"), ("all_leap", "1850-02-29")],
)
def test_plus_dated_by_nominal_untagged(calendar, expected):
    """
    The thirty-first of January plus one month

    Clamped to the end of the month landed in, never spilling into March.
    """
    axis = daily(calendar)
    landed = on(axis, 1850, 1, 31).plus(TimeDelta(1, MONTHS))

    assert date_at(landed) == expected


def test_the_thirtieth_of_january_plus_one_month_in_a_360_day_calendar():
    """
    Where February has thirty days, so nothing is clamped at all
    """
    axis = daily("360_day")
    landed = on(axis, 1850, 1, 30).plus(TimeDelta(1, MONTHS))

    assert date_at(landed) == "1850-02-30"


def test_the_twenty_ninth_of_february_plus_one_year_in_a_standard_calendar():
    """
    1852 is a leap year and 1853 is not, so the day has to give
    """
    axis = daily("standard")
    landed = on(axis, 1852, 2, 29).plus(TimeDelta(1, YEARS))

    assert date_at(landed) == "1853-02-28"


@pytest.mark.parametrize("calendar", ["standard", "noleap", "360_day", "all_leap"])
def test_twenty_years_after_the_first_of_march_is_the_first_of_march(calendar):
    """
    A year and a month offset is exact at any frequency, given a calendar

    The ambiguity people expect here only appears if you try to turn "twenty years"
    into a fixed number of days detached from a time point on the axis.
    """
    axis = daily(calendar)
    landed = on(axis, 1850, 3, 1).plus(TimeDelta(20, YEARS))

    assert date_at(landed) == "1870-03-01"


def test_the_thirty_first_of_march_minus_one_month():
    """
    Moving back clamps the same way moving forward does
    """
    axis = daily("standard")
    landed = on(axis, 1850, 3, 31).plus(TimeDelta(-1, MONTHS))

    assert date_at(landed) == "1850-02-28"


@pytest.mark.parametrize(
    ("calendar", "days_in_twenty_years"),
    [("noleap", 7300), ("all_leap", 7320), ("360_day", 7200)],
)
def test_how_long_twenty_years_turns_out_to_be(calendar, days_in_twenty_years):
    """
    The number the calendars disagree about

    Pinned here all the same, because it is the evidence that rolling our own leap
    rules would have been the job it looks like it is not.
    """
    axis = daily(calendar)
    start = on(axis, 1850, 1, 1)

    assert start.plus(TimeDelta(20, YEARS)).delta_from(start) == TimeDelta(
        days_in_twenty_years, DAYS, calendar
    )


def test_plus_dated_by_nominal_same_calendar():
    """
    Years measured in the axis' own calendar are just years
    """
    axis = daily("noleap")
    landed = on(axis, 1850, 3, 1).plus(TimeDelta(20, YEARS, "noleap"))

    assert date_at(landed) == "1870-03-01"


def test_plus_dated_by_nominal_other_calendar_refused():
    """
    "Two years, as `noleap` counts them" is ambiguous on a `standard` axis

    Is it two calendar years, or the 730 days it was in `noleap`? Neither is assumed.
    """
    axis = daily("standard")

    with pytest.raises(DeltaCalendarMismatchError, match="in_any_calendar"):
        on(axis, 1850, 1, 1).plus(TimeDelta(2, YEARS, "noleap"))


def test_plus_dated_by_nominal_in_any_calendar_crosses():
    """
    Saying so is all it takes, and it is said where it is done
    """
    axis = daily("standard")
    by = TimeDelta(2, YEARS, "noleap").in_any_calendar()

    assert date_at(on(axis, 1850, 1, 1).plus(by)) == "1852-01-01"


def test_plus_dated_by_nominal_converted_to_days_crosses():
    """
    The other way to cross: as the number of days it was where it was measured
    """
    axis = daily("standard")
    by = TimeDelta(2, YEARS, "noleap").in_steps(DAYS)

    # 730 days from 1851-01-01 in `standard` crosses 29 February 1852,
    # so it falls a day short of two years.
    assert date_at(on(axis, 1851, 1, 1).plus(by)) == "1852-12-31"
    assert date_at(on(axis, 1851, 1, 1).plus(TimeDelta(2, YEARS))) == "1853-01-01"


def test_plus_dated_months_since_axis_by_nominal():
    """
    A dated axis which counts months counts them exactly, with nothing installed
    """
    axis = TimeAxis(TimeUnits(MONTHS, "1850-01-01"), "360_day")

    assert TimePoint(0, axis).plus(TimeDelta(2, YEARS)).value == 24


# ------------------------------------------- plus: a dated axis, days, hours or minutes


def test_plus_dated_by_exact_same_calendar():
    axis = daily("noleap")

    assert date_at(on(axis, 1850, 2, 27).plus(TimeDelta(2, DAYS))) == "1850-03-01"


def test_plus_dated_by_exact_other_calendar_walks():
    """
    A day is a day in every calendar, so days measured in one are walked in another

    Whether that is what is wanted when two runs are in different calendars is the
    caller's question: see `Covers`' `_lengths_cross`.
    """
    axis = daily("360_day")
    walked = TimePoint(0, axis).plus(TimeDelta(730, DAYS, "noleap"))

    assert walked == TimePoint(730, axis)
    # Which in `360_day` is ten days past two years.
    assert date_at(walked) == "1852-01-11"


@pytest.mark.parametrize(
    ("units", "by", "expected"),
    [
        (TimeUnits(HOURS, "1850-01-01"), TimeDelta(36, HOURS), 36),
        (TimeUnits(HOURS, "1850-01-01"), TimeDelta(1, DAYS), 24),
        (TimeUnits(MINUTES, "1850-01-01"), TimeDelta(90, MINUTES), 90),
        (TimeUnits(MINUTES, "1850-01-01"), TimeDelta(1, HOURS), 60),
        (TimeUnits(DAYS, "1850-01-01"), TimeDelta(12, HOURS), 0.5),
        (TimeUnits(DAYS, "1850-01-01"), TimeDelta(-36, HOURS), -1.5),
    ],
)
def test_plus_dated_by_exact_unit_conversion(units, by, expected):
    """
    Sub-daily moves, where a day is twenty-four hours in every calendar
    """
    axis = TimeAxis(units, "noleap")

    assert TimePoint(0, axis).plus(by).value == expected


def test_plus_dated_months_since_axis_by_exact():
    """
    Days on an axis which counts months need the calendar to say how long a month is

    `cftime` reads `months since` in a `360_day` calendar only, where a month is
    thirty days.
    """
    axis = TimeAxis(TimeUnits(MONTHS, "1850-01-01"), "360_day")

    assert TimePoint(0, axis).plus(TimeDelta(45, DAYS)).value == pytest.approx(1.5)


# ---------------------------------- plus: a dated axis which does not know its calendar


@pytest.mark.parametrize(
    "by",
    [
        TimeDelta(1, YEARS),
        TimeDelta(1, YEARS, "noleap"),
        TimeDelta(1, DAYS),
        TimeDelta(1, DAYS, "noleap"),
    ],
)
def test_plus_unknown_calendar_raises(by):
    """
    Work waiting to be done, and distinguishable from every other failure
    """
    with pytest.raises(UnknownCalendarError, match="enrich the dataset"):
        TimePoint(0, daily(None)).plus(by)


# ----------------------------------------------------------------------- delta_from


def test_delta_from_on_comparable_axes():
    """
    Subtracted as they stand, and tagged with the calendar they were measured in
    """
    axis = daily("noleap")

    assert TimePoint(730, axis).delta_from(TimePoint(0, axis)) == TimeDelta(
        730, DAYS, "noleap"
    )


def test_delta_from_on_a_natural_axis_has_no_calendar():
    assert TimePoint(22429, MONTHLY).delta_from(TimePoint(22189, MONTHLY)) == TimeDelta(
        240, MONTHS
    )


def test_delta_from_can_be_negative():
    assert TimePoint(0, MONTHLY).delta_from(TimePoint(12, MONTHLY)) == TimeDelta(
        -12, MONTHS
    )


def test_delta_from_with_a_different_epoch():
    """
    The same calendar from two different dates: one is moved onto the other's axis

    1850-01-01 is 1849 `noleap` years after 0001-01-01.
    """
    since_0001 = daily("noleap", epoch="0001-01-01")
    since_1850 = daily("noleap", epoch="1850-01-01")
    later = TimePoint(1849 * 365 + 10, since_0001)

    assert later.delta_from(TimePoint(0, since_1850)) == TimeDelta(10, DAYS, "noleap")


def test_delta_from_with_a_different_exact_step():
    """
    Counted in the finer of the two, so that nothing is rounded
    """
    in_days = TimeAxis(TimeUnits(DAYS, "1850-01-01"), "noleap")
    in_hours = TimeAxis(TimeUnits(HOURS, "1850-01-01"), "noleap")

    assert TimePoint(2, in_days).delta_from(TimePoint(36, in_hours)) == TimeDelta(
        12, HOURS, "noleap"
    )


@pytest.mark.parametrize(
    ("later", "earlier", "expected"),
    [
        (TimePoint(1860, YEARLY), TimePoint(total_months(1850, 12), MONTHLY), 109),
        (TimePoint(total_months(1860, 6), MONTHLY), TimePoint(1850, YEARLY), 125),
    ],
)
def test_delta_from_years_against_months(later, earlier, expected):
    """
    Counted in months either way round, because a month moved onto a yearly axis
    would be rounded down to its year
    """
    assert later.delta_from(earlier) == TimeDelta(expected, MONTHS)


def test_delta_from_refuses_calendar_mismatch():
    """
    The same day count means different spans in different calendars
    """
    with pytest.raises(MismatchedTimeAxisError):
        TimePoint(0, daily("noleap")).delta_from(TimePoint(0, daily("standard")))


def test_delta_from_refuses_natural_against_dated():
    """
    A month count has no calendar, so relating it to a day count would assume one
    """
    with pytest.raises(MismatchedTimeAxisError):
        TimePoint(22189, MONTHLY).delta_from(TimePoint(0, daily("noleap")))


@pytest.mark.parametrize(
    ("later", "earlier"),
    [
        (daily(None), daily(None)),
        (daily(None), daily("noleap", epoch="0001-01-01")),
        (daily("noleap"), daily(None, epoch="0001-01-01")),
    ],
)
def test_delta_from_refuses_unknown_calendar(later, earlier):
    """
    Not even on the same axis: a day count means nothing until its calendar is known
    """
    with pytest.raises(UnknownCalendarError):
        TimePoint(10, later).delta_from(TimePoint(0, earlier))


def test_the_same_distance_after_a_branch_on_another_ruler():
    """
    What `Covers` does at every branch, in one line
    """
    parent_axis = daily("noleap", epoch="0001-01-01")
    child_axis = daily("noleap", epoch="1850-01-01")
    branch_in_parent = TimePoint(182500, parent_axis)
    branch_in_child = TimePoint(0, child_axis)
    point_in_child = TimePoint(31390, child_axis)

    assert branch_in_parent.plus(
        point_in_child.delta_from(branch_in_child)
    ) == TimePoint(213890, parent_axis)


# ------------------------------------------------------- converting between axes


@pytest.mark.parametrize(
    ("point", "axis", "expected"),
    [
        (TimePoint(1850, YEARLY), MONTHLY, 22189),
        (TimePoint(22200, MONTHLY), YEARLY, 1850),
        (TimePoint(22189, MONTHLY), MONTHLY, 22189),
    ],
)
def test_converting_between_natural_axes(point, axis, expected):
    assert convert_time_point(point, axis) == TimePoint(expected, axis)


def test_converting_a_month_onto_a_daily_axis_is_the_instant_it_starts():
    converted = convert_time_point(TimePoint(22190, MONTHLY), daily("noleap"))

    assert converted.value == 31
    assert date_at(converted) == "1850-02-01"


def test_converting_a_midnight_bound_onto_a_monthly_axis_lands_in_that_month():
    """
    Which is what a half-open end needs
    """
    bound = on(daily("standard"), 1861, 1, 1)

    assert convert_time_point(bound, MONTHLY) == TimePoint(
        total_months(1861, 1), MONTHLY
    )


def test_converting_between_dated_axes_in_different_calendars_is_refused():
    with pytest.raises(MismatchedTimeAxisError):
        convert_time_point(TimePoint(0, daily("noleap")), daily("360_day"))


def test_converting_onto_an_axis_which_does_not_know_its_calendar_says_so():
    """
    Moving along the axis and converting onto it are two ways to need the same calendar
    """
    with pytest.raises(UnknownCalendarError):
        convert_time_point(TimePoint(22189, MONTHLY), daily(None))


def test_a_natural_axis_cannot_carry_a_calendar():
    """
    A count with no epoch is the same count in every calendar, so naming one says
    nothing
    """
    with pytest.raises(CalendarOnANaturalAxisError):
        TimeAxis(TimeUnits(DAYS), "360_day")


# ------------------------------------------------- placing a year and month on an axis


@pytest.mark.parametrize(
    ("axis", "year", "month", "expected"),
    [
        (YEARLY, 1850, None, 1850),
        (MONTHLY, 1850, 1, 22189),
        (MONTHLY, 1, 1, 1),
    ],
)
def test_placing_a_year_and_month_on_a_natural_axis(axis, year, month, expected):
    assert TimePoint.from_year_and_month(axis, year=year, month=month) == TimePoint(
        expected, axis
    )


@pytest.mark.parametrize(("axis", "month"), [(YEARLY, 7), (MONTHLY, None)])
def test_a_year_and_month_which_does_not_match_the_axis_step_is_refused(axis, month):
    """
    A month on a yearly axis would have to be dropped, and a monthly axis without one
    would have to fill it in
    """
    with pytest.raises(ValueError, match="got year=1850"):
        TimePoint.from_year_and_month(axis, year=1850, month=month)


def test_a_year_and_month_is_not_placed_on_a_dated_axis_this_way():
    """
    That is an instant in a calendar, and `convert_time_point`'s job
    """
    with pytest.raises(ValueError, match="convert_time_point"):
        TimePoint.from_year_and_month(daily("noleap"), year=1850)


def test_a_natural_sub_monthly_axis_has_no_years_on_it():
    """
    Which is exactly why a daily range needs a header read
    """
    with pytest.raises(NoEpochError):
        TimePoint.from_year_and_month(NATURAL_DAILY, year=1850)


# ----------------------------------------------------- without the extra installed


def test_the_calendar_free_paths_work_without_cftime(monkeypatch):
    """
    Monthly and yearly requirements need nothing installed, which is the promise

    `cftime` is hidden rather than uninstalled, so this runs in an environment which
    happens to have it.
    """
    _hide_cftime(monkeypatch)
    twenty_years = TimeDelta(20, YEARS)

    assert TimePoint(total_months(1850, 1), MONTHLY).plus(twenty_years) == (
        TimePoint(total_months(1870, 1), MONTHLY)
    )
    assert TimePoint(1850, YEARLY).plus(twenty_years) == TimePoint(1870, YEARLY)
    assert TimePoint(1870, YEARLY).delta_from(TimePoint(22189, MONTHLY)) == TimeDelta(
        240, MONTHS
    )


def test_walking_days_on_a_daily_axis_works_without_cftime(monkeypatch):
    """
    Days onto a day count is counting, so there is no date to look up
    """
    _hide_cftime(monkeypatch)
    axis = TimeAxis(TimeUnits(HOURS, "1850-01-01"), "noleap")

    assert TimePoint(0, axis).plus(TimeDelta(1, DAYS)).value == 24


@pytest.mark.parametrize(
    "needs_cftime",
    [
        pytest.param(
            lambda: TimePoint(0, daily("noleap")).plus(TimeDelta(1, YEARS)),
            id="years on a daily axis",
        ),
        pytest.param(
            lambda: convert_time_point(TimePoint(22189, MONTHLY), daily("noleap")),
            id="converting onto a daily axis",
        ),
        pytest.param(
            lambda: TimePoint(0, daily("noleap", epoch="0001-01-01")).delta_from(
                TimePoint(0, daily("noleap"))
            ),
            id="delta_from across epochs",
        ),
    ],
)
def test_calendar_arithmetic_without_cftime_names_the_extra(monkeypatch, needs_cftime):
    """
    An environment problem, raised as one, so it can never become a solve status

    Every way in has to say which extra it is.
    """
    _hide_cftime(monkeypatch)

    with pytest.raises(MissingCalendarsExtraError, match=r"esmporium\[calendars\]"):
        needs_cftime()


def test_the_missing_extra_is_an_import_error():
    """
    So that nothing downstream mistakes it for a fact about the data
    """
    assert issubclass(MissingCalendarsExtraError, ImportError)


def _hide_cftime(monkeypatch) -> None:
    """
    Make importing `cftime` fail, as it does without the extra

    Parameters
    ----------
    monkeypatch
        pytest's monkeypatch fixture
    """
    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        """Refuse `cftime` and pass everything else through"""
        if name == "cftime":
            msg = "No module named 'cftime'"
            raise ImportError(msg)

        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(importlib.sys.modules, "cftime", raising=False)
    monkeypatch.setattr(builtins, "__import__", refuse)
