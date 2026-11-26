"""
Parsing a file's first and last time bounds into the range its data spans

The bounds are the data itself, so the dated range read off them rests on no
assumption: the first lower bound is where the data starts and the last upper bound is
where it ends. The natural range, in whole months or whole years, is worked out from
that, and rests on an assumption only when a bound falls part way through one of its
steps. The dated range is never widened or rounded.

Climatology bounds are read as a range only when they show the data is a run of data:
when every step starts where the previous one ended. Bounds which do not meet end to
end, as a multi-year climatology's or an average day's do, have no span to give. A
dataset its project labels a climatology is given no range whatever its bounds say.
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from esmporium.datasets.information import (
    Assumption,
    InformationSource,
    NoRangeReason,
    SourcedTimeRange,
)
from esmporium.datasets.time import (
    TimeAxis,
    TimePoint,
    TimeRange,
    TimeStep,
    TimeUnits,
    convert_time_point,
)
from esmporium.enrich.known_parsers import (
    CLIMATOLOGY_INTERVALS_BY_PROJECT,
    DELTA_BY_REPORTING_INTERVAL,
    delta_of_reporting_interval,
    is_climatology_interval,
)
from esmporium.headers.contents import BoundsAttribute

if TYPE_CHECKING:
    from esmporium.datasets.time import TimeDelta
    from esmporium.enrich.variable_attributes import ParsedTimeVariable
    from esmporium.headers import FirstAndLastTimeBounds

ON_A_STEP_EDGE_WITHIN_SECONDS = 1.0
"""
How close two time bounds have to be to count as the same instant

Used to decide whether a bound is on the edge of a month (or a year), and whether one
step of climatology bounds ends where the next begins.

Bounds are stored as floating-point numbers, so a bound written as the start of a month
can come back a hair either side of it, e.g. `1415999.9999998` hours rather than
`1416000`. A second is far finer than any step a range is counted in, and far coarser
than that error.
"""

_SECONDS_PER_DATED_STEP: Mapping[TimeStep, int] = {
    TimeStep.DAYS: 86_400,
    TimeStep.HOURS: 3_600,
    TimeStep.MINUTES: 60,
}
"""
How many seconds one step of a dated axis is, for the steps whose length is fixed

A dated axis counting months or years has steps of varying length, so a bound on one is
on a step edge only when it is exactly on it.
"""


@dataclass(frozen=True)
class ParsedTimeValues:
    """
    The range a file's data spans, from its first and last time bounds
    """

    dated: SourcedTimeRange | None
    """
    `[first lower bound, last upper bound)`, on the time variable's own axis

    Rests on no assumption. `None` when there is no range to give, and
    `no_range_because` says why.
    """

    natural: SourcedTimeRange | None
    """
    The same data on a natural axis: whole years for yearly data, whole months for
    everything else

    Each end is widened to the edge of the natural step it falls in, so a month (or
    year) the data covers only part of is counted as covered. Only this natural range
    is widened; `dated` is the bounds as published. When widening happened at either
    end the range rests on
    [Assumption.PART_STEPS_COUNT_AS_COVERED][esmporium.datasets.Assumption.PART_STEPS_COUNT_AS_COVERED];
    when both bounds are on step edges it rests on none. `None` exactly when `dated`
    is.
    """

    no_range_because: NoRangeReason | None
    """
    Why there is no range, or `None` when there is one

    [CLIMATOLOGY][esmporium.datasets.NoRangeReason.CLIMATOLOGY] for a dataset its
    project labels a multi-year climatology;
    [NO_BOUNDS][esmporium.datasets.NoRangeReason.NO_BOUNDS] for a time variable with
    no bounds;
    [CLIMATOLOGY_BOUNDS_NOT_CONTIGUOUS][esmporium.datasets.NoRangeReason.CLIMATOLOGY_BOUNDS_NOT_CONTIGUOUS]
    for climatology bounds whose steps do not meet end to end; and
    [CLIMATOLOGY_BOUNDS_NOT_CHECKED][esmporium.datasets.NoRangeReason.CLIMATOLOGY_BOUNDS_NOT_CHECKED]
    for climatology bounds which were not all read.
    """


def natural_step_for(
    reporting_interval: str,
    deltas: Mapping[str, TimeDelta] = DELTA_BY_REPORTING_INTERVAL,
) -> TimeStep:
    """
    Get the step a dataset's natural range is counted in

    Parameters
    ----------
    reporting_interval
        The dataset's reporting interval, e.g. `mon`

    deltas
        How wide one step of each reporting interval is

    Returns
    -------
    :
        Years for data reported yearly or less often, and months for everything else

        Daily and finer data is counted in months too, because months are the finest
        step with the same count in every calendar.

    Raises
    ------
    UnknownReportingIntervalError
        We do not know how wide one step of `reporting_interval` is

    Examples
    --------
    >>> natural_step_for("yr").value, natural_step_for("3hr").value
    ('years', 'months')
    """
    step = delta_of_reporting_interval(reporting_interval, deltas).step
    if step.is_calendar_free:
        return step

    return TimeStep.MONTHS


def parse_variable_values(  # noqa: PLR0913 - one argument per injection seam
    values: FirstAndLastTimeBounds,
    variable: ParsedTimeVariable,
    *,
    project: str,
    reporting_interval: str,
    deltas: Mapping[str, TimeDelta] = DELTA_BY_REPORTING_INTERVAL,
    climatology_intervals: Mapping[str, frozenset[str]] = (
        CLIMATOLOGY_INTERVALS_BY_PROJECT
    ),
) -> ParsedTimeValues:
    """
    Parse a file's first and last time bounds into the range its data spans

    In this order:

    1. a dataset its project labels a multi-year climatology, by its reporting
       interval, gives no range, whatever its bounds say;
    2. a time variable with no bounds gives no range;
    3. climatology bounds give a range only when every step starts where the previous
       one ended, which needs every pair
       ([FirstAndLastTimeBounds.climatology_bounds][esmporium.headers.FirstAndLastTimeBounds.climatology_bounds]);
    4. ordinary bounds give a range from the first lower to the last upper bound.

    Parameters
    ----------
    values
        The first and last values and bounds of the file's time variable, as
        [esmporium.headers][] read them

    variable
        What the same time variable's attributes say, which gives the axis the bounds
        are on

    project
        The project the dataset belongs to, which says which reporting intervals are
        climatologies

    reporting_interval
        The dataset's reporting interval, which picks the step of the natural range
        (see [natural_step_for][(m).natural_step_for]) and may label it a climatology

    deltas
        How wide one step of each reporting interval is

    climatology_intervals
        Which reporting intervals mean a multi-year climatology, per project

    Returns
    -------
    :
        The dated range, the natural range, or why there is neither

    Raises
    ------
    UnknownProjectClimatologyIntervalsError
        We do not know which of `project`'s reporting intervals are climatologies

    UnknownReportingIntervalError
        We do not know how wide one step of `reporting_interval` is

    EmptyTimeRangeError
        The last upper bound is not after the first lower bound

    MissingCalendarsExtraError
        `cftime` is not installed, which placing a bound on a month needs

    Examples
    --------
    A monthly `noleap` file running from 1850 to the end of 2014, whose bounds sit on
    month edges:

    >>> from esmporium.enrich.variable_attributes import parse_variable_attributes
    >>> from esmporium.headers import BoundsAttribute, FirstAndLastTimeBounds
    >>> variable = parse_variable_attributes(
    ...     {
    ...         "units": "days since 1850-01-01",
    ...         "calendar": "noleap",
    ...         "bounds": "time_bnds",
    ...     }
    ... )
    >>> values = FirstAndLastTimeBounds(
    ...     time_variable="time",
    ...     n_steps=1980,
    ...     first_value=15.5,
    ...     last_value=60209.5,
    ...     bounds_attribute=BoundsAttribute.BOUNDS,
    ...     bounds_variable="time_bnds",
    ...     first_bounds=(0.0, 31.0),
    ...     last_bounds=(60194.0, 60225.0),
    ... )
    >>> parsed = parse_variable_values(
    ...     values, variable, project="CMIP6", reporting_interval="mon"
    ... )
    >>> str(parsed.natural.range.extent), parsed.natural.assumption
    ('1980 months', None)

    Daily data stopping on 16 December 2014 is counted as covering December:

    >>> stops_mid_month = FirstAndLastTimeBounds(
    ...     time_variable="time",
    ...     n_steps=60209,
    ...     first_value=0.5,
    ...     last_value=60208.5,
    ...     bounds_attribute=BoundsAttribute.BOUNDS,
    ...     bounds_variable="time_bnds",
    ...     first_bounds=(0.0, 1.0),
    ...     last_bounds=(60208.0, 60209.0),
    ... )
    >>> parsed = parse_variable_values(
    ...     stops_mid_month, variable, project="CMIP6", reporting_interval="day"
    ... )
    >>> str(parsed.natural.range.extent), parsed.natural.assumption.value
    ('1980 months', 'part_steps_count_as_covered')

    Climatology bounds which overlap, as a multi-year climatology's do, give no range:

    >>> averaged = FirstAndLastTimeBounds(
    ...     time_variable="time",
    ...     n_steps=2,
    ...     first_value=15.5,
    ...     last_value=3299.5,
    ...     bounds_attribute=BoundsAttribute.CLIMATOLOGY,
    ...     bounds_variable="climatology_bnds",
    ...     first_bounds=(0.0, 3315.0),
    ...     last_bounds=(31.0, 3343.0),
    ...     climatology_bounds=((0.0, 3315.0), (31.0, 3343.0)),
    ... )
    >>> parse_variable_values(
    ...     averaged, variable, project="CMIP7", reporting_interval="mon"
    ... ).no_range_because.value
    'climatology_bounds_not_contiguous'
    """
    if is_climatology_interval(project, reporting_interval, climatology_intervals):
        return _no_range(NoRangeReason.CLIMATOLOGY)

    if values.first_bounds is None or values.last_bounds is None:
        return _no_range(NoRangeReason.NO_BOUNDS)

    if values.bounds_attribute is BoundsAttribute.CLIMATOLOGY:
        if values.climatology_bounds is None:
            return _no_range(NoRangeReason.CLIMATOLOGY_BOUNDS_NOT_CHECKED)

        if not _meet_end_to_end(values.climatology_bounds, variable.axis):
            return _no_range(NoRangeReason.CLIMATOLOGY_BOUNDS_NOT_CONTIGUOUS)

    dated = TimeRange.from_values(
        values.first_bounds[0], values.last_bounds[1], variable.axis
    )
    natural, widened = _widened_to_whole_steps(
        dated, TimeAxis(TimeUnits(natural_step_for(reporting_interval, deltas)))
    )

    return ParsedTimeValues(
        dated=SourcedTimeRange(range=dated, source=InformationSource.VARIABLE_VALUES),
        natural=SourcedTimeRange(
            range=natural,
            source=InformationSource.VARIABLE_VALUES,
            assumption=Assumption.PART_STEPS_COUNT_AS_COVERED if widened else None,
        ),
        no_range_because=None,
    )


def _no_range(reason: NoRangeReason) -> ParsedTimeValues:
    """
    Get the parse of a file whose bounds give no range

    Parameters
    ----------
    reason
        Why they give none

    Returns
    -------
    :
        No dated range, no natural range, and the reason
    """
    return ParsedTimeValues(dated=None, natural=None, no_range_because=reason)


def _meet_end_to_end(
    pairs: tuple[tuple[float | int, float | int], ...], axis: TimeAxis
) -> bool:
    """
    Determine whether every step starts where the previous one ended

    Parameters
    ----------
    pairs
        Every step's lower and upper bound, in order

    axis
        The axis the bounds are on

    Returns
    -------
    :
        `True` if each upper bound is within
        [ON_A_STEP_EDGE_WITHIN_SECONDS][(m).ON_A_STEP_EDGE_WITHIN_SECONDS] of the next
        lower bound. A single step meets nothing, so is `True`.
    """
    tolerance = _tolerance_on(axis)

    return all(
        abs(next_lower - upper) <= tolerance
        for (_, upper), (next_lower, _) in itertools.pairwise(pairs)
    )


def _widened_to_whole_steps(
    dated: TimeRange, natural_axis: TimeAxis
) -> tuple[TimeRange, bool]:
    """
    Get the range of whole steps a dated range touches

    Parameters
    ----------
    dated
        The range, on a dated axis

    natural_axis
        The natural axis to count it in

    Returns
    -------
    :
        From the step `dated` starts in to the step after the one it ends in, and
        whether either end had to be moved to reach a step edge
    """
    start = convert_time_point(dated.start, natural_axis)
    start_moved = not _is_on_step_edge(start, dated.start)

    # The step a half-open end falls in is the first step not in the data when the end
    # is on its edge, and a step the data reaches part way into when it is not.
    end_exclusive = convert_time_point(dated.end_exclusive, natural_axis)
    end_moved = not _is_on_step_edge(end_exclusive, dated.end_exclusive)
    if end_moved:
        end_exclusive = TimePoint(end_exclusive.value + 1, natural_axis)

    return TimeRange(start=start, end_exclusive=end_exclusive), start_moved or end_moved


def _is_on_step_edge(step: TimePoint, instant: TimePoint) -> bool:
    """
    Determine whether an instant is where a step starts

    Parameters
    ----------
    step
        The step, on a natural axis

    instant
        The instant, on a dated axis

    Returns
    -------
    :
        `True` if `instant` is within
        [ON_A_STEP_EDGE_WITHIN_SECONDS][(m).ON_A_STEP_EDGE_WITHIN_SECONDS] of the
        start of `step`
    """
    edge = convert_time_point(step, instant.axis)

    return abs(edge.value - instant.value) <= _tolerance_on(instant.axis)


def _tolerance_on(axis: TimeAxis) -> float:
    """
    Get how far apart two values on a dated axis can be and still count as equal

    Parameters
    ----------
    axis
        The dated axis

    Returns
    -------
    :
        [ON_A_STEP_EDGE_WITHIN_SECONDS][(m).ON_A_STEP_EDGE_WITHIN_SECONDS] in the axis'
        own step, or zero for a step whose length varies
    """
    seconds_per_step = _SECONDS_PER_DATED_STEP.get(axis.units.step)
    if seconds_per_step is None:
        return 0.0

    return ON_A_STEP_EDGE_WITHIN_SECONDS / seconds_per_step
