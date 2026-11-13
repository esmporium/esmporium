"""
Tests of calendar arithmetic

Two halves. The first needs nothing installed, which is the point: monthly and yearly
axes are what nearly every requirement asks about, and twelve months is a year in
every CF calendar. The second is the awkward end of the calendars, and is where the
`cftime` dependency earns its keep.
"""

from __future__ import annotations

import builtins
import importlib

import pytest

from esmporium.datasets.calendars import (
    MissingCalendarsExtraError,
    NoEpochError,
    UnknownCalendarError,
    advance,
    position_of_date,
    retreat,
)
from esmporium.datasets.time import Span, TimeAxis, TimeStep, TimeUnits, total_months

cftime = pytest.importorskip("cftime", reason="the optional `calendars` extra")

MONTHLY = TimeAxis(TimeUnits(TimeStep.MONTHS))
YEARLY = TimeAxis(TimeUnits(TimeStep.YEARS))

DAY_UNITS = "days since 1850-01-01"


def daily(calendar: str) -> TimeAxis:
    """
    Get a daily axis in a calendar

    Parameters
    ----------
    calendar
        The CF calendar name

    Returns
    -------
    :
        A daily axis counting from 1850-01-01 in that calendar
    """
    return TimeAxis(TimeUnits(TimeStep.DAYS, "1850-01-01"), calendar)


def on(axis: TimeAxis, *parts: int) -> int:
    """
    Get a date's position on an axis

    Parameters
    ----------
    axis
        The axis

    *parts
        Year, then optionally month, day and hour

    Returns
    -------
    :
        The date's position
    """
    year, month, day, hour = (*parts, *(1, 1, 1, 0)[len(parts) :])

    return position_of_date(axis, year=year, month=month, day=day, hour=hour)


def date_at(position: int, calendar: str) -> str:
    """
    Render a position on a daily axis as a date

    Parameters
    ----------
    position
        The position

    calendar
        The CF calendar name

    Returns
    -------
    :
        The date, as `YYYY-MM-DD`
    """
    at = cftime.num2date(position, DAY_UNITS, calendar=calendar)

    return f"{at.year:04d}-{at.month:02d}-{at.day:02d}"


# -------------------------------------------------- the paths which need nothing


@pytest.mark.parametrize(
    ("axis", "position", "by", "expected"),
    [
        (MONTHLY, 22189, Span(20, TimeStep.YEARS), 22429),
        (MONTHLY, 22189, Span(240, TimeStep.MONTHS), 22429),
        (MONTHLY, 0, Span(0, TimeStep.YEARS), 0),
        (YEARLY, 1850, Span(20, TimeStep.YEARS), 1870),
        (YEARLY, 1850, Span(240, TimeStep.MONTHS), 1870),
    ],
)
def test_advancing_a_calendar_free_axis(axis, position, by, expected):
    """
    Exact integer arithmetic, because twelve months is a year everywhere
    """
    assert advance(position, by, axis) == expected


def test_a_year_and_twelve_months_are_the_same_move():
    """
    Which is the whole reason monthly data never needs a header
    """
    assert advance(0, Span(1, TimeStep.YEARS), MONTHLY) == advance(
        0, Span(12, TimeStep.MONTHS), MONTHLY
    )


def test_retreating_undoes_advancing():
    """On an axis where both are exact"""
    twenty_years = Span(20, TimeStep.YEARS)

    assert (
        retreat(advance(22189, twenty_years, MONTHLY), twenty_years, MONTHLY) == 22189
    )


def test_days_of_padding_against_monthly_data_is_refused():
    """
    A monthly axis has no step smaller than a month, so days do not name a position
    """
    with pytest.raises(ValueError, match="no step smaller than one month"):
        advance(0, Span(5, TimeStep.DAYS), MONTHLY)


def test_a_part_year_against_yearly_data_is_refused():
    """
    Six months is not a position on a yearly axis; twelve months is
    """
    with pytest.raises(ValueError, match="no step smaller than one year"):
        advance(1850, Span(6, TimeStep.MONTHS), YEARLY)

    assert advance(1850, Span(12, TimeStep.MONTHS), YEARLY) == 1851


@pytest.mark.parametrize(
    ("axis", "parts", "expected"),
    [
        (YEARLY, (1850,), 1850),
        (YEARLY, (1850, 7, 15), 1850),
        (MONTHLY, (1850, 1), 22189),
        (MONTHLY, (1, 1), 1),
    ],
)
def test_placing_a_date_on_a_calendar_free_axis(axis, parts, expected):
    """
    Components finer than the axis' own step are ignored, not refused
    """
    assert on(axis, *parts) == expected


def test_a_natural_sub_monthly_axis_has_no_dates_on_it():
    """
    Which is exactly why a daily range needs a header read
    """
    with pytest.raises(NoEpochError):
        position_of_date(TimeAxis(TimeUnits(TimeStep.DAYS)), year=1850)


# --------------------------------------------------- the awkward end of the calendars


@pytest.mark.parametrize(
    ("calendar", "expected"),
    [("standard", "1850-02-28"), ("noleap", "1850-02-28"), ("all_leap", "1850-02-29")],
)
def test_the_thirty_first_of_january_plus_one_month(calendar, expected):
    """
    Clamped to the end of the month landed in, never spilling into March
    """
    axis = daily(calendar)
    landed = advance(on(axis, 1850, 1, 31), Span(1, TimeStep.MONTHS), axis)

    assert date_at(landed, calendar) == expected


def test_the_thirtieth_of_january_plus_one_month_in_a_360_day_calendar():
    """
    Where February has thirty days, so nothing is clamped at all
    """
    axis = daily("360_day")
    landed = advance(on(axis, 1850, 1, 30), Span(1, TimeStep.MONTHS), axis)

    assert date_at(landed, "360_day") == "1850-02-30"


def test_the_twenty_ninth_of_february_plus_one_year_in_a_standard_calendar():
    """
    1852 is a leap year and 1853 is not, so the day has to give
    """
    axis = daily("standard")
    landed = advance(on(axis, 1852, 2, 29), Span(1, TimeStep.YEARS), axis)

    assert date_at(landed, "standard") == "1853-02-28"


@pytest.mark.parametrize("calendar", ["standard", "noleap", "360_day", "all_leap"])
def test_twenty_years_after_the_first_of_march_is_the_first_of_march(calendar):
    """
    A year and a month offset is exact at any frequency, given a calendar

    The ambiguity people expect here only appears if you try to turn "twenty years"
    into a fixed number of days detached from a position on the axis.
    """
    axis = daily(calendar)
    landed = advance(on(axis, 1850, 3, 1), Span(20, TimeStep.YEARS), axis)

    assert date_at(landed, calendar) == "1870-03-01"


def test_the_thirty_first_of_march_minus_one_month():
    """
    Retreating clamps the same way advancing does
    """
    axis = daily("standard")
    landed = retreat(on(axis, 1850, 3, 31), Span(1, TimeStep.MONTHS), axis)

    assert date_at(landed, "standard") == "1850-02-28"


@pytest.mark.parametrize(
    ("calendar", "days_in_twenty_years"),
    [("noleap", 7300), ("all_leap", 7320), ("360_day", 7200)],
)
def test_how_long_twenty_years_turns_out_to_be(calendar, days_in_twenty_years):
    """
    The number the calendars disagree about, which is why we never compute it

    Pinned here all the same, because it is the evidence that rolling our own leap
    rules would have been the job it looks like it is not.
    """
    axis = daily(calendar)
    start = on(axis, 1850, 1, 1)

    assert advance(start, Span(20, TimeStep.YEARS), axis) - start == (
        days_in_twenty_years
    )


def test_hours_and_days_on_an_hourly_axis():
    """
    Sub-daily moves, where a day is twenty-four hours in every calendar
    """
    axis = TimeAxis(TimeUnits(TimeStep.HOURS, "1850-01-01"), "noleap")

    assert advance(0, Span(36, TimeStep.HOURS), axis) == 36
    assert advance(0, Span(1, TimeStep.DAYS), axis) == 24


def test_minutes_on_an_hourly_axis():
    """
    The step CMIP6 stamps its sub-daily filenames to
    """
    axis = TimeAxis(TimeUnits(TimeStep.MINUTES, "1850-01-01"), "noleap")

    assert advance(0, Span(90, TimeStep.MINUTES), axis) == 90
    assert advance(0, Span(1, TimeStep.HOURS), axis) == 60


@pytest.mark.parametrize("move", [advance, position_of_date])
def test_an_axis_which_does_not_know_its_calendar_says_so(move):
    """
    Work waiting to be done, and distinguishable from every other failure

    Both directions have to say it: placing a date on the axis and moving along it
    are two ways to need the same calendar.
    """
    axis = TimeAxis(TimeUnits(TimeStep.DAYS, "1850-01-01"), calendar=None)

    with pytest.raises(UnknownCalendarError):
        if move is advance:
            advance(0, Span(1, TimeStep.YEARS), axis)
        else:
            position_of_date(axis, year=1850)


def test_a_sub_monthly_axis_with_a_calendar_but_no_epoch_says_so():
    """
    A reachable state, and a different complaint from not knowing the calendar

    "Days, in the 360-day calendar" is still not a scale: there is no date to count
    from, so there is nothing to do arithmetic against.
    """
    axis = TimeAxis(TimeUnits(TimeStep.DAYS), "360_day")

    with pytest.raises(NoEpochError):
        advance(0, Span(1, TimeStep.YEARS), axis)


# ----------------------------------------------------- without the extra installed


def test_the_calendar_free_paths_work_without_cftime(monkeypatch):
    """
    Monthly and yearly requirements need nothing installed, which is the promise

    `cftime` is hidden rather than uninstalled, so this runs in an environment which
    happens to have it.
    """
    _hide_cftime(monkeypatch)

    assert advance(total_months(1850, 1), Span(20, TimeStep.YEARS), MONTHLY) == (
        total_months(1870, 1)
    )
    assert advance(1850, Span(20, TimeStep.YEARS), YEARLY) == 1870


def test_a_daily_axis_without_cftime_names_the_extra(monkeypatch):
    """
    An environment problem, raised as one, so it can never become a solve status
    """
    _hide_cftime(monkeypatch)

    with pytest.raises(MissingCalendarsExtraError, match=r"esmporium\[calendars\]"):
        advance(0, Span(1, TimeStep.YEARS), daily("noleap"))


def test_placing_a_date_without_cftime_names_the_extra_too(monkeypatch):
    """
    Both entry points need the extra, so both have to say which one it is
    """
    _hide_cftime(monkeypatch)

    with pytest.raises(MissingCalendarsExtraError, match=r"esmporium\[calendars\]"):
        position_of_date(daily("noleap"), year=1850)


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
