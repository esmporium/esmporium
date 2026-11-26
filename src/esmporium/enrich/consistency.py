"""
Comparing what two sources say, so that a disagreement is found rather than overwritten

When a more trusted source is recorded beside a less trusted one, both are kept and the
more trusted one answers. What is checked here is whether they agree, and if not, by how
much, so that the disagreement can be recorded and shown: a filename whose stamps do not
match its bounds, a search record that disagrees with the filenames, or a
`branch_time_in_child` that is not where the child's data starts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from esmporium.datasets.information import time_range_trust_rank
from esmporium.datasets.time import (
    MismatchedTimeAxisError,
    NoEpochError,
    UnknownCalendarError,
    convert_time_point,
)

if TYPE_CHECKING:
    from esmporium.datasets.information import SourcedTimeRange
    from esmporium.datasets.time import TimeAxis, TimeDelta, TimePoint, TimeRange


class NotLessTrustedError(ValueError):
    """
    Raised when two ranges are compared with the more trusted one in the wrong place
    """

    def __init__(
        self, more_trusted: SourcedTimeRange, less_trusted: SourcedTimeRange
    ) -> None:
        """
        Initialise the error

        Parameters
        ----------
        more_trusted
            The range passed as the more trusted one

        less_trusted
            The range passed as the less trusted one
        """
        self.more_trusted = more_trusted
        self.less_trusted = less_trusted
        super().__init__(
            f"A range from {less_trusted.source.value!r} was passed as less trusted "
            f"than one from {more_trusted.source.value!r}, and it is not. A "
            "disagreement is recorded against the less trusted of the two, so which "
            "is which has to be right. See `TIME_RANGE_TRUST_ORDER`."
        )


@dataclass(frozen=True)
class TimeRangeDisagreement:
    """
    Two sources which give different ranges for the same data
    """

    more_trusted: SourcedTimeRange
    """The range which answers"""

    less_trusted: SourcedTimeRange
    """The range which disagrees with it"""

    start_difference: TimeDelta
    """
    How far after the more trusted start the less trusted one is

    Negative when it is before. Zero when only the ends disagree.
    """

    end_difference: TimeDelta
    """
    How far after the more trusted end the less trusted one is

    Negative when it is before. Zero when only the starts disagree.
    """


@dataclass(frozen=True)
class NotComparable:
    """
    Two ranges with no common axis to compare them on

    An answer rather than an error, because a pair of sources which cannot be checked
    against each other is worth knowing about, and is not a fault in either.
    """

    more_trusted: SourcedTimeRange
    """The more trusted range"""

    less_trusted: SourcedTimeRange
    """The less trusted range"""

    reason: str
    """Why there is no common axis"""


def compare_time_ranges(
    more_trusted: SourcedTimeRange, less_trusted: SourcedTimeRange
) -> TimeRangeDisagreement | NotComparable | None:
    """
    Compare what two sources say about the range of the same data

    Parameters
    ----------
    more_trusted
        The range which answers

    less_trusted
        The range checked against it, from a less trusted source

    Returns
    -------
    :
        `None` when the two agree, how they differ when they do not, and
        [NotComparable][(m).NotComparable] when there is no axis to compare them on

        They are compared as they stand when their axes are comparable. Otherwise the
        less trusted range is moved onto the more trusted one's axis where that needs
        no assumption: between two natural axes, or between two dated axes in one
        calendar. A natural range and a dated one are not compared, because placing a
        dated bound on a month is a choice about part-steps that neither source made.

    Raises
    ------
    NotLessTrustedError
        `less_trusted` comes from a source at least as trusted as `more_trusted`'s

    MissingCalendarsExtraError
        Two dated axes in one calendar have different units, so one has to be
        converted onto the other, and `cftime` is not installed

    Examples
    --------
    >>> from esmporium.datasets import (
    ...     InformationSource,
    ...     SourcedTimeRange,
    ...     TimeAxis,
    ...     TimeRange,
    ...     TimeStep,
    ...     TimeUnits,
    ... )
    >>> monthly = TimeAxis(TimeUnits(TimeStep.MONTHS))
    >>> from_bounds = SourcedTimeRange(
    ...     TimeRange.from_values(22189, 24169, monthly),
    ...     InformationSource.VARIABLE_VALUES,
    ... )
    >>> from_filename = SourcedTimeRange(
    ...     TimeRange.from_values(22189, 24170, monthly), InformationSource.FILENAME
    ... )
    >>> disagreement = compare_time_ranges(from_bounds, from_filename)
    >>> str(disagreement.start_difference), str(disagreement.end_difference)
    ('0 months', '1 month')

    A yearly range is compared with a monthly one in months:

    >>> yearly = TimeAxis(TimeUnits(TimeStep.YEARS))
    >>> from_search_record = SourcedTimeRange(
    ...     TimeRange.from_values(1850, 2015, yearly), InformationSource.SEARCH_RECORD
    ... )
    >>> print(compare_time_ranges(from_bounds, from_search_record))
    None
    """
    if time_range_trust_rank(less_trusted.source) <= time_range_trust_rank(
        more_trusted.source
    ):
        raise NotLessTrustedError(more_trusted, less_trusted)

    on = more_trusted.range.axis
    moved = _moved_onto(less_trusted.range, on)
    if isinstance(moved, str):
        return NotComparable(
            more_trusted=more_trusted, less_trusted=less_trusted, reason=moved
        )

    start_difference = moved.start.delta_from(more_trusted.range.start)
    end_difference = moved.end_exclusive.delta_from(more_trusted.range.end_exclusive)
    if start_difference.count == 0 and end_difference.count == 0:
        return None

    return TimeRangeDisagreement(
        more_trusted=more_trusted,
        less_trusted=less_trusted,
        start_difference=start_difference,
        end_difference=end_difference,
    )


@dataclass(frozen=True)
class BranchTimeInChildDisagreement:
    """
    A published `branch_time_in_child` which is not where the child's data starts
    """

    in_child: TimePoint
    """The branch, on the child's axis, as published"""

    first_bound: TimePoint
    """Where the child's data starts: the first lower time bound of its first file"""

    difference: TimeDelta
    """
    How far after the branch the data starts

    Positive when the child's data starts after it branched, which is what a missing
    leading file looks like.
    """


def compare_branch_time_in_child(
    in_child: TimePoint, first_bound: TimePoint
) -> BranchTimeInChildDisagreement | None:
    """
    Compare a child's published branch time with where its data starts

    Parameters
    ----------
    in_child
        The published `branch_time_in_child`, on the child's axis

    first_bound
        The first lower time bound of the child's first file

    Returns
    -------
    :
        `None` when the data starts at the branch, and how far apart they are when it
        does not

    Raises
    ------
    MismatchedTimeAxisError
        The two are in different calendars, or one is on a natural axis and the other
        on a dated one

    MissingCalendarsExtraError
        The two are on dated axes with different units and `cftime` is not installed

    Examples
    --------
    >>> from esmporium.datasets import TimeAxis, TimePoint, TimeStep, TimeUnits
    >>> axis = TimeAxis(TimeUnits(TimeStep.DAYS, "1850-01-01"), "noleap")
    >>> print(compare_branch_time_in_child(TimePoint(0.0, axis), TimePoint(0.0, axis)))
    None

    A child whose first year of files is missing:

    >>> late = compare_branch_time_in_child(
    ...     TimePoint(0.0, axis), TimePoint(365.0, axis)
    ... )
    >>> str(late.difference)
    '365.0 days (noleap)'
    """
    difference = first_bound.delta_from(in_child)
    if difference.count == 0:
        return None

    return BranchTimeInChildDisagreement(
        in_child=in_child, first_bound=first_bound, difference=difference
    )


def _moved_onto(time_range: TimeRange, axis: TimeAxis) -> TimeRange | str:
    """
    Get a range on another axis, where that needs no assumption

    Parameters
    ----------
    time_range
        The range

    axis
        The axis wanted

    Returns
    -------
    :
        The range on `axis`, or why it cannot be put there without an assumption
    """
    if time_range.axis.is_comparable_with(axis):
        return time_range

    if time_range.axis.units.is_natural != axis.units.is_natural:
        return (
            f"one range is on {time_range.axis.units} and the other on {axis.units}: "
            "a natural axis and a dated one, and placing a dated bound on a whole "
            "step is a choice about part-steps"
        )

    try:
        return type(time_range)(
            start=convert_time_point(time_range.start, axis),
            end_exclusive=convert_time_point(time_range.end_exclusive, axis),
        )
    except (MismatchedTimeAxisError, UnknownCalendarError, NoEpochError) as error:
        return str(error)
