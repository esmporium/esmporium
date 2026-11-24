"""
When a dataset's data is, and where a run branched from its parent

The information ESGF's search APIs may not publish, as values. Everything here holds
numbers, a unit and a calendar name and does no calendar arithmetic -- that is
[esmporium.datasets.calendars][], which is separate because it needs `cftime` and
these types must not.

Two conventions are carried by the types rather than by docstrings, because both are
off-by-one traps:

- a range is **half-open**, `[start, end_exclusive)`, so
  [extent][(m).TimeRange.extent] is one subtraction with no correction term, and two
  files are adjacent exactly when `previous.end_exclusive == next.start`;
- a **natural** value is a 1-based ordinal step count with no epoch, so the first
  month of year 1 is month 1. A udunits `since` epoch counts *elapsed* steps and would
  make it month 0.

Positions are held as given. On a natural axis a position is a whole number of steps,
and anything else is refused; on a dated axis it is whatever was published, fraction
and all, because a branch time or a time bound is a float and rounding it would be a
conversion nobody asked for. Moving between axes is
[convert_position][esmporium.datasets.calendars.convert_position].
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

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
        if text in (step.value, step.value.rstrip("s")):
            return step

    return None


@dataclass(frozen=True)
class TimeAxis:
    """
    The axis a dataset's time values live on

    A pair of positions is only comparable when they are on axes which agree -- see
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
        Determine whether positions on this axis can be compared with another's

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

        Conversions are
        [convert_position][esmporium.datasets.calendars.convert_position].

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
    Raised when a position on a natural axis is not a whole number of steps
    """

    def __init__(self, value: float, axis: TimeAxis) -> None:
        """
        Initialise the error

        Parameters
        ----------
        value
            The position which was given

        axis
            The natural axis it was given on
        """
        self.value = value
        self.axis = axis
        super().__init__(
            f"{value!r} is not a position on a {axis.units} axis, which counts whole "
            f"{axis.units.step.value}. A fractional value is a position on a dated "
            f"axis; putting it on a natural one is a conversion, done by "
            f"`esmporium.datasets.calendars.convert_position`."
        )


def check_position(value: float, axis: TimeAxis) -> None:
    """
    Refuse a position which a natural axis cannot hold

    Parameters
    ----------
    value
        The position

    axis
        The axis it is a position on

    Raises
    ------
    NotAWholeStepError
        `axis` is natural and `value` is not a whole number

    Examples
    --------
    >>> check_position(22189, TimeAxis(TimeUnits(TimeStep.MONTHS)))

    >>> check_position(438000.5, TimeAxis(TimeUnits(TimeStep.DAYS, "1850-01-01")))

    >>> try:
    ...     check_position(1850.5, TimeAxis(TimeUnits(TimeStep.YEARS)))
    ... except NotAWholeStepError as error:
    ...     error.value
    1850.5
    """
    if axis.units.is_natural and not float(value).is_integer():
        raise NotAWholeStepError(value, axis)


@dataclass(frozen=True)
class Position:
    """
    A point in time, as a number read off a particular axis

    A number on its own does not say which axis it was read off, and two numbers off
    different axes cannot be compared or subtracted: day 91615 since 1850 and day
    182500 since 0001 are just readings off two rulers with different zeros. So the
    number and its axis are held together, and anything which combines two positions
    can check they are on the same ruler first.

    On a natural axis the value has to be a whole number of steps; on a dated axis it
    is as published, fraction and all.

    Examples
    --------
    >>> str(Position(22189, TimeAxis(TimeUnits(TimeStep.MONTHS))))
    '22189 in months'

    >>> str(Position(91615, TimeAxis(TimeUnits(TimeStep.DAYS, "1850-01-01"), "noleap")))
    "91615 in days since 1850-01-01, calendar 'noleap'"
    """

    value: float
    """The number, read off `axis`"""

    axis: TimeAxis
    """The axis `value` is read off"""

    def __post_init__(self) -> None:
        """
        Check the value is a position its axis can hold

        Raises
        ------
        NotAWholeStepError
            The axis is natural and the value is not a whole number
        """
        check_position(self.value, self.axis)

    def __str__(self) -> str:
        """
        Get the position as text for a message

        Returns
        -------
        :
            The value and its units, and its calendar when the axis names one
        """
        if self.axis.calendar is None:
            return f"{self.value} in {self.axis.units}"

        return f"{self.value} in {self.axis.units}, calendar {self.axis.calendar!r}"


class MismatchedTimeAxisError(ValueError):
    """
    Raised when positions or ranges on axes which do not agree are compared
    """

    def __init__(self, left: TimeAxis, right: TimeAxis) -> None:
        """
        Initialise the error

        Parameters
        ----------
        left
            Axis of the range the comparison was asked of

        right
            Axis of the range it was asked about
        """
        self.left = left
        self.right = right
        super().__init__(
            f"These time ranges are not on comparable axes: {left.units} in calendar "
            f"{left.calendar!r} against {right.units} in calendar {right.calendar!r}. "
            f"Convert one onto the other first, with "
            f"`esmporium.datasets.calendars.convert_position`."
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


@dataclass(frozen=True)
class TimeRange:
    """
    The span of a dataset's data, half-open

    `[start, end_exclusive)`: `start` is in the data and `end_exclusive` is the first
    step which is not. Stored that way, rather than merely documented, because of the
    third of these:

    1. [extent][(c).extent] is `end_exclusive - start`, with no `+ 1` anywhere;
    2. `min_overlap` and padding are *lengths, not positions*, so the convention never
       touches them;
    3. two files are adjacent exactly when `previous.end_exclusive == next.start`,
       which is what [is_adjacent_to][(c).is_adjacent_to] -- and so gap detection --
       is. With inclusive ends that test would need a `+ 1` which has to know the
       unit.

    Positions on a natural axis are whole numbers of steps; on a dated axis they are
    as published, e.g. a time bound of `0.125` days.

    Examples
    --------
    Yearly data published as 1850 to 1860 holds eleven years, not ten. Count the
    boxes, not the fence posts:

    >>> yearly = TimeAxis(TimeUnits(TimeStep.YEARS))
    >>> str(TimeRange(1850, 1861, yearly).extent)
    '11 years'

    A dataset with a single step has an extent of one, which is the case that catches
    a conversion which subtracted the published values:

    >>> str(TimeRange(1850, 1851, yearly).extent)
    '1 year'
    """

    start: float
    """The first step in the data, as a position on the axis"""

    end_exclusive: float
    """
    The first step *not* in the data, as a position on the axis

    Note that this is **not** what the filename says. A monthly file named
    `..._320001-325012.nc` contains December 325012, so `end_exclusive` is January
    325101 -- one step past it.
    """

    axis: TimeAxis
    """The axis these numbers are positions on"""

    def __post_init__(self) -> None:
        """
        Check the range holds at least one step, at positions its axis can hold

        Raises
        ------
        NotAWholeStepError
            The axis is natural and a position is not a whole number

        EmptyTimeRangeError
            `end_exclusive` is not greater than `start`
        """
        check_position(self.start, self.axis)
        check_position(self.end_exclusive, self.axis)

        if self.end_exclusive <= self.start:
            raise EmptyTimeRangeError(self.start, self.end_exclusive)

    @property
    def extent(self) -> Span:
        """
        How long the data runs for, in the axis' units

        Returns
        -------
        :
            `end_exclusive - start` steps of the axis' own step, with no correction
            term

            A whole number on a natural axis. On a dated axis it is whatever the
            positions give, so hourly data on a `days since` axis can have a
            fractional extent.
        """
        length = self.end_exclusive - self.start
        if self.axis.units.is_natural:
            return Span(int(length), self.axis.units.step)

        return Span(length, self.axis.units.step)

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
        >>> TimeRange(0, 100, monthly).contains(TimeRange(0, 100, monthly))
        True

        >>> TimeRange(0, 100, monthly).contains(TimeRange(50, 101, monthly))
        False
        """
        if not self.axis.is_comparable_with(other.axis):
            raise MismatchedTimeAxisError(self.axis, other.axis)

        return self.start <= other.start and self.end_exclusive >= other.end_exclusive

    def holds(self, position: Position) -> bool:
        """
        Determine whether a position is one of this range's steps

        Half-open, so `end_exclusive` itself is **not** held: it is the first instant
        after the data. To ask whether the data reaches a position, `end_exclusive`
        included, use [runs_up_to][(c).runs_up_to].

        Parameters
        ----------
        position
            The position

        Returns
        -------
        :
            `True` if `start <= position < end_exclusive`

        Raises
        ------
        MismatchedTimeAxisError
            `position` is not on an axis comparable with this range's

        Examples
        --------
        >>> monthly = TimeAxis(TimeUnits(TimeStep.MONTHS))
        >>> TimeRange(0, 100, monthly).holds(Position(0, monthly))
        True

        Half-open, so `end_exclusive` itself is not held:

        >>> TimeRange(0, 100, monthly).holds(Position(100, monthly))
        False
        """
        if not self.axis.is_comparable_with(position.axis):
            raise MismatchedTimeAxisError(self.axis, position.axis)

        return self.start <= position.value < self.end_exclusive

    def runs_up_to(self, position: Position) -> bool:
        """
        Determine whether this range's data runs without a break up to a position

        Unlike [holds][(c).holds], `end_exclusive` itself counts, because a range
        whose last step ends exactly at `position` has data right up to it:

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
        position
            The position

        Returns
        -------
        :
            `True` if `start <= position <= end_exclusive` -- with `<=` at **both**
            ends, where [holds][(c).holds] has `<` at the top

        Raises
        ------
        MismatchedTimeAxisError
            `position` is not on an axis comparable with this range's

        Examples
        --------
        >>> monthly = TimeAxis(TimeUnits(TimeStep.MONTHS))
        >>> TimeRange(0, 100, monthly).runs_up_to(Position(100, monthly))
        True

        One step past the end is a gap:

        >>> TimeRange(0, 100, monthly).runs_up_to(Position(101, monthly))
        False
        """
        if not self.axis.is_comparable_with(position.axis):
            raise MismatchedTimeAxisError(self.axis, position.axis)

        return self.start <= position.value <= self.end_exclusive

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
        >>> first = TimeRange(22189, 22309, monthly)
        >>> second = TimeRange(22309, 22429, monthly)
        >>> first.is_adjacent_to(second), second.is_adjacent_to(first)
        (True, True)

        One month missing between them is not adjacency, which is the whole point:

        >>> first.is_adjacent_to(TimeRange(22310, 22430, monthly))
        False
        """
        if not self.axis.is_comparable_with(other.axis):
            raise MismatchedTimeAxisError(self.axis, other.axis)

        return self.end_exclusive == other.start or other.end_exclusive == self.start


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


class NegativeSpanError(ValueError):
    """
    Raised when a length of time is given as a negative number
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
            f"A span is a length, so it cannot be negative, but got {count} "
            f"{step.value}. To ask for data before a point rather than after it, "
            f"say so at the point of use rather than with a negative length."
        )


class FractionalCalendarFreeSpanError(ValueError):
    """
    Raised when a span of years or months is not a whole number of them
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
            f"A span of {step.value} is a whole number of them, but got {count}. "
            f"State a part of a year in months, e.g. 6 months rather than 0.5 years."
        )


@dataclass(frozen=True)
class Span:
    """
    A length of time, as a requirement states one

    A *length*, not a position: "twenty years of overlap", "ten years of padding".
    Lengths are what the half-open convention does not touch, so there is no
    exclusive-end question here at all.

    A count of years or months is whole: a fractional year is not a length this
    states. A count of anything finer may be fractional, because that is what the
    extent of sub-daily data on a `days since` axis comes to.

    Examples
    --------
    >>> str(Span(20, TimeStep.YEARS))
    '20 years'

    >>> str(Span(1, TimeStep.YEARS))
    '1 year'
    """

    count: int | float
    """How many steps long"""

    step: TimeStep
    """The size of one step"""

    def __post_init__(self) -> None:
        """
        Check the length is not negative, and is whole where it has to be

        Raises
        ------
        NegativeSpanError
            `count` is less than zero

        FractionalCalendarFreeSpanError
            `step` is years or months and `count` is not a whole number
        """
        if self.count < 0:
            raise NegativeSpanError(self.count, self.step)

        if self.step.is_calendar_free and not float(self.count).is_integer():
            raise FractionalCalendarFreeSpanError(self.count, self.step)

    def __str__(self) -> str:
        """
        Render for a message a user reads

        Returns
        -------
        :
            The count and the step, singular when the count is one
        """
        if self.count == 1:
            return f"{self.count} {self.step.value.rstrip('s')}"

        return f"{self.count} {self.step.value}"


@dataclass(frozen=True)
class BranchPoint:
    """
    Where a run branched from its parent

    A property of the (child to parent) edge rather than of either dataset, which is a
    deliberate departure from ESGF, whose data model has no edges. The facts are the
    child's -- its header publishes the numbers, and for CMIP7 its search record
    publishes the units -- but the place they belong is the link.

    One instant, read off two rulers: `in_parent` off the parent's axis and `in_child`
    off the child's. Each is a [Position][(m).Position], so the axis it was read off
    travels with it. Nothing here converts either one. On a dated axis a position is
    the published float; on a natural axis it has to be whole, and putting a published
    float there is the caller's conversion,
    [convert_position][esmporium.datasets.calendars.convert_position].
    """

    in_parent: Position
    """
    Where the child branched, on the parent's axis

    Published as `branch_time_in_parent`, a float, on
    [DatasetVersionParent][esmporium.db.schema.DatasetVersionParent]. The axis it
    counts on is published only by some projects: a CMIP6 header gives
    `parent_time_units` beside it, CMIP7 publishes them in the search index, and CMIP5
    publishes a bare `branch_time` with no units at all -- which is why a CMIP5 branch
    point needs a fix before one of these can be built.
    """

    in_child: Position | None = None
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
    >>> str(spanned([TimeRange(0, 12, monthly), TimeRange(24, 36, monthly)]).extent)
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
        start=min(each_range.start for each_range in in_order),
        end_exclusive=max(each_range.end_exclusive for each_range in in_order),
        axis=first.axis,
    )
