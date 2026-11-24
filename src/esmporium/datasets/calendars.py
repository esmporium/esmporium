"""
Moving along a time axis by a length of time, and from one axis to another

A thin wrapper over `cftime` rather than our own leap rules:
six CF calendars with six sets of leap rules is
exactly the job which looks small and is not.

`cftime` comes from the optional `calendars` extra and is imported at point of use.
Monthly and yearly axes never reach that import, because twelve months is a year in
every calendar -- so the common path needs nothing installed, and only the
sub-monthly path and conversions onto or off a dated axis pay.

This lives in `esmporium.datasets` rather than beside the rest of enrichment because
[Covers][esmporium.requirements.Covers] moves along axes *at solve time*, and
`esmporium.requirements` may import `esmporium.datasets` and not `esmporium.enrich`.
"""

from __future__ import annotations

import datetime
from typing import Any, cast

from esmporium.datasets.time import (
    MONTHS_PER_YEAR,
    MismatchedTimeAxisError,
    Span,
    TimeAxis,
    TimeStep,
    TimeUnits,
    check_position,
    total_months,
    year_and_month,
)


class MissingCalendarsExtraError(ImportError):
    """
    Raised when calendar arithmetic is needed and `cftime` is not installed

    An `ImportError` rather than anything of ours, because it is not a fact about
    the data and not a feature we have not written, so it must never become a
    solve status beside `NEEDS_ENRICHMENT`.
    """

    def __init__(self) -> None:
        """
        Initialise the error
        """
        super().__init__(
            "Calendar arithmetic on a dated axis needs cftime, which is the optional "
            "`calendars` extra: install `esmporium[calendars]`. Moving along a "
            "monthly or yearly axis does not need it, because twelve months is a "
            "year in every CF calendar."
        )


class UnknownCalendarError(ValueError):
    """
    Raised when arithmetic on a dated axis is wanted and its calendar is not known
    """

    def __init__(self, axis: TimeAxis) -> None:
        """
        Initialise the error

        Parameters
        ----------
        axis
            The axis whose calendar is not known
        """
        self.axis = axis
        super().__init__(
            f"Arithmetic on a {axis.units} axis needs its calendar, and this axis does "
            "not know one. A calendar is read from the attributes of a file's time "
            "variable, so this is work waiting to be done rather than a question "
            "with no answer: enrich the dataset and ask again."
        )


class NoEpochError(ValueError):
    """
    Raised when a sub-monthly axis has no epoch to count from
    """

    def __init__(self, units: TimeUnits) -> None:
        """
        Initialise the error

        Parameters
        ----------
        units
            The units which carry no epoch
        """
        self.units = units
        super().__init__(
            f"A {units} axis has no epoch, so its numbers are an ordinal count and "
            "there is no date to do calendar arithmetic from. Only years and months "
            "have a calendar-free ordinal; anything finer is a dated axis, "
            "'<step> since <epoch>'."
        )


def advance(position: float, by: Span, axis: TimeAxis) -> float:
    """
    Move forwards along a time axis by a length of time

    Parameters
    ----------
    position
        Where to start, as a position on `axis`

    by
        How far to move

    axis
        The axis `position` is on, and which the answer is on

    Returns
    -------
    :
        Where `by` after `position` falls, as a position on `axis`

    Raises
    ------
    MissingCalendarsExtraError
        The axis is daily or finer and `cftime` is not installed

    UnknownCalendarError
        The axis is daily or finer and its calendar is not known

    NoEpochError
        The axis is daily or finer and carries no epoch

    ValueError
        `by` is finer than the axis' own step, e.g. days of padding against a yearly
        axis

    Examples
    --------
    Twenty years of padding on a monthly axis, with nothing installed:

    >>> monthly = TimeAxis(TimeUnits(TimeStep.MONTHS))
    >>> advance(22189, Span(20, TimeStep.YEARS), monthly)
    22429

    A month is a month, and a year is twelve of them:

    >>> advance(0, Span(1, TimeStep.YEARS), monthly) == advance(
    ...     0, Span(12, TimeStep.MONTHS), monthly
    ... )
    True
    """
    return _move(position, by, axis, backwards=False)


def retreat(position: float, by: Span, axis: TimeAxis) -> float:
    """
    Move backwards along a time axis by a length of time

    Parameters
    ----------
    position
        Where to start, as a position on `axis`

    by
        How far to move back

    axis
        The axis `position` is on, and which the answer is on

    Returns
    -------
    :
        Where `by` before `position` falls, as a position on `axis`

    Raises
    ------
    MissingCalendarsExtraError
        The axis is daily or finer and `cftime` is not installed

    UnknownCalendarError
        The axis is daily or finer and its calendar is not known

    NoEpochError
        The axis is daily or finer and carries no epoch

    ValueError
        `by` is finer than the axis' own step


    Examples
    --------
    >>> monthly = TimeAxis(TimeUnits(TimeStep.MONTHS))
    >>> retreat(22429, Span(20, TimeStep.YEARS), monthly)
    22189
    """
    return _move(position, by, axis, backwards=True)


def position_of_date(axis: TimeAxis, *, year: int, month: int | None = None) -> int:
    """
    Get where a year, or a year and a month, falls on a natural axis

    Parameters
    ----------
    axis
        The natural axis to place the date on, yearly or monthly

    year
        Calendar year

    month
        Month of that year, 1 to 12, or `None` for a year on its own

        Required on a monthly axis, and refused on a yearly one, where it would
        have to be dropped.

    Returns
    -------
    :
        The date's ordinal position on `axis`

    Raises
    ------
    NoEpochError
        The axis is natural and finer than a month, so it has no ordinal at all

    ValueError
        The axis is dated, or `month` does not match the axis' step

    Notes
    -----
    Natural axes only. A date on a dated axis is an instant in a calendar, and putting
    one there is [convert_position][(m).convert_position].

    Examples
    --------
    >>> position_of_date(TimeAxis(TimeUnits(TimeStep.YEARS)), year=1850)
    1850

    >>> position_of_date(TimeAxis(TimeUnits(TimeStep.MONTHS)), year=1850, month=1)
    22189
    """
    if not axis.units.is_natural:
        msg = (
            f"A {axis.units} axis is dated, so a date on it is an instant in its "
            "calendar. Place one there with `convert_position`."
        )
        raise ValueError(msg)

    if axis.units.step == TimeStep.YEARS and month is None:
        return year

    if axis.units.step == TimeStep.MONTHS and month is not None:
        return total_months(year, month)

    if not axis.units.step.is_calendar_free:
        raise NoEpochError(axis.units)

    wanted = (
        "a year on its own"
        if axis.units.step == TimeStep.YEARS
        else "a year and a month"
    )
    msg = (
        f"A position on a {axis.units} axis is {wanted}, "
        f"got year={year} and month={month}."
    )
    raise ValueError(msg)


def convert_position(value: float, *, from_axis: TimeAxis, to_axis: TimeAxis) -> float:
    """
    Get where a position on one axis falls on another

    Parameters
    ----------
    value
        The position, on `from_axis`

    from_axis
        The axis `value` is a position on

    to_axis
        The axis wanted

    Returns
    -------
    :
        The same instant, or the same step, on `to_axis`

        - onto a **dated** axis, the instant itself, unrounded. From a natural axis
          that is the instant the step starts: month 22189 is 1850-01-01 00:00.
        - onto a **natural** axis, the step the instant falls in, a whole number. A
          step's first instant falls in that step, so a time bound at midnight on
          1861-01-01 lands on January 1861, which is what a half-open end needs.

    Raises
    ------
    MissingCalendarsExtraError
        A dated axis is involved and `cftime` is not installed

    UnknownCalendarError
        A dated axis involved does not know its calendar

    MismatchedTimeAxisError
        Both axes are dated and on different calendars, which no conversion bridges

    NotAWholeStepError
        `from_axis` is natural and `value` is not a whole number

    Examples
    --------
    Natural to natural needs nothing installed:

    >>> yearly = TimeAxis(TimeUnits(TimeStep.YEARS))
    >>> monthly = TimeAxis(TimeUnits(TimeStep.MONTHS))
    >>> convert_position(1850, from_axis=yearly, to_axis=monthly)
    22189

    >>> convert_position(22200, from_axis=monthly, to_axis=yearly)
    1850
    """
    check_position(value, from_axis)

    if from_axis.is_comparable_with(to_axis):
        return value

    if from_axis.units.is_natural and to_axis.units.is_natural:
        return _natural_to_natural(int(value), from_axis, to_axis)

    for axis in (from_axis, to_axis):
        if not axis.units.is_natural and axis.calendar is None:
            raise UnknownCalendarError(axis)

    if (
        not from_axis.units.is_natural
        and not to_axis.units.is_natural
        and from_axis.calendar != to_axis.calendar
    ):
        raise MismatchedTimeAxisError(from_axis, to_axis)

    cftime = _import_cftime()

    if from_axis.units.is_natural:
        year, month = _year_and_month_of_step(int(value), from_axis)
        at = cftime.datetime(year, month, 1, calendar=to_axis.calendar)
    else:
        at = cftime.num2date(value, str(from_axis.units), calendar=from_axis.calendar)

    if to_axis.units.is_natural:
        return _step_of(at.year, at.month, to_axis)

    # `cftime` ships no type information, so name what `date2num` gives back.
    return cast(
        "float", cftime.date2num(at, str(to_axis.units), calendar=to_axis.calendar)
    )


def _import_cftime() -> Any:
    """
    Import `cftime`, naming the extra if it is not installed

    Returns
    -------
    :
        The `cftime` module

    Raises
    ------
    MissingCalendarsExtraError
        `cftime` is not installed
    """
    try:
        import cftime  # noqa: PLC0415 - the optional `calendars` extra, imported on use
    except ImportError as error:
        raise MissingCalendarsExtraError from error

    return cftime


def _year_and_month_of_step(value: int, axis: TimeAxis) -> tuple[int, int]:
    """
    Get the year and month a step on a natural axis starts in

    Parameters
    ----------
    value
        The step's ordinal

    axis
        The natural axis, yearly or monthly

    Returns
    -------
    :
        Year and month, January for a year

    Raises
    ------
    NoEpochError
        The axis is natural and finer than a month
    """
    if axis.units.step == TimeStep.YEARS:
        return value, 1

    if axis.units.step == TimeStep.MONTHS:
        return year_and_month(value)

    raise NoEpochError(axis.units)


def _step_of(year: int, month: int, axis: TimeAxis) -> int:
    """
    Get the step of a natural axis a year and month fall in

    Parameters
    ----------
    year
        Calendar year

    month
        Month of that year, 1 to 12

    axis
        The natural axis, yearly or monthly

    Returns
    -------
    :
        The step's ordinal

    Raises
    ------
    NoEpochError
        The axis is natural and finer than a month
    """
    if axis.units.step == TimeStep.YEARS:
        return year

    if axis.units.step == TimeStep.MONTHS:
        return total_months(year, month)

    raise NoEpochError(axis.units)


def _natural_to_natural(value: int, from_axis: TimeAxis, to_axis: TimeAxis) -> int:
    """
    Get where a step on one natural axis falls on another

    Parameters
    ----------
    value
        The step's ordinal on `from_axis`

    from_axis
        Its axis, yearly or monthly

    to_axis
        The axis wanted, yearly or monthly

    Returns
    -------
    :
        The step on `to_axis` the start of `value` falls in
    """
    year, month = _year_and_month_of_step(value, from_axis)

    return _step_of(year, month, to_axis)


def _move(position: float, by: Span, axis: TimeAxis, *, backwards: bool) -> float:
    """
    Move along a time axis, in either direction

    Parameters
    ----------
    position
        Where to start, as a position on `axis`

    by
        How far to move

    axis
        The axis `position` is on

    backwards
        Whether to move back rather than forward

    Returns
    -------
    :
        Where the move lands, as a position on `axis`
    """
    if axis.units.step.is_calendar_free:
        steps = _whole_steps(by, axis)

        return position - steps if backwards else position + steps

    return _advance_on_a_dated_axis(position, by, axis, backwards=backwards)


def _whole_steps(by: Span, axis: TimeAxis) -> int:
    """
    Get a span as a whole number of the axis' own steps

    Parameters
    ----------
    by
        The span

    axis
        The axis it is being resolved against, a yearly or monthly one

    Returns
    -------
    :
        How many of the axis' steps long the span is

    Raises
    ------
    ValueError
        The span is finer than the axis' step, or is a number of months which is not
        a whole number of years
    """
    count = int(by.count)
    months = count * MONTHS_PER_YEAR if by.step == TimeStep.YEARS else count

    if axis.units.step == TimeStep.MONTHS and by.step in (
        TimeStep.YEARS,
        TimeStep.MONTHS,
    ):
        return months

    if (
        axis.units.step == TimeStep.YEARS
        and by.step in (TimeStep.YEARS, TimeStep.MONTHS)
        and months % MONTHS_PER_YEAR == 0
    ):
        return months // MONTHS_PER_YEAR

    msg = (
        f"{by} cannot be resolved against a {axis.units} axis: the axis has no step "
        f"smaller than one {axis.units.step.value.rstrip('s')}, so {by} does not name "
        f"a position on it. Either state the requirement in whole "
        f"{axis.units.step.value}, or ask for data whose frequency can answer it."
    )
    raise ValueError(msg)


def _advance_on_a_dated_axis(
    position: float, by: Span, axis: TimeAxis, *, backwards: bool
) -> float:
    """
    Move along a daily or sub-daily axis, which needs the calendar

    Parameters
    ----------
    position
        Where to start, as a position on `axis`

    by
        How far to move

    axis
        The axis `position` is on

    backwards
        Whether to move back rather than forward

    Returns
    -------
    :
        Where the move lands, as a position on `axis`, unrounded

    Raises
    ------
    MissingCalendarsExtraError
        `cftime` is not installed

    UnknownCalendarError
        The axis does not know its calendar

    NoEpochError
        The axis carries no epoch

    Notes
    -----
    Day-of-month is clamped to the length of the month landed in, which is the rule
    every calendar library uses and the only one which keeps the answer inside the
    month asked for -- 31 January plus one month is 28, 29 or 30 February depending on
    the calendar, not 3 March.
    """
    cftime = _import_cftime()

    if axis.units.epoch is None:
        raise NoEpochError(axis.units)

    if axis.calendar is None:
        raise UnknownCalendarError(axis)

    units = str(axis.units)
    at = cftime.num2date(position, units, calendar=axis.calendar)
    sign = -1 if backwards else 1

    if by.step.is_calendar_free:
        count = int(by.count)
        months = count * MONTHS_PER_YEAR if by.step == TimeStep.YEARS else count
        total = at.year * MONTHS_PER_YEAR + (at.month - 1) + sign * months
        year, month = divmod(total, MONTHS_PER_YEAR)
        month += 1

        # How many days that month has, asked of the calendar rather than assumed.
        first = at.replace(year=year, month=month, day=1)
        next_first = (
            first.replace(year=year + 1, month=1)
            if month == MONTHS_PER_YEAR
            else first.replace(month=month + 1)
        )
        moved = at.replace(
            year=year, month=month, day=min(at.day, (next_first - first).days)
        )
    elif by.step == TimeStep.DAYS:
        moved = at + datetime.timedelta(days=sign * by.count)
    elif by.step == TimeStep.HOURS:
        moved = at + datetime.timedelta(hours=sign * by.count)
    else:
        moved = at + datetime.timedelta(minutes=sign * by.count)

    # `cftime` ships no type information, so name what `date2num` gives back.
    return cast("float", cftime.date2num(moved, units, calendar=axis.calendar))
