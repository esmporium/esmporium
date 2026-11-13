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
    """

    units: TimeUnits
    """What the numbers count, and from where"""

    calendar: str | None = None
    """
    The CF calendar name, e.g. `standard`, `noleap`, `360_day`

    `None` *means* something: we did not have the calendar when this was recorded. It
    is not a default standing in for `standard`, because guessing a calendar is how a
    day count silently comes out wrong.
    """

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
            `True` if the two sets of numbers are on the same scale

        Notes
        -----
        The units have to match exactly, because the numbers mean nothing without
        them. The calendar only has to match when *both* axes know it: a range
        measured from a filename does not know its calendar, and refusing to compare
        it with one that does would make the cheap answer useless.

        Examples
        --------
        >>> monthly = TimeAxis(TimeUnits(TimeStep.MONTHS))
        >>> monthly.is_comparable_with(TimeAxis(TimeUnits(TimeStep.MONTHS), "noleap"))
        True

        >>> noleap = TimeAxis(TimeUnits(TimeStep.DAYS, "1850-01-01"), "noleap")
        >>> gregorian = TimeAxis(TimeUnits(TimeStep.DAYS, "1850-01-01"), "standard")
        >>> noleap.is_comparable_with(gregorian)
        False
        """
        if self.units != other.units:
            return False

        if self.calendar is None or other.calendar is None:
            return True

        return self.calendar == other.calendar


class MismatchedTimeAxisError(ValueError):
    """
    Raised when two time ranges on axes which do not agree are compared
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
            f"Convert one of them first."
        )


class EmptyTimeRangeError(ValueError):
    """
    Raised when a time range would hold no data at all
    """

    def __init__(self, start: int, end_exclusive: int) -> None:
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

    Examples
    --------
    Yearly data published as 1850 to 1860 holds eleven years, not ten. Count the
    boxes, not the fence posts:

    >>> yearly = TimeAxis(TimeUnits(TimeStep.YEARS))
    >>> TimeRange(1850, 1861, yearly).extent
    11

    A dataset with a single step has an extent of one, which is the case that catches
    a conversion which subtracted the published values:

    >>> TimeRange(1850, 1851, yearly).extent
    1
    """

    start: int
    """The first step in the data, counted in the axis' units"""

    end_exclusive: int
    """
    The first step *not* in the data, counted in the axis' units

    Note that this is **not** what the filename says. A monthly file named
    `..._320001-325012.nc` contains December 325012, so `end_exclusive` is January
    325101 -- one step past it.
    """

    axis: TimeAxis
    """The axis these numbers are positions on"""

    def __post_init__(self) -> None:
        """
        Check the range holds at least one step

        Raises
        ------
        EmptyTimeRangeError
            `end_exclusive` is not greater than `start`
        """
        if self.end_exclusive <= self.start:
            raise EmptyTimeRangeError(self.start, self.end_exclusive)

    @property
    def extent(self) -> int:
        """
        How long the data runs for, in the axis' units

        Returns
        -------
        :
            `end_exclusive - start`, with no correction term
        """
        return self.end_exclusive - self.start

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
        self._check_comparable(other)

        return self.start <= other.start and self.end_exclusive >= other.end_exclusive

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
        self._check_comparable(other)

        return self.end_exclusive == other.start or other.end_exclusive == self.start

    def _check_comparable(self, other: TimeRange) -> None:
        """
        Refuse a comparison between ranges on axes which do not agree

        Parameters
        ----------
        other
            The range being compared against

        Raises
        ------
        MismatchedTimeAxisError
            The two axes are not comparable
        """
        if not self.axis.is_comparable_with(other.axis):
            raise MismatchedTimeAxisError(self.axis, other.axis)


@dataclass(frozen=True)
class VersionTimeRange:
    """
    What one version's files add up to

    The span, and whether the files actually fill it. Both are recorded because they
    are different facts: a version whose files leave a gap still has a first and a last
    step, and [Covers][esmporium.requirements.Covers] refuses it on the strength of the
    flag rather than on the strength of a missing range.

    Here rather than beside the measurement which produces it
    ([measure_version][esmporium.enrich.measure_version]) because
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
    How many files were measured

    Kept so that a stored row can be judged without re-reading anything: a version
    which gained a file since it was measured is a row to measure again.
    """


class NegativeSpanError(ValueError):
    """
    Raised when a length of time is given as a negative number
    """

    def __init__(self, count: int, step: TimeStep) -> None:
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


@dataclass(frozen=True)
class Span:
    """
    A length of time, as a requirement states one

    A *length*, not a position: "twenty years of overlap", "ten years of padding".
    Lengths are what the half-open convention does not touch, so there is no
    exclusive-end question here at all.

    Examples
    --------
    >>> str(Span(20, TimeStep.YEARS))
    '20 years'

    >>> str(Span(1, TimeStep.YEARS))
    '1 year'
    """

    count: int
    """How many steps long"""

    step: TimeStep
    """The size of one step"""

    def __post_init__(self) -> None:
        """
        Check the length is not negative

        Raises
        ------
        NegativeSpanError
            `count` is less than zero
        """
        if self.count < 0:
            raise NegativeSpanError(self.count, self.step)

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
    child's -- its header publishes the number, and for CMIP7 its search record
    publishes the units -- but the place they belong is the link.

    The one piece of arithmetic coverage needs is that **after mapping, the child's
    start is the branch point**: a position `p` on the child's axis is
    `in_parent + (p - child_start)` on the parent's.
    """

    in_parent: int
    """
    Where the child's data starts, as a position on the *parent's* axis

    Interpreted, not raw, in two senses. The published number is on
    [DatasetVersionParent.branch_time_in_parent][esmporium.db.schema.DatasetVersionParent.branch_time_in_parent],
    a float, so building one of these floors it to a whole step rather than rounding.
    And the axis it counts on is published only by some projects: a CMIP6 header gives
    `parent_time_units` beside it, CMIP7 publishes them in the search index, and CMIP5
    publishes a bare `branch_time` with no units at all -- which is why a CMIP5 branch
    point needs a fix before one of these can be built.
    """

    parent_axis: TimeAxis
    """The axis `in_parent` is a position on"""

    in_child: int | None = None
    """
    Where the child's data starts, as a position on the child's own axis

    `None` when the file did not say. Published as `branch_time_in_child` by a header
    which carries one, and kept because it is a check rather than an input: it should
    equal the child's measured start, and a disagreement is a fact about the data
    worth noticing rather than a number to compute with.
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
    >>> spanned([TimeRange(0, 12, monthly), TimeRange(24, 36, monthly)]).extent
    36
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
