"""
Moving along a time axis by a length of time

The one module here with real arithmetic in it, and a thin wrapper over `cftime`
rather than our own leap rules: six CF calendars with six sets of leap rules is
exactly the job which looks small and is not.

`cftime` comes from the optional `calendars` extra and is imported at point of use,
the way [esmporium.headers.hdf5][] does with `h5py`. Monthly and yearly axes never
reach that import, because twelve months is a year in every calendar -- so the common
path needs nothing installed, and only the sub-monthly path pays. Following the same
discipline as `hdf5.py`: nothing `cftime` returns reaches a signature, public or
private. Every value it hands back is reduced to an `int` before it leaves the two
private functions which touch it at all.

This lives in `esmporium.datasets` rather than beside the rest of enrichment because
[Covers][esmporium.requirements.Covers] needs it *at solve time*, and
`esmporium.requirements` may import `esmporium.datasets` and not `esmporium.enrich`.
The one rule `esmporium.datasets` has to keep is that it imports nothing else from
esmporium, which a third-party package does not breach.
"""

from __future__ import annotations

import datetime
import math
from typing import cast

from esmporium.datasets.time import (
    MONTHS_PER_YEAR,
    Span,
    TimeAxis,
    TimeStep,
    TimeUnits,
    total_months,
)


class MissingCalendarsExtraError(ImportError):
    """
    Raised when calendar arithmetic is needed and `cftime` is not installed

    An `ImportError` rather than anything of ours, because that is what it is: an
    environment missing a package. It is not a fact about the data and not a feature
    we have not written, so it must never become a solve status beside
    `NEEDS_ENRICHMENT`.
    """

    def __init__(self) -> None:
        """
        Initialise the error
        """
        super().__init__(
            "Calendar arithmetic on a daily or sub-daily axis needs cftime, which is "
            "the optional `calendars` extra: install `esmporium[calendars]`. Monthly "
            "and yearly axes do not need it, because twelve months is a year in "
            "every CF calendar."
        )


class UnknownCalendarError(ValueError):
    """
    Raised when a move along a sub-monthly axis is wanted and no calendar is known
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
            f"Moving along a {axis.units} axis needs its calendar, and this axis does "
            "not know one. A calendar is read from a file header, so this is work "
            "waiting to be done rather than a question with no answer: enrich the "
            "dataset and ask again."
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
            "have a calendar-free ordinal; anything finer is stored as "
            "'<step> since <epoch>' once a header has been read."
        )


def advance(position: int, by: Span, axis: TimeAxis) -> int:
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

    Notes
    -----
    Years and months are exact on a yearly or monthly axis with no calendar at all,
    which is the case nearly every requirement is: "twenty years after month `m`" is
    `m + 240` in all six CF calendars.

    They are also exact on a *daily* axis, given a calendar: "twenty years after
    1850-03-01" is 1870-03-01, unambiguously. The ambiguity people expect here only
    appears if you try to turn "twenty years" into a fixed number of days detached
    from a position on the axis, and there is never a reason to do that.

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


def retreat(position: int, by: Span, axis: TimeAxis) -> int:
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

    Notes
    -----
    A function of its own rather than a negative [Span][esmporium.datasets.Span],
    because a span is a *length* and lengths are not negative. It also keeps the
    end-of-month rule honest in both directions: "one month before 31 March" needs the
    same clamp as "one month after 31 January", and one function per direction means
    neither has to guess which way the other rounded.

    Examples
    --------
    >>> monthly = TimeAxis(TimeUnits(TimeStep.MONTHS))
    >>> retreat(22429, Span(20, TimeStep.YEARS), monthly)
    22189
    """
    return _move(position, by, axis, backwards=True)


def position_of_date(  # noqa: PLR0913 - a date is its components, and all are optional
    axis: TimeAxis,
    *,
    year: int,
    month: int = 1,
    day: int = 1,
    hour: int = 0,
    minute: int = 0,
) -> int:
    """
    Get where a date falls on a time axis

    The mirror of [advance][(m).advance]: that one puts a *length* onto an axis, this
    one puts a *date* onto it.

    Parameters
    ----------
    axis
        The axis to place the date on

    year
        Calendar year

    month
        Month of that year, 1 to 12

    day
        Day of that month, from 1

    hour
        Hour of that day, from 0

    minute
        Minute of that hour, from 0

    Returns
    -------
    :
        The date's position on `axis`

        Floored to a whole step, so a date part way through a step gives the step it
        falls in. Components finer than the axis' own step are therefore ignored: the
        position of 1850-07-15 on a yearly axis is 1850.

    Raises
    ------
    MissingCalendarsExtraError
        The axis is daily or finer and `cftime` is not installed

    UnknownCalendarError
        The axis is daily or finer and its calendar is not known

    NoEpochError
        The axis is daily or finer and carries no epoch

    Examples
    --------
    Natural axes need no calendar, because a year and a month are the same everywhere:

    >>> position_of_date(TimeAxis(TimeUnits(TimeStep.YEARS)), year=1850)
    1850

    >>> position_of_date(TimeAxis(TimeUnits(TimeStep.MONTHS)), year=1850, month=1)
    22189
    """
    if axis.units.is_natural:
        if axis.units.step == TimeStep.YEARS:
            return year

        if axis.units.step == TimeStep.MONTHS:
            return total_months(year, month)

        raise NoEpochError(axis.units)

    return _position_on_a_dated_axis(axis, (year, month, day, hour, minute))


def _position_on_a_dated_axis(
    axis: TimeAxis, moment: tuple[int, int, int, int, int]
) -> int:
    """
    Get where a date falls on an axis which counts from an epoch

    Parameters
    ----------
    axis
        The axis, which must know its calendar

    moment
        Year, month, day, hour and minute

    Returns
    -------
    :
        The date's position on `axis`, floored to a whole step

    Raises
    ------
    MissingCalendarsExtraError
        `cftime` is not installed

    UnknownCalendarError
        The axis does not know its calendar

    Notes
    -----
    Every `cftime` value stays inside this function; only an `int` leaves it.
    """
    try:
        import cftime  # noqa: PLC0415 - the optional `calendars` extra, imported on use
    except ImportError as error:
        raise MissingCalendarsExtraError from error

    if axis.calendar is None:
        raise UnknownCalendarError(axis)

    at = cftime.datetime(*moment, calendar=axis.calendar)

    # `cftime` ships no type information, so name what `date2num` gives back. The
    # floor is then a real `int` rather than one more `Any` wearing the annotation.
    elapsed = cast(
        "float", cftime.date2num(at, str(axis.units), calendar=axis.calendar)
    )

    return math.floor(elapsed)


def _move(position: int, by: Span, axis: TimeAxis, *, backwards: bool) -> int:
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
    if axis.units.step in (TimeStep.YEARS, TimeStep.MONTHS):
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
    months = by.count * MONTHS_PER_YEAR if by.step == TimeStep.YEARS else by.count

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
    position: int, by: Span, axis: TimeAxis, *, backwards: bool
) -> int:
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
        Where the move lands, as a position on `axis`

        Floored, so the answer is the step the date falls in rather than a fraction of
        one.

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
    Every `cftime` value stays inside this function, exactly as `hdf5.py` keeps every
    `h5py` value inside its own: the only thing which leaves is an `int`.

    Day-of-month is clamped to the length of the month landed in, which is the rule
    every calendar library uses and the only one which keeps the answer inside the
    month asked for -- 31 January plus one month is 28, 29 or 30 February depending on
    the calendar, not 3 March.
    """
    try:
        import cftime  # noqa: PLC0415 - the optional `calendars` extra, imported on use
    except ImportError as error:
        raise MissingCalendarsExtraError from error

    if axis.calendar is None:
        raise UnknownCalendarError(axis)

    if axis.units.epoch is None:
        raise NoEpochError(axis.units)

    units = str(axis.units)
    at = cftime.num2date(position, units, calendar=axis.calendar)
    sign = -1 if backwards else 1

    if by.step in (TimeStep.YEARS, TimeStep.MONTHS):
        months = by.count * MONTHS_PER_YEAR if by.step == TimeStep.YEARS else by.count
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

    elapsed = cast("float", cftime.date2num(moved, units, calendar=axis.calendar))

    return math.floor(elapsed)
