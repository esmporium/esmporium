"""
When a dataset's data is, how far apart two instants are, and where a run branched

The information ESGF's search APIs may not publish, as values,
and the arithmetic on them.

Three kinds of value, kept apart because mixing them up is how a time comparison goes
quietly wrong:

- a [TimePoint][(m).TimePoint] is one instant, as a number read off a stated
  [TimeAxis][(m).TimeAxis];
- a [TimeDelta][(m).TimeDelta] is a length of time, with no axis,
  and optionally the calendar it was measured in;
- a [TimeRange][(m).TimeRange] is two time points on one axis.

Subtracting two time points gives a time delta
([TimePoint.delta_from][(m).TimePoint.delta_from]),
and shifting a time point by a time delta gives a time point
([TimePoint.plus][(m).TimePoint.plus]).
Both check, at that line, that the two can be combined.

Two conventions are carried by the types rather than by docstrings, because both are
off-by-one traps:

- a range is **half-open**, `[start, end_exclusive)`, so
  [extent][(m).TimeRange.extent] is one subtraction with no correction term, and two
  files are adjacent exactly when `previous.end_exclusive == next.start`;
- a **natural** value is a 1-based ordinal step count with no epoch, so the first
  month of year 1 is month 1. A udunits `since` epoch counts *elapsed* steps and would
  make it month 0.

Time points are held as given. On a natural axis a time point is a whole number of
steps, and anything else is refused; on a dated axis it is whatever was published,
fraction and all, because a branch time or a time bound is a float and rounding it
would be a conversion nobody asked for. Moving between axes is
[convert_time_point][(m).convert_time_point].

Why these types rather than `cftime`'s own.
`cftime` is the right tool for one thing --
what date a number on a dated axis is, in a given calendar --
and it is what [TimePoint.plus][(m).TimePoint.plus] and
[convert_time_point][(m).convert_time_point] use for that.
It cannot carry the rest, which is most of what we need:

- it has no natural units.
  A `cftime.datetime` is always an instant in a calendar,
  and a count of years or months with no epoch and no calendar --
  which is what a monthly filename gives us,
  and what lets monthly and yearly data be judged with nothing opened --
  is not something it can represent;
- its difference of two instants is a `datetime.timedelta`,
  which counts days and seconds only.
  There is no delta of years or months,
  so "twenty years of overlap" cannot be stated in it at all,
  only resolved against a start date;
- that `timedelta` carries no calendar,
  so nothing stops a day count measured in `noleap`
  from being walked on a `360_day` axis as if it were a number of years,
  and nothing can say that 730 `noleap` days is exactly two years;
- it is an optional extra, and the common, monthly path must not need it.

So the values, and the rules for when two of them can be combined, are ours,
and `cftime` is only asked the calendar questions.

Calendar arithmetic is a thin wrapper over `cftime` rather than our own leap rules:
six CF calendars with six sets of leap rules is
exactly the job which looks small and is not.
`cftime` comes from the optional `calendars` extra
and is imported at the point of use, never when this module is imported.
Monthly and yearly axes never reach that import,
because twelve months is a year in every calendar,
so the common path needs nothing installed,
and only the sub-monthly path and conversions onto or off a dated axis pay.

This lives in `esmporium.datasets` rather than beside the rest of enrichment because
[Covers][esmporium.requirements.Covers] moves along axes *at solve time*, and
`esmporium.requirements` may import `esmporium.datasets` and not `esmporium.enrich`.
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

MONTHS_PER_YEAR = 12
"""
How many months there are in a year, in every CF calendar
"""


class TimeStep(str, Enum):
    """
    The size of one step along a time axis
    """

    YEARS = "years"
    """Years"""

    MONTHS = "months"
    """Months"""

    DAYS = "days"
    """Days"""

    HOURS = "hours"
    """Hours"""

    MINUTES = "minutes"
    """
    Minutes

    Here because CMIP6 stamps its sub-daily filenames to the minute
    (`..._185001010300-185101010000.nc`), not because any dataset reports every
    minute. It is a precision a filename is written to more often than it is an axis
    anything is stored on.
    """

    @property
    def is_calendar_free(self) -> bool:
        """
        Whether counting steps of this size is the same in every calendar

        Returns
        -------
        :
            `True` for years and months, `False` for anything finer

        Notes
        -----
        This is the line the whole enrichment step is organised around. Twelve months
        is a year in all six CF calendars, so a monthly or yearly dataset's extent can
        be worked out from its filename alone. A day count cannot: how many days lie
        between two dates is exactly what the calendars disagree about.

        Examples
        --------
        >>> TimeStep.MONTHS.is_calendar_free
        True

        >>> TimeStep.DAYS.is_calendar_free
        False
        """
        return self in (TimeStep.YEARS, TimeStep.MONTHS)

    @property
    def singular(self) -> str:
        """
        Get the name of one step, for a message

        Returns
        -------
        :
            E.g. `"month"`
        """
        return self.value.rstrip("s")


_COARSEST_FIRST = (
    TimeStep.YEARS,
    TimeStep.MONTHS,
    TimeStep.DAYS,
    TimeStep.HOURS,
    TimeStep.MINUTES,
)
"""Every step, from the longest to the shortest"""

_MINUTES_PER_EXACT_STEP: Mapping[TimeStep, int] = {
    TimeStep.DAYS: 24 * 60,
    TimeStep.HOURS: 60,
    TimeStep.MINUTES: 1,
}
"""
How many minutes one step of each exact size is

The same in every CF calendar: none of them has leap seconds,
so a day is twenty-four hours everywhere.
"""

_DAYS_PER_NOMINAL_STEP_BY_CALENDAR: Mapping[str, Mapping[TimeStep, int]] = {
    "noleap": {TimeStep.YEARS: 365},
    "365_day": {TimeStep.YEARS: 365},
    "all_leap": {TimeStep.YEARS: 366},
    "366_day": {TimeStep.YEARS: 366},
    "360_day": {TimeStep.YEARS: 360, TimeStep.MONTHS: 30},
}
"""
How many days a year or a month is, in the calendars where that is fixed

Only these. In `standard`, `gregorian`, `julian` and `proleptic_gregorian`
a year is 365 or 366 days depending on which year it is,
and in every calendar but `360_day` a month is 28 to 31 days depending on which month.
Those need a point to count from, which is [TimePoint.plus][(m).TimePoint.plus].
"""


class UnknownTimeUnitsError(ValueError):
    """
    Raised when a units string is not one we know how to read
    """

    def __init__(self, text: str) -> None:
        """
        Initialise the error

        Parameters
        ----------
        text
            The units string which could not be read
        """
        self.text = text
        known = ", ".join(step.value for step in TimeStep)
        super().__init__(
            f"{text!r} is not a units string this reads. Expected one of {known} "
            "on its own, for a natural value, or '<step> since <epoch>' for a rich "
            "one, e.g. 'days since 1850-01-01'."
        )


@dataclass(frozen=True)
class TimeUnits:
    """
    What the numbers on a time axis count, and from where

    Two shapes, and `epoch` is what tells them apart:

    - **natural**, `epoch is None` -- a 1-based ordinal count of steps, so the first
      month of year 1 is `1`. No epoch, deliberately: a count of years or months is
      the same count in every calendar, and an epoch date would imply a calendar the
      value does not carry.
    - **rich**, `epoch` set -- udunits' own form, counting elapsed steps from a date,
      which is what a file header writes.

    Examples
    --------
    >>> str(TimeUnits(TimeStep.MONTHS))
    'months'

    >>> str(TimeUnits(TimeStep.DAYS, epoch="1850-01-01"))
    'days since 1850-01-01'
    """

    step: TimeStep
    """The size of one step"""

    epoch: str | None = None
    """
    The date the count starts from, as the file wrote it

    `None` means there is no epoch, which is what makes the value a 1-based ordinal.
    Kept as text rather than parsed into a date, because interpreting it needs the
    calendar, and a CMIP file can write `1850-1-1` or `1850-01-01 00:00:00` for the
    same instant.
    """

    @property
    def is_natural(self) -> bool:
        """
        Whether these are natural units, i.e. an ordinal count with no epoch

        Returns
        -------
        :
            `True` if there is no epoch
        """
        return self.epoch is None

    def __str__(self) -> str:
        """
        Render as the string a `units` column stores

        Returns
        -------
        :
            The step on its own for natural units, `'<step> since <epoch>'` for rich
        """
        if self.epoch is None:
            return self.step.value

        return f"{self.step.value} since {self.epoch}"

    @classmethod
    def parse(cls, text: str) -> TimeUnits:
        """
        Read a units string back

        Splits `<step> since <epoch>` and does no more. The epoch is kept as the text
        the file wrote, because interpreting it -- partial dates, times of day, the
        calendar it is a date in -- is what `cftime` does and is not repeated here.

        Parameters
        ----------
        text
            A units string, as [__str__][(c).__str__] renders one or as a file header
            writes one

            Singular step names are accepted, because udunits allows them.

        Returns
        -------
        :
            The units it names

        Raises
        ------
        UnknownTimeUnitsError
            The step is not one we read, or the string is not one of the two shapes

        Examples
        --------
        >>> TimeUnits.parse("months") == TimeUnits(TimeStep.MONTHS)
        True

        >>> TimeUnits.parse("day since 1850-1-1").epoch
        '1850-1-1'

        >>> try:
        ...     TimeUnits.parse("fortnights since 1850-01-01")
        ... except UnknownTimeUnitsError as error:
        ...     error.text
        'fortnights since 1850-01-01'
        """
        # The whole string is stripped first, so a trailing `" since "` cannot match
        # and there is no "named an epoch but did not give one" case to handle: such a
        # string reaches `_step_named` as `"days since"`, which is not a step.
        before, _, after = text.strip().partition(" since ")
        step_text, epoch = before.strip(), after.strip()

        step = _step_named(step_text)
        if step is None:
            raise UnknownTimeUnitsError(text)

        return cls(step, epoch=epoch or None)


def _step_named(text: str) -> TimeStep | None:
    """
    Get the step a name refers to, plural or singular

    Parameters
    ----------
    text
        The name, e.g. `"months"` or `"month"`

    Returns
    -------
    :
        The step, or `None` if the name is not one of ours
    """
    for step in TimeStep:
        if text in (step.value, step.singular):
            return step

    return None


@dataclass(frozen=True)
class TimeAxis:
    """
    The axis a dataset's time values live on

    A pair of time points is only comparable when they are on axes which agree -- see
    [is_comparable_with][(c).is_comparable_with] -- which is why this is a type rather
    than two loose fields.

    A natural axis has no calendar, and is refused if given one: a count of years or
    months is the same count in every calendar, so a calendar on it would say nothing.
    """

    units: TimeUnits
    """What the numbers count, and from where"""

    calendar: str | None = None
    """
    The CF calendar name, e.g. `standard`, `noleap`, `360_day`

    Always `None` on a natural axis. On a dated axis `None` means the calendar is not
    known, and such an axis is comparable with nothing, itself included: guessing a
    calendar is how a day count silently comes out wrong.
    """

    def __post_init__(self) -> None:
        """
        Check a natural axis does not carry a calendar

        Raises
        ------
        CalendarOnANaturalAxisError
            The units are natural and a calendar was given
        """
        if self.units.is_natural and self.calendar is not None:
            raise CalendarOnANaturalAxisError(self.units, self.calendar)

    def is_comparable_with(self, other: TimeAxis) -> bool:
        """
        Determine whether time points on this axis can be compared with another's

        Parameters
        ----------
        other
            The other axis

        Returns
        -------
        :
            `True` if a number on one axis means the same instant, or the same step,
            as that number on the other

        Notes
        -----
        Comparable means the numbers can be compared as they stand, with no
        conversion:

        - **natural against natural**: the same step. Neither carries a calendar,
          because a natural axis cannot.
        - **dated against dated**: the same units, epoch included, and the same
          calendar, which both have to know. Two dated axes on one calendar with
          different epochs or steps are not comparable as they stand, and are once one
          is converted onto the other.
        - **natural against dated**: never. A month count and a day count are related
          only through a calendar, by a conversion.

        Conversions are [convert_time_point][(m).convert_time_point].

        Examples
        --------
        >>> monthly = TimeAxis(TimeUnits(TimeStep.MONTHS))
        >>> monthly.is_comparable_with(TimeAxis(TimeUnits(TimeStep.MONTHS)))
        True

        >>> noleap = TimeAxis(TimeUnits(TimeStep.DAYS, "1850-01-01"), "noleap")
        >>> gregorian = TimeAxis(TimeUnits(TimeStep.DAYS, "1850-01-01"), "standard")
        >>> noleap.is_comparable_with(gregorian)
        False

        >>> unknown = TimeAxis(TimeUnits(TimeStep.DAYS, "1850-01-01"))
        >>> unknown.is_comparable_with(unknown)
        False

        >>> monthly.is_comparable_with(noleap)
        False
        """
        if self.units != other.units:
            return False

        if self.units.is_natural:
            return True

        if self.calendar is None or other.calendar is None:
            return False

        # Strict on purpose: different calendars are never comparable,
        # so nothing slips through by accident.
        # We may have to loosen this once we work with real data at scale,
        # because some models use different calendars for a control and its experiments.
        # Sometimes that can be worked around
        # (with monthly data you usually don't care which calendar each run used),
        # and sometimes it is a real problem,
        # so any loosening has to say which case it allows,
        # rather than dropping the check.
        return self.calendar == other.calendar


class CalendarOnANaturalAxisError(ValueError):
    """
    Raised when a natural axis is given a calendar
    """

    def __init__(self, units: TimeUnits, calendar: str) -> None:
        """
        Initialise the error

        Parameters
        ----------
        units
            The natural units

        calendar
            The calendar which was given with them
        """
        self.units = units
        self.calendar = calendar
        super().__init__(
            f"A {units} axis is a count of {units.step.value} with no epoch, which is "
            f"the same count in every calendar, so it cannot carry calendar "
            f"{calendar!r}. A calendar belongs to a dated axis, '<step> since <epoch>'."
        )


class NotAWholeStepError(ValueError):
    """
    Raised when a time point on a natural axis is not a whole number of steps
    """

    def __init__(self, value: float, axis: TimeAxis) -> None:
        """
        Initialise the error

        Parameters
        ----------
        value
            The value which was given

        axis
            The natural axis it was given on
        """
        self.value = value
        self.axis = axis
        super().__init__(
            f"{value!r} is not a time point on a {axis.units} axis, which counts whole "
            f"{axis.units.step.value}. A fractional value is a time point on a dated "
            f"axis; putting it on a natural one is a conversion, done by "
            f"`esmporium.datasets.convert_time_point`."
        )


class MismatchedTimeAxisError(ValueError):
    """
    Raised when time points or ranges on axes which do not agree are combined
    """

    def __init__(self, left: TimeAxis, right: TimeAxis) -> None:
        """
        Initialise the error

        Parameters
        ----------
        left
            Axis of the value the comparison was asked of

        right
            Axis of the value it was asked about
        """
        self.left = left
        self.right = right
        super().__init__(
            f"These time values are not on comparable axes: {left.units} in calendar "
            f"{left.calendar!r} against {right.units} in calendar {right.calendar!r}. "
            f"Convert one onto the other first, with "
            f"`esmporium.datasets.convert_time_point`."
        )


class EmptyTimeRangeError(ValueError):
    """
    Raised when a time range would hold no data at all
    """

    def __init__(self, start: float, end_exclusive: float) -> None:
        """
        Initialise the error

        Parameters
        ----------
        start
            The first step in the range

        end_exclusive
            The first step after it
        """
        self.start = start
        self.end_exclusive = end_exclusive
        super().__init__(
            f"end_exclusive must be greater than start, got start={start} and "
            f"end_exclusive={end_exclusive}, i.e. a range holding no steps. "
            f"end_exclusive is the first step *not* in the data, so a dataset with a "
            f"single step has end_exclusive = start + 1."
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


class FractionalCalendarFreeDeltaError(ValueError):
    """
    Raised when a time delta of years or months is not a whole number of them
    """

    def __init__(self, count: float, step: TimeStep) -> None:
        """
        Initialise the error

        Parameters
        ----------
        count
            The count which was given

        step
            The step it counted
        """
        self.count = count
        self.step = step
        super().__init__(
            f"A time delta of {step.value} is a whole number of them, but got {count}. "
            f"State a part of a year in months, e.g. 6 months rather than 0.5 years."
        )


class NegativeLengthError(ValueError):
    """
    Raised when a time delta which has to be a length is negative
    """

    def __init__(self, delta: TimeDelta) -> None:
        """
        Initialise the error

        Parameters
        ----------
        delta
            The time delta which was given
        """
        self.delta = delta
        super().__init__(
            f"A length of time cannot be negative, but got {delta}. To ask for data "
            "before a point rather than after it, say so at the point of use rather "
            "than with a negative length."
        )


class NeedsAnAnchorError(ValueError):
    """
    Raised when a time delta's calendar does not fix how long it is in another step
    """

    def __init__(self, delta: TimeDelta, step: TimeStep) -> None:
        """
        Initialise the error

        Parameters
        ----------
        delta
            The time delta which was to be converted

        step
            The step it was to be converted to
        """
        self.delta = delta
        self.step = step
        calendar = (
            "with no calendar"
            if delta.calendar is None
            else f"in calendar {delta.calendar!r}"
        )
        super().__init__(
            f"{delta} cannot be counted in {step.value} on its own: {calendar}, how "
            "many days a year or a month is depends on which year or month it is, so "
            "it depends on where it starts. Apply it to a time point with "
            "`TimePoint.plus` instead, which knows where it starts. Only "
            "`noleap`/`365_day`, `all_leap`/`366_day` and `360_day` fix the length "
            "of a year, and only `360_day` fixes the length of a month."
        )


class DeltaCalendarMismatchError(ValueError):
    """
    Raised when years or months measured in one calendar are applied in another
    """

    def __init__(self, delta: TimeDelta, axis: TimeAxis) -> None:
        """
        Initialise the error

        Parameters
        ----------
        delta
            The time delta, which carries a calendar

        axis
            The axis it was applied on
        """
        self.delta = delta
        self.axis = axis
        super().__init__(
            f"{delta} is a length in calendar {delta.calendar!r}, and it was applied "
            f"on a {axis.units} axis in calendar {axis.calendar!r}. How long a year "
            "or a month is, is exactly what calendars disagree about, so it is not "
            "carried across them silently. For the same number of "
            f"{delta.step.value} in any calendar, use `delta.in_any_calendar()`; "
            "for the same number of days, where its calendar fixes them, use "
            "`delta.in_steps(TimeStep.DAYS)`."
        )


class NotAStepOnThisAxisError(ValueError):
    """
    Raised when a time delta does not move a natural axis a whole number of steps
    """

    def __init__(self, delta: TimeDelta, axis: TimeAxis) -> None:
        """
        Initialise the error

        Parameters
        ----------
        delta
            The time delta

        axis
            The natural axis it was applied on
        """
        self.delta = delta
        self.axis = axis
        step = axis.units.step
        super().__init__(
            f"{delta} cannot be resolved against a {axis.units} axis: the axis has no "
            f"step smaller than one {step.singular}, so {delta} does not name a time "
            f"point on it. Either state it in whole {step.value}, convert it with "
            "`TimeDelta.in_steps` where its calendar fixes the length, or ask for "
            "data whose frequency can answer it."
        )


def check_time_point_value(value: float, axis: TimeAxis) -> None:
    """
    Refuse a value which a natural axis cannot hold

    Parameters
    ----------
    value
        The value

    axis
        The axis it is a time point on

    Raises
    ------
    NotAWholeStepError
        `axis` is natural and `value` is not a whole number

    Examples
    --------
    >>> check_time_point_value(22189, TimeAxis(TimeUnits(TimeStep.MONTHS)))

    >>> check_time_point_value(
    ...     438000.5, TimeAxis(TimeUnits(TimeStep.DAYS, "1850-01-01"))
    ... )

    >>> try:
    ...     check_time_point_value(1850.5, TimeAxis(TimeUnits(TimeStep.YEARS)))
    ... except NotAWholeStepError as error:
    ...     error.value
    1850.5
    """
    if axis.units.is_natural and not float(value).is_integer():
        raise NotAWholeStepError(value, axis)


@dataclass(frozen=True)
class TimeDelta:
    """
    A length of time: "twenty years of overlap", "730 days after the branch"

    Signed, because the difference of two time points can go either way.
    Where a length has to be positive, e.g. padding in a requirement,
    the place which needs it says so.

    Two kinds of step, which behave differently:

    - **nominal** -- years and months.
      Twelve months is a year in every calendar,
      but how many days a year is depends on the calendar
      and, in most calendars, on which year it is.
      So a nominal count is always whole.
    - **exact** -- days, hours and minutes.
      A day is twenty-four hours in every CF calendar.
      An exact count may be fractional,
      because that is what the extent of sub-daily data on a `days since` axis comes to.

    `calendar` is the calendar the length was measured in,
    e.g. the calendar of the two time points it is the difference of.
    `None` means it applies in any calendar,
    which is what a length stated in a requirement means.
    What it does:

    - [in_steps][(c).in_steps] can convert between years and days
      when the calendar fixes how long a year is (`noleap`, `360_day`, ...);
    - [TimePoint.plus][(m).TimePoint.plus] refuses to apply
      a delta of years or months measured in one calendar on an axis in another,
      because what "a year" is, is where calendars disagree.
      A delta of days is walked as it is, in any calendar.

    Equality is strict: `12 months` is not equal to `1 year`.
    Compare after [in_steps][(c).in_steps] to compare lengths.

    Examples
    --------
    >>> str(TimeDelta(20, TimeStep.YEARS))
    '20 years'

    >>> str(TimeDelta(1, TimeStep.YEARS))
    '1 year'

    >>> str(TimeDelta(730, TimeStep.DAYS, "noleap"))
    '730 days (noleap)'
    """

    count: int | float
    """How many steps long, negative for a length backwards in time"""

    step: TimeStep
    """The size of one step"""

    calendar: str | None = None
    """
    The CF calendar the length was measured in, or `None` for any calendar
    """

    def __post_init__(self) -> None:
        """
        Check the length is whole where it has to be

        Raises
        ------
        FractionalCalendarFreeDeltaError
            `step` is years or months and `count` is not a whole number
        """
        if self.step.is_calendar_free and not float(self.count).is_integer():
            raise FractionalCalendarFreeDeltaError(self.count, self.step)

    def __str__(self) -> str:
        """
        Render for a message a user reads

        Returns
        -------
        :
            The count and the step, singular when the count is one,
            then the calendar in brackets if there is one
        """
        name = self.step.singular if self.count == 1 else self.step.value
        rendered = f"{self.count} {name}"
        if self.calendar is None:
            return rendered

        return f"{rendered} ({self.calendar})"

    @property
    def is_negative(self) -> bool:
        """
        Whether this is a length backwards in time

        Returns
        -------
        :
            `True` if `count` is less than zero
        """
        return self.count < 0

    def negated(self) -> TimeDelta:
        """
        Get the same length in the other direction

        Returns
        -------
        :
            The same time delta with its count negated

        Examples
        --------
        >>> str(TimeDelta(10, TimeStep.YEARS).negated())
        '-10 years'
        """
        return replace(self, count=-self.count)

    def in_any_calendar(self) -> TimeDelta:
        """
        Get the same length, applicable in any calendar

        The explicit way to apply a delta of years or months
        measured in one calendar on an axis in another,
        so the choice is visible where it is made.

        Returns
        -------
        :
            The same time delta with no calendar

        Examples
        --------
        >>> TimeDelta(2, TimeStep.YEARS, "noleap").in_any_calendar().calendar is None
        True
        """
        return replace(self, calendar=None)

    def in_steps(self, step: TimeStep) -> TimeDelta:
        """
        Get the same length, counted in a different step

        Parameters
        ----------
        step
            The step to count in

        Returns
        -------
        :
            The same length in `step`, in the same calendar

            - Years and months convert in any calendar, twelve to one.
            - Days, hours and minutes convert in any calendar.
            - Between the two, only where the calendar fixes the length:
              a year in `noleap`/`365_day`, `all_leap`/`366_day` and `360_day`,
              and a month in `360_day`.

        Raises
        ------
        NeedsAnAnchorError
            Converting between years or months and days, hours or minutes,
            and the calendar does not fix how long a year or month is

        FractionalCalendarFreeDeltaError
            The answer is a number of years or months which is not whole,
            e.g. 13 months in years

        Examples
        --------
        >>> str(TimeDelta(2, TimeStep.YEARS).in_steps(TimeStep.MONTHS))
        '24 months'

        >>> str(TimeDelta(90, TimeStep.MINUTES).in_steps(TimeStep.HOURS))
        '1.5 hours'

        >>> str(TimeDelta(73000, TimeStep.DAYS, "noleap").in_steps(TimeStep.YEARS))
        '200 years (noleap)'

        >>> try:
        ...     TimeDelta(1, TimeStep.YEARS, "standard").in_steps(TimeStep.DAYS)
        ... except NeedsAnAnchorError as error:
        ...     error.step
        <TimeStep.DAYS: 'days'>
        """
        if step is self.step:
            return self

        from_nominal = self.step.is_calendar_free
        to_nominal = step.is_calendar_free

        if from_nominal and to_nominal:
            months = _months_in(self)
            count = months if step is TimeStep.MONTHS else months / MONTHS_PER_YEAR

            return TimeDelta(_tidy(count), step, self.calendar)

        if not from_nominal and not to_nominal:
            minutes = self.count * _MINUTES_PER_EXACT_STEP[self.step]

            return TimeDelta(
                _tidy(minutes / _MINUTES_PER_EXACT_STEP[step]), step, self.calendar
            )

        minutes_per_day = _MINUTES_PER_EXACT_STEP[TimeStep.DAYS]
        if from_nominal:
            days = self.count * self._days_per(self.step, step)

            return TimeDelta(
                _tidy(days * minutes_per_day / _MINUTES_PER_EXACT_STEP[step]),
                step,
                self.calendar,
            )

        days = self.count * _MINUTES_PER_EXACT_STEP[self.step] / minutes_per_day

        return TimeDelta(_tidy(days / self._days_per(step, step)), step, self.calendar)

    def _days_per(self, nominal: TimeStep, wanted: TimeStep) -> int:
        """
        Get how many days one year or month is, in this delta's calendar

        Parameters
        ----------
        nominal
            Years or months

        wanted
            The step being converted to, named in the error

        Returns
        -------
        :
            How many days one `nominal` step is

        Raises
        ------
        NeedsAnAnchorError
            The calendar does not fix it
        """
        fixed = _DAYS_PER_NOMINAL_STEP_BY_CALENDAR.get(self.calendar or "", {})
        if nominal not in fixed:
            raise NeedsAnAnchorError(self, wanted)

        return fixed[nominal]


def _months_in(delta: TimeDelta) -> int:
    """
    Get how many months a time delta of years or months is

    Parameters
    ----------
    delta
        The time delta, of years or months

    Returns
    -------
    :
        Its length in months
    """
    count = int(delta.count)

    return count * MONTHS_PER_YEAR if delta.step is TimeStep.YEARS else count


def _tidy(count: float) -> int | float:
    """
    Get a count as an `int` when it is whole

    So that a conversion which comes out whole reads as a whole number in a message.

    Parameters
    ----------
    count
        The count

    Returns
    -------
    :
        `count` as an `int` if it is whole, otherwise unchanged
    """
    if float(count).is_integer():
        return int(count)

    return count


# Developer note:
# Named methods rather than operators -- `later.delta_from(earlier)` rather than
# `later - earlier`, `point.plus(by)` rather than `point + by`.
# Operators could be offered as well, delegating to these,
# but for now we prefer clarity over syntactic beauty:
# each of these can refuse, some only with the `calendars` extra missing,
# and a named method is where a reader looks for that.
@dataclass(frozen=True)
class TimePoint:
    """
    A point in time, as a number read off a particular axis

    A number on its own does not say which axis it was read off, and two numbers off
    different axes cannot be compared or subtracted as they stand: day 91615 since 1850
    and day 182500 since 0001 are just readings off two rulers with different zeros.
    So the number and its axis are held together, and anything which combines two
    time points can check they are on the same ruler first.

    On a natural axis the value has to be a whole number of steps; on a dated axis it
    is as published, fraction and all.

    Examples
    --------
    >>> str(TimePoint(22189, TimeAxis(TimeUnits(TimeStep.MONTHS))))
    '22189 months'

    >>> noleap = TimeAxis(TimeUnits(TimeStep.DAYS, "1850-01-01"), "noleap")
    >>> str(TimePoint(91615, noleap))
    "91615 days since 1850-01-01, calendar 'noleap'"

    A time bound on a dated axis is kept as published, fraction and all,
    e.g. three hours into 1 January 2101:

    >>> str(TimePoint(91615.125, noleap))
    "91615.125 days since 1850-01-01, calendar 'noleap'"

    A fraction on a natural axis is refused, because there it would be part of a step:

    >>> try:
    ...     TimePoint(22189.5, TimeAxis(TimeUnits(TimeStep.MONTHS)))
    ... except NotAWholeStepError as error:
    ...     error.value
    22189.5
    """

    value: float
    """The number, read off `axis`"""

    axis: TimeAxis
    """The axis `value` is read off"""

    def __post_init__(self) -> None:
        """
        Check the value is one its axis can hold

        Raises
        ------
        NotAWholeStepError
            The axis is natural and the value is not a whole number
        """
        check_time_point_value(self.value, self.axis)

    def __str__(self) -> str:
        """
        Get the time point as text for a message

        Returns
        -------
        :
            The value and its units, and its calendar when the axis names one
        """
        if self.axis.calendar is None:
            return f"{self.value} {self.axis.units}"

        return f"{self.value} {self.axis.units}, calendar {self.axis.calendar!r}"

    @classmethod
    def from_year_and_month(
        cls, axis: TimeAxis, *, year: int, month: int | None = None
    ) -> TimePoint:
        """
        Get where a year, or a year and a month, falls on a natural axis

        Parameters
        ----------
        axis
            The natural axis to place it on, yearly or monthly

        year
            Calendar year

        month
            Month of that year, 1 to 12, or `None` for a year on its own

            Required on a monthly axis, and refused on a yearly one, where it would
            have to be dropped.

        Returns
        -------
        :
            The year and month as a time point on `axis`

        Raises
        ------
        NoEpochError
            The axis is natural and finer than a month, so it has no ordinal at all

        ValueError
            The axis is dated, or `month` does not match the axis' step

        Notes
        -----
        Natural axes only. A year and month on a dated axis is an instant in a
        calendar, and putting one there is
        [convert_time_point][(m).convert_time_point].

        Examples
        --------
        >>> TimePoint.from_year_and_month(
        ...     TimeAxis(TimeUnits(TimeStep.YEARS)), year=1850
        ... ).value
        1850

        >>> TimePoint.from_year_and_month(
        ...     TimeAxis(TimeUnits(TimeStep.MONTHS)), year=1850, month=1
        ... ).value
        22189
        """
        if not axis.units.is_natural:
            msg = (
                f"A {axis.units} axis is dated, so a year and month on it is an "
                "instant in its calendar. Place one there with `convert_time_point`."
            )
            raise ValueError(msg)

        if axis.units.step == TimeStep.YEARS and month is None:
            return cls(year, axis)

        if axis.units.step == TimeStep.MONTHS and month is not None:
            return cls(total_months(year, month), axis)

        if not axis.units.step.is_calendar_free:
            raise NoEpochError(axis.units)

        wanted = (
            "a year on its own"
            if axis.units.step == TimeStep.YEARS
            else "a year and a month"
        )
        msg = (
            f"A time point on a {axis.units} axis is {wanted}, "
            f"got year={year} and month={month}."
        )
        raise ValueError(msg)

    def is_before(self, other: TimePoint) -> bool:
        """
        Determine whether this time point comes before another

        Parameters
        ----------
        other
            The other time point

        Returns
        -------
        :
            `True` if this one is strictly earlier

        Raises
        ------
        MismatchedTimeAxisError
            The two are not on comparable axes

        Examples
        --------
        >>> monthly = TimeAxis(TimeUnits(TimeStep.MONTHS))
        >>> TimePoint(1, monthly).is_before(TimePoint(2, monthly))
        True
        """
        self._check_comparable_with(other)

        return self.value < other.value

    def is_at_or_before(self, other: TimePoint) -> bool:
        """
        Determine whether this time point is another or comes before it

        Parameters
        ----------
        other
            The other time point

        Returns
        -------
        :
            `True` if this one is the same instant or earlier

        Raises
        ------
        MismatchedTimeAxisError
            The two are not on comparable axes

        Examples
        --------
        >>> monthly = TimeAxis(TimeUnits(TimeStep.MONTHS))
        >>> TimePoint(2, monthly).is_at_or_before(TimePoint(2, monthly))
        True
        """
        self._check_comparable_with(other)

        return self.value <= other.value

    def _check_comparable_with(self, other: TimePoint) -> None:
        """
        Refuse another time point whose number cannot be compared with this one's

        Parameters
        ----------
        other
            The other time point

        Raises
        ------
        MismatchedTimeAxisError
            The two are not on comparable axes
        """
        if not self.axis.is_comparable_with(other.axis):
            raise MismatchedTimeAxisError(self.axis, other.axis)

    def delta_from(self, earlier: TimePoint) -> TimeDelta:
        """
        Get how long after another time point this one is

        Parameters
        ----------
        earlier
            The time point to measure from

        Returns
        -------
        :
            The length from `earlier` to this one,
            negative if `earlier` is in fact later

            Counted in this time point's step,
            or in `earlier`'s where that is finer, so that nothing is rounded,
            and tagged with the axes' calendar (`None` on a natural axis).

        Raises
        ------
        MismatchedTimeAxisError
            The two are in different calendars,
            or one is on a natural axis and the other on a dated one:
            relating those would mean assuming a calendar

        UnknownCalendarError
            The two are on dated axes and one does not know its calendar

        MissingCalendarsExtraError
            The two are on dated axes with different units,
            so one has to be converted onto the other's axis,
            and `cftime` is not installed

        NoEpochError
            The two are on different natural axes and one is finer than a month

        Notes
        -----
        Two time points on comparable axes are subtracted as they stand.
        Otherwise, as long as they share a known calendar
        (or are both natural, where there is none),
        the coarser one is moved onto the finer one's axis first,
        which is how two dated axes with different epochs are subtracted.

        Examples
        --------
        >>> monthly = TimeAxis(TimeUnits(TimeStep.MONTHS))
        >>> str(TimePoint(22429, monthly).delta_from(TimePoint(22189, monthly)))
        '240 months'

        A year against a month is counted in months, so that nothing is rounded:

        >>> yearly = TimeAxis(TimeUnits(TimeStep.YEARS))
        >>> str(TimePoint(1860, yearly).delta_from(TimePoint(22200, monthly)))
        '109 months'

        On a dated axis, the length is tagged with the calendar it was measured in,
        and keeps any fraction:

        >>> since_1850 = TimeAxis(TimeUnits(TimeStep.DAYS, "1850-01-01"), "noleap")
        >>> str(TimePoint(91615, since_1850).delta_from(TimePoint(60225, since_1850)))
        '31390 days (noleap)'

        >>> str(TimePoint(91615.5, since_1850).delta_from(TimePoint(91615, since_1850)))
        '0.5 days (noleap)'

        Two dated axes in the same calendar may count from different dates.
        2015-01-01 is 60225 `noleap` days after 1850-01-01
        (this needs `cftime`, to place one epoch on the other's axis):

        >>> since_2015 = TimeAxis(TimeUnits(TimeStep.DAYS, "2015-01-01"), "noleap")
        >>> str(TimePoint(0, since_2015).delta_from(TimePoint(0, since_1850)))
        '60225 days (noleap)'

        Or in different steps, counted in the finer one:

        >>> in_hours = TimeAxis(TimeUnits(TimeStep.HOURS, "1850-01-01"), "noleap")
        >>> str(TimePoint(2, since_1850).delta_from(TimePoint(36, in_hours)))
        '12 hours (noleap)'

        Different calendars are refused,
        because the same number of days is a different length of calendar time in each:

        >>> since_1850_360 = TimeAxis(TimeUnits(TimeStep.DAYS, "1850-01-01"), "360_day")
        >>> try:
        ...     TimePoint(360, since_1850_360).delta_from(TimePoint(0, since_1850))
        ... except MismatchedTimeAxisError:
        ...     print("refused")
        refused
        """
        if self.axis.is_comparable_with(earlier.axis):
            return _delta_on(self.axis, self.value - earlier.value)

        _check_can_be_subtracted(self.axis, earlier.axis)

        if _is_finer(earlier.axis.units.step, self.axis.units.step):
            later = convert_time_point(self, earlier.axis)

            return _delta_on(earlier.axis, later.value - earlier.value)

        moved = convert_time_point(earlier, self.axis)

        return _delta_on(self.axis, self.value - moved.value)

    def plus(self, by: TimeDelta) -> TimePoint:
        """
        Get the time point a length of time after this one, on the same axis

        Parameters
        ----------
        by
            How far to move, backwards if negative

        Returns
        -------
        :
            Where `by` after this time point falls, on this time point's axis

            On a dated axis it is unrounded.
            Moving by years or months on a daily or finer axis
            clamps the day of the month to the length of the month landed in,
            which is the rule every calendar library uses
            and the only one which keeps the answer inside the month asked for:
            31 January plus one month is 28, 29 or 30 February depending on the
            calendar, not 3 March.

        Raises
        ------
        NotAStepOnThisAxisError
            This time point is on a yearly or monthly natural axis
            and `by` is not a whole number of its steps,
            e.g. days against a monthly axis, or six months against a yearly one

        NoEpochError
            This time point is on a natural axis finer than a month,
            and `by` is years or months, which need a date to count from

        UnknownCalendarError
            This time point is on a dated axis which does not know its calendar,
            and moving it needs the calendar

        DeltaCalendarMismatchError
            `by` is years or months measured in one calendar,
            and this time point's axis is in another

        MissingCalendarsExtraError
            The move needs `cftime` and it is not installed

        Notes
        -----
        What happens, by the kind of axis and the kind of step:

        | axis | years or months | days, hours or minutes |
        |---|---|---|
        | natural, yearly or monthly | whole steps, nothing installed needed | refused |
        | natural, finer | refused, there is no date to count from | whole steps |
        | dated | in its calendar, via `cftime` | walked, in any calendar |

        On a dated axis, `by`'s calendar has to be `None` or the axis' own
        when `by` is years or months.
        Days, hours and minutes are walked whatever calendar they were measured in,
        because a day is a day in every calendar.
        Whether walking the same number of days is what you mean
        when two runs are in different calendars is a separate question,
        which is the caller's to answer.

        Examples
        --------
        Twenty years on a monthly axis, with nothing installed:

        >>> monthly = TimeAxis(TimeUnits(TimeStep.MONTHS))
        >>> TimePoint(22189, monthly).plus(TimeDelta(20, TimeStep.YEARS)).value
        22429

        A year is twelve months:

        >>> one_year = TimePoint(0, monthly).plus(TimeDelta(1, TimeStep.YEARS))
        >>> twelve_months = TimePoint(0, monthly).plus(TimeDelta(12, TimeStep.MONTHS))
        >>> one_year == twelve_months
        True

        Backwards is a negative length:

        >>> TimePoint(22429, monthly).plus(TimeDelta(-20, TimeStep.YEARS)).value
        22189
        """
        units = self.axis.units

        if units.is_natural:
            return self._plus_on_a_natural_axis(by)

        if by.step.is_calendar_free:
            if by.calendar is not None:
                if self.axis.calendar is None:
                    raise UnknownCalendarError(self.axis)

                if by.calendar != self.axis.calendar:
                    raise DeltaCalendarMismatchError(by, self.axis)

            if units.step.is_calendar_free:
                # A dated axis counting months or years: counting is exact, as on a
                # natural one, so there is nothing for the calendar to decide.
                return TimePoint(self.value + _whole_steps(by, self.axis), self.axis)

            return TimePoint(_plus_months_with_cftime(self, by), self.axis)

        if self.axis.calendar is None:
            raise UnknownCalendarError(self.axis)

        if units.step.is_calendar_free:
            return TimePoint(_plus_minutes_with_cftime(self, by), self.axis)

        return TimePoint(self.value + _exact_count_in(by, units.step), self.axis)

    def _plus_on_a_natural_axis(self, by: TimeDelta) -> TimePoint:
        """
        Move along a natural axis, which is counting steps

        Parameters
        ----------
        by
            How far to move

        Returns
        -------
        :
            Where the move lands

        Raises
        ------
        NotAStepOnThisAxisError
            The axis is yearly or monthly and `by` is not a whole number of its steps

        NoEpochError
            The axis is finer than a month and `by` is years or months
        """
        step = self.axis.units.step
        if step.is_calendar_free:
            # `by`'s calendar does not matter here: on an axis which counts years or
            # months there are no days for it to decide.
            if not by.step.is_calendar_free:
                raise NotAStepOnThisAxisError(by, self.axis)

            return TimePoint(self.value + _whole_steps(by, self.axis), self.axis)

        if by.step.is_calendar_free:
            raise NoEpochError(self.axis.units)

        return TimePoint(self.value + _exact_count_in(by, step), self.axis)


def _delta_on(axis: TimeAxis, difference: float) -> TimeDelta:
    """
    Get the difference of two values on one axis as a time delta

    Parameters
    ----------
    axis
        The axis both values are on

    difference
        The later value minus the earlier

    Returns
    -------
    :
        That many of the axis' steps, in its calendar,
        and a whole number on a natural axis
    """
    count = int(difference) if axis.units.is_natural else difference

    return TimeDelta(count, axis.units.step, axis.calendar)


def _is_finer(step: TimeStep, than: TimeStep) -> bool:
    """
    Determine whether one step is shorter than another

    Parameters
    ----------
    step
        The step

    than
        The step to compare against

    Returns
    -------
    :
        `True` if `step` is strictly shorter
    """
    return _COARSEST_FIRST.index(step) > _COARSEST_FIRST.index(than)


def _check_can_be_subtracted(later: TimeAxis, earlier: TimeAxis) -> None:
    """
    Refuse two axes whose time points cannot be subtracted, even with a conversion

    Parameters
    ----------
    later
        The axis of the time point being measured to

    earlier
        The axis of the time point being measured from

    Raises
    ------
    MismatchedTimeAxisError
        One is natural and one is dated, or they are in different calendars

    UnknownCalendarError
        Both are dated and one does not know its calendar
    """
    if later.units.is_natural != earlier.units.is_natural:
        raise MismatchedTimeAxisError(later, earlier)

    if later.units.is_natural:
        return

    for axis in (later, earlier):
        if axis.calendar is None:
            raise UnknownCalendarError(axis)

    if later.calendar != earlier.calendar:
        raise MismatchedTimeAxisError(later, earlier)


def convert_time_point(point: TimePoint, to_axis: TimeAxis) -> TimePoint:
    """
    Get the same instant as a time point on another axis

    Parameters
    ----------
    point
        The time point

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

    NoEpochError
        A natural axis involved is finer than a month, so it has no dates on it

    Examples
    --------
    Natural to natural needs nothing installed:

    >>> yearly = TimeAxis(TimeUnits(TimeStep.YEARS))
    >>> monthly = TimeAxis(TimeUnits(TimeStep.MONTHS))
    >>> convert_time_point(TimePoint(1850, yearly), monthly).value
    22189

    >>> convert_time_point(TimePoint(22200, monthly), yearly).value
    1850

    Onto or off a dated axis needs `cftime`.
    A month onto a daily axis is the instant it starts,
    so February 1850 is 31 days after 1850-01-01:

    >>> since_1850 = TimeAxis(TimeUnits(TimeStep.DAYS, "1850-01-01"), "noleap")
    >>> str(convert_time_point(TimePoint(22190, monthly), since_1850))
    "31 days since 1850-01-01, calendar 'noleap'"

    An instant on a daily axis lands in the month it falls in,
    so a time bound at midnight on 1861-01-01 is January 1861,
    which is what a half-open end needs:

    >>> convert_time_point(TimePoint(4015, since_1850), monthly).value
    22321

    >>> year_and_month(22321)
    (1861, 1)

    Between two dated axes in one calendar, counted from different dates:

    >>> since_0001 = TimeAxis(TimeUnits(TimeStep.DAYS, "0001-01-01"), "noleap")
    >>> str(convert_time_point(TimePoint(0.5, since_1850), since_0001))
    "674885.5 days since 0001-01-01, calendar 'noleap'"

    Or in different steps:

    >>> in_hours = TimeAxis(TimeUnits(TimeStep.HOURS, "1850-01-01"), "noleap")
    >>> convert_time_point(TimePoint(36, in_hours), since_1850).value
    1.5
    """
    from_axis = point.axis

    if from_axis.is_comparable_with(to_axis):
        return TimePoint(point.value, to_axis)

    if from_axis.units.is_natural and to_axis.units.is_natural:
        year, month = _year_and_month_of_step(int(point.value), from_axis)

        return TimePoint(_step_of(year, month, to_axis), to_axis)

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
        year, month = _year_and_month_of_step(int(point.value), from_axis)
        at = cftime.datetime(year, month, 1, calendar=to_axis.calendar)

    else:
        at = cftime.num2date(
            point.value, str(from_axis.units), calendar=from_axis.calendar
        )

    if to_axis.units.is_natural:
        return TimePoint(_step_of(at.year, at.month, to_axis), to_axis)

    value = _date_to_number(cftime, at, to_axis)

    return TimePoint(value, to_axis)


@dataclass(frozen=True)
class TimeRange:
    """
    The span of a dataset's data, half-open

    `[start, end_exclusive)`: `start` is in the data and `end_exclusive` is the first
    step which is not. Stored that way, rather than merely documented, because of the
    third of these:

    1. [extent][(c).extent] is `end_exclusive - start`, with no `+ 1` anywhere;
    2. `min_overlap` and padding are *lengths, not time points*, so the convention
       never touches them;
    3. two files are adjacent exactly when `previous.end_exclusive == next.start`,
       which is what [is_adjacent_to][(c).is_adjacent_to] -- and so gap detection --
       is. With inclusive ends that test would need a `+ 1` which has to know the
       unit.

    Both ends are time points on the one axis. On a natural axis they are whole
    numbers of steps; on a dated axis they are as published, e.g. a time bound of
    `0.125` days.

    Examples
    --------
    Yearly data published as 1850 to 1860 holds eleven years, not ten. Count the
    boxes, not the fence posts:

    >>> yearly = TimeAxis(TimeUnits(TimeStep.YEARS))
    >>> str(TimeRange.from_values(1850, 1861, yearly).extent)
    '11 years'

    A dataset with a single step has an extent of one, which is the case that catches
    a conversion which subtracted the published values:

    >>> str(TimeRange.from_values(1850, 1851, yearly).extent)
    '1 year'
    """

    start: TimePoint
    """The first step in the data"""

    end_exclusive: TimePoint
    """
    The first step *not* in the data

    Note that this is **not** what the filename says. A monthly file named
    `..._320001-325012.nc` contains December 325012, so `end_exclusive` is January
    325101 -- one step past it.
    """

    def __post_init__(self) -> None:
        """
        Check both ends are on one axis and the range holds at least one step

        Raises
        ------
        MismatchedTimeAxisError
            The two ends are on different axes

        EmptyTimeRangeError
            `end_exclusive` is not after `start`
        """
        # The same axis, not merely a comparable one: a dated axis which does not
        # know its calendar is comparable with nothing, itself included, and a range
        # is recorded on one before any header has been read.
        if self.start.axis != self.end_exclusive.axis:
            raise MismatchedTimeAxisError(self.start.axis, self.end_exclusive.axis)

        if self.end_exclusive.value <= self.start.value:
            raise EmptyTimeRangeError(self.start.value, self.end_exclusive.value)

    @classmethod
    def from_values(
        cls, start: float, end_exclusive: float, axis: TimeAxis
    ) -> TimeRange:
        """
        Get a range from its two ends, as numbers read off one axis

        Parameters
        ----------
        start
            The first step in the data

        end_exclusive
            The first step *not* in the data

        axis
            The axis both are read off

        Returns
        -------
        :
            The range

        Raises
        ------
        NotAWholeStepError
            The axis is natural and an end is not a whole number

        EmptyTimeRangeError
            `end_exclusive` is not greater than `start`
        """
        return cls(TimePoint(start, axis), TimePoint(end_exclusive, axis))

    @property
    def axis(self) -> TimeAxis:
        """
        The axis both ends are on

        Returns
        -------
        :
            The axis
        """
        return self.start.axis

    @property
    def extent(self) -> TimeDelta:
        """
        How long the data runs for, in the axis' units

        Returns
        -------
        :
            `end_exclusive - start` steps of the axis' own step, with no correction
            term, in the axis' calendar

            A whole number on a natural axis. On a dated axis it is whatever the
            time points give, so hourly data on a `days since` axis can have a
            fractional extent.

        Notes
        -----
        Computed on the shared axis directly rather than with
        [TimePoint.delta_from][(m).TimePoint.delta_from],
        so that a range on an axis which does not know its calendar
        still has an extent.
        """
        return _delta_on(self.axis, self.end_exclusive.value - self.start.value)

    def contains(self, other: TimeRange) -> bool:
        """
        Determine whether this range covers the whole of another

        Parameters
        ----------
        other
            The range which has to be covered

        Returns
        -------
        :
            `True` if every step of `other` is a step of this range

        Raises
        ------
        MismatchedTimeAxisError
            The two ranges are not on comparable axes

        Examples
        --------
        >>> monthly = TimeAxis(TimeUnits(TimeStep.MONTHS))
        >>> everything = TimeRange.from_values(0, 100, monthly)
        >>> everything.contains(TimeRange.from_values(0, 100, monthly))
        True

        >>> everything.contains(TimeRange.from_values(50, 101, monthly))
        False
        """
        return self.start.is_at_or_before(
            other.start
        ) and other.end_exclusive.is_at_or_before(self.end_exclusive)

    def holds(self, point: TimePoint) -> bool:
        """
        Determine whether a time point is one of this range's steps

        Half-open, so `end_exclusive` itself is **not** held: it is the first instant
        after the data. To ask whether the data reaches a time point, `end_exclusive`
        included, use [runs_up_to][(c).runs_up_to].

        Parameters
        ----------
        point
            The time point

        Returns
        -------
        :
            `True` if `start <= point < end_exclusive`

        Raises
        ------
        MismatchedTimeAxisError
            `point` is not on an axis comparable with this range's

        Examples
        --------
        >>> monthly = TimeAxis(TimeUnits(TimeStep.MONTHS))
        >>> TimeRange.from_values(0, 100, monthly).holds(TimePoint(0, monthly))
        True

        Half-open, so `end_exclusive` itself is not held:

        >>> TimeRange.from_values(0, 100, monthly).holds(TimePoint(100, monthly))
        False
        """
        return self.start.is_at_or_before(point) and point.is_before(self.end_exclusive)

    def runs_up_to(self, point: TimePoint) -> bool:
        """
        Determine whether this range's data runs without a break up to a time point

        Unlike [holds][(c).holds], `end_exclusive` itself counts, because a range
        whose last step ends exactly at `point` has data right up to it:

        ```text
          [0, 100) holds steps 0 .. 99, and step 99 ends at 100

          ══════ data ══════╡
                            ▲ 100 = end_exclusive
          runs_up_to(100)  →  0 <= 100 <= 100  →  True
          holds(100)       →  0 <= 100 <  100  →  False
        ```

        This is what a parent has to do at the instant a child branches off it: the
        parent's data stops, and the child's starts, at the same instant. A scenario
        which branches off `historical` on the first instant after its last step is
        a seamless join, and has to read as one.

        Parameters
        ----------
        point
            The time point

        Returns
        -------
        :
            `True` if `start <= point <= end_exclusive` -- with `<=` at **both**
            ends, where [holds][(c).holds] has `<` at the top

        Raises
        ------
        MismatchedTimeAxisError
            `point` is not on an axis comparable with this range's

        Examples
        --------
        >>> monthly = TimeAxis(TimeUnits(TimeStep.MONTHS))
        >>> TimeRange.from_values(0, 100, monthly).runs_up_to(TimePoint(100, monthly))
        True

        One step past the end is a gap:

        >>> TimeRange.from_values(0, 100, monthly).runs_up_to(TimePoint(101, monthly))
        False
        """
        return self.start.is_at_or_before(point) and point.is_at_or_before(
            self.end_exclusive
        )

    def is_adjacent_to(self, other: TimeRange) -> bool:
        """
        Determine whether this range and another meet with no gap and no overlap

        Parameters
        ----------
        other
            The other range

        Returns
        -------
        :
            `True` if one range's `end_exclusive` is the other's `start`

            Symmetric, so the caller does not have to know which range comes first.

        Raises
        ------
        MismatchedTimeAxisError
            The two ranges are not on comparable axes

        Examples
        --------
        Two consecutive monthly files, the first ending December 1859 and the second
        starting January 1860:

        >>> monthly = TimeAxis(TimeUnits(TimeStep.MONTHS))
        >>> first = TimeRange.from_values(22189, 22309, monthly)
        >>> second = TimeRange.from_values(22309, 22429, monthly)
        >>> first.is_adjacent_to(second), second.is_adjacent_to(first)
        (True, True)

        One month missing between them is not adjacency, which is the whole point:

        >>> first.is_adjacent_to(TimeRange.from_values(22310, 22430, monthly))
        False
        """
        if not self.axis.is_comparable_with(other.axis):
            raise MismatchedTimeAxisError(self.axis, other.axis)

        return (
            self.end_exclusive.value == other.start.value
            or other.end_exclusive.value == self.start.value
        )


@dataclass(frozen=True)
class VersionTimeRange:
    """
    What one version's files add up to

    The span, and whether the files actually fill it. Both are recorded because they
    are different facts: a version whose files leave a gap still has a first and a last
    step, and [Covers][esmporium.requirements.Covers] refuses it on the strength of the
    flag rather than on the strength of a missing range.

    Here rather than beside the function which produces it
    ([combine_file_ranges][esmporium.enrich.combine_file_ranges]) because
    `Covers` reads one *at solve time*, and
    `esmporium.requirements` may import `esmporium.datasets` and not
    `esmporium.enrich`.
    """

    range: TimeRange
    """Earliest start to latest `end_exclusive` across the files"""

    is_contiguous: bool
    """
    Whether the files fill the span with no gap and no overlap

    `True` when each file's `end_exclusive` is the next one's `start`, which is what
    adjacency is under the half-open convention. An overlap counts as a failure too:
    two files covering the same month is as much a sign that something is wrong with
    the publication as a month nobody published.
    """

    n_files: int
    """
    How many files were combined

    Kept so that a stored row can be judged without re-reading anything: a version
    which gained a file since it was combined is a row to combine again.
    """


@dataclass(frozen=True)
class BranchPoint:
    """
    Where a run branched from its parent

    A property of the (child to parent) edge rather than of either dataset, which is a
    deliberate departure from ESGF, whose data model has no edges. The facts are the
    child's -- its header publishes the numbers, and for CMIP7 its search record
    publishes the units -- but the place they belong is the link.

    One instant, read off two rulers: `in_parent` off the parent's axis and `in_child`
    off the child's. Each is a [TimePoint][(m).TimePoint], so the axis it was read off
    travels with it. Nothing here converts either one. On a dated axis a time point is
    the published float; on a natural axis it has to be whole, and putting a published
    float there is the caller's conversion,
    [convert_time_point][(m).convert_time_point].
    """

    in_parent: TimePoint
    """
    Where the child branched, on the parent's axis

    Published as `branch_time_in_parent`, a float, on
    [DatasetVersionParent][esmporium.db.schema.DatasetVersionParent]. The axis it
    counts on is published only by some projects: a CMIP6 header gives
    `parent_time_units` beside it, CMIP7 publishes them in the search index, and CMIP5
    publishes a bare `branch_time` with no units at all -- which is why a CMIP5 branch
    point needs a fix before one of these can be built.
    """

    in_child: TimePoint | None = None
    """
    Where the child branched, on the child's axis

    Published as `branch_time_in_child` by a header which carries one, and `None` when
    the file did not say, which is every CMIP5 file.
    """


def total_months(year: int, month: int) -> int:
    """
    Get the ordinal month number of a year and month

    Parameters
    ----------
    year
        Calendar year, as the filename spells it

    month
        Month of that year, 1 to 12

    Returns
    -------
    :
        Which month this is, counting the first month of year 1 as 1

    Notes
    -----
    1-based, so that `0001-01` is month 1. A udunits `months since 0001-01` would make
    it month 0, because `since` counts *elapsed* steps, and that off-by-one is exactly
    what this module exists to keep out of every call site.

    Examples
    --------
    >>> total_months(1, 1)
    1

    >>> total_months(1850, 1)
    22189

    >>> total_months(3200, 1)
    38389
    """
    return (year - 1) * MONTHS_PER_YEAR + month


def year_and_month(total: int) -> tuple[int, int]:
    """
    Get the year and month an ordinal month number refers to

    The inverse of [total_months][(m).total_months].

    Parameters
    ----------
    total
        Ordinal month number, counting the first month of year 1 as 1

    Returns
    -------
    :
        The calendar year and the month of it, 1 to 12

    Examples
    --------
    >>> year_and_month(22189)
    (1850, 1)

    >>> year_and_month(total_months(3250, 12))
    (3250, 12)
    """
    year, month = divmod(total - 1, MONTHS_PER_YEAR)

    return year + 1, month + 1


def spanned(ranges: Iterable[TimeRange]) -> TimeRange:
    """
    Get the one range which spans every range given

    Parameters
    ----------
    ranges
        The ranges to span, all on comparable axes

        At least one, because there is no such thing as the span of nothing.

    Returns
    -------
    :
        Earliest `start` to latest `end_exclusive`, on the first range's axis

        Says nothing about whether the ranges fill it -- that is
        [is_adjacent_to][(m).TimeRange.is_adjacent_to]'s question, and a version whose
        files leave a gap still has a span.

    Raises
    ------
    ValueError
        No ranges were given

    MismatchedTimeAxisError
        The ranges are not all on comparable axes

    Examples
    --------
    >>> monthly = TimeAxis(TimeUnits(TimeStep.MONTHS))
    >>> str(
    ...     spanned(
    ...         [
    ...             TimeRange.from_values(0, 12, monthly),
    ...             TimeRange.from_values(24, 36, monthly),
    ...         ]
    ...     ).extent
    ... )
    '36 months'
    """
    in_order = tuple(ranges)
    if not in_order:
        msg = "At least one time range is needed to span anything, got none"
        raise ValueError(msg)

    first = in_order[0]
    for each_range in in_order[1:]:
        if not first.axis.is_comparable_with(each_range.axis):
            raise MismatchedTimeAxisError(first.axis, each_range.axis)

    return TimeRange(
        start=min((each.start for each in in_order), key=lambda point: point.value),
        end_exclusive=max(
            (each.end_exclusive for each in in_order), key=lambda point: point.value
        ),
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


def _date_to_number(cftime: Any, at: Any, axis: TimeAxis) -> int | float:
    """
    Get a `cftime` date as a plain number on a dated axis

    Parameters
    ----------
    cftime
        The `cftime` module

    at
        The date, a `cftime.datetime`

    axis
        The dated axis, which knows its calendar

    Returns
    -------
    :
        The date's value on `axis`, as a plain `int` when it is whole and a `float`
        otherwise

        Never a numpy number, which is what `date2num` gives back: nothing from
        `cftime` reaches a value this module hands out.
    """
    number = cftime.date2num(at, str(axis.units), calendar=axis.calendar)

    return _tidy(float(number))


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


def _whole_steps(by: TimeDelta, axis: TimeAxis) -> int:
    """
    Get a time delta of years or months as a whole number of an axis' own steps

    Parameters
    ----------
    by
        The time delta, of years or months

    axis
        The axis it is being resolved against, which counts years or months

    Returns
    -------
    :
        How many of the axis' steps long the time delta is

    Raises
    ------
    NotAStepOnThisAxisError
        The axis is yearly and the time delta is not a whole number of years
    """
    months = _months_in(by)

    if axis.units.step == TimeStep.MONTHS:
        return months

    if months % MONTHS_PER_YEAR == 0:
        return months // MONTHS_PER_YEAR

    raise NotAStepOnThisAxisError(by, axis)


def _exact_count_in(by: TimeDelta, step: TimeStep) -> int | float:
    """
    Get a time delta of days, hours or minutes as a count of another exact step

    Parameters
    ----------
    by
        The time delta, of days, hours or minutes

    step
        Days, hours or minutes

    Returns
    -------
    :
        How many `step`s long `by` is, as an `int` when it is whole
    """
    return _tidy(
        by.count * _MINUTES_PER_EXACT_STEP[by.step] / _MINUTES_PER_EXACT_STEP[step]
    )


def _plus_months_with_cftime(point: TimePoint, by: TimeDelta) -> int | float:
    """
    Move by years or months along a dated daily or finer axis, which needs the calendar

    Parameters
    ----------
    point
        Where to start, on a dated axis which knows its calendar

    by
        How far to move, in years or months

    Returns
    -------
    :
        Where the move lands, on `point`'s axis, unrounded

    Raises
    ------
    UnknownCalendarError
        The axis does not know its calendar

    MissingCalendarsExtraError
        `cftime` is not installed

    Notes
    -----
    Day-of-month is clamped to the length of the month landed in.
    See [TimePoint.plus][(m).TimePoint.plus].
    """
    axis = point.axis
    if axis.calendar is None:
        raise UnknownCalendarError(axis)

    cftime = _import_cftime()

    units = str(axis.units)
    at = cftime.num2date(point.value, units, calendar=axis.calendar)

    total = at.year * MONTHS_PER_YEAR + (at.month - 1) + _months_in(by)
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

    return _date_to_number(cftime, moved, axis)


def _plus_minutes_with_cftime(point: TimePoint, by: TimeDelta) -> int | float:
    """
    Move by days, hours or minutes along a dated axis which counts years or months

    Parameters
    ----------
    point
        Where to start, on a dated axis which knows its calendar

    by
        How far to move, in days, hours or minutes

    Returns
    -------
    :
        Where the move lands, on `point`'s axis, unrounded

    Raises
    ------
    UnknownCalendarError
        The axis does not know its calendar

    MissingCalendarsExtraError
        `cftime` is not installed
    """
    axis = point.axis
    if axis.calendar is None:
        raise UnknownCalendarError(axis)

    cftime = _import_cftime()

    units = str(axis.units)
    at = cftime.num2date(point.value, units, calendar=axis.calendar)
    moved = at + datetime.timedelta(minutes=by.count * _MINUTES_PER_EXACT_STEP[by.step])

    return _date_to_number(cftime, moved, axis)
