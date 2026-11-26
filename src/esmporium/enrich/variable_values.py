"""
Parsing a file's first and last time bounds into the range its data spans

The bounds are the data itself, so the dated range read off them rests on no
assumption: the first lower bound is where the data starts and the last upper bound is
where it ends. The natural range, in whole months or whole years, is worked out from
that, and rests on an assumption only when a bound falls part way through one of its
steps. The dated range is never widened or rounded.
"""

from __future__ import annotations

import re
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
    DELTA_BY_REPORTING_INTERVAL,
    delta_of_reporting_interval,
)
from esmporium.headers.contents import BoundsAttribute

if TYPE_CHECKING:
    from collections.abc import Mapping

    from esmporium.datasets.time import TimeDelta
    from esmporium.enrich.variable_attributes import ParsedTimeVariable
    from esmporium.headers import FirstAndLastTimeBounds

ON_A_STEP_EDGE_WITHIN_SECONDS = 1.0
"""
How close a time bound has to be to the edge of a step to count as on it

Bounds are stored as floating-point numbers, so a bound written as the start of a month
can come back a hair either side of it, e.g. `1415999.9999998` hours rather than
`1416000`. A second is far finer than any step a natural range is counted in, and far
coarser than that error.
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

    [NO_BOUNDS][esmporium.datasets.NoRangeReason.NO_BOUNDS] for a time variable with
    no bounds; [CLIMATOLOGY][esmporium.datasets.NoRangeReason.CLIMATOLOGY] for a
    multi-year climatology, whose bounds are the period averaged over; and
    [CLIMATOLOGY_OF_AN_UNSTATED_KIND][esmporium.datasets.NoRangeReason.CLIMATOLOGY_OF_AN_UNSTATED_KIND]
    for climatology bounds with no `cell_methods` to say which kind they are.
    """


_OVER_YEARS = re.compile(r"\bover\s+years\b")
"""What CF's `cell_methods` says of a statistic taken across years"""


def is_multi_year_climatology(cell_methods: str) -> bool:
    """
    Determine whether a data variable's `cell_methods` describe a multi-year climatology

    CF names two kinds of statistic with climatology bounds (section 7.4). One is
    taken within each step and then across the years, e.g. the average January over
    1961 to 1990, whose bounds span the whole period. The other is taken within a
    shorter period and then over that period, e.g. a monthly mean of daily minima, whose
    bounds are each month's own. Only the first is taken over years.

    Parameters
    ----------
    cell_methods
        The data variable's `cell_methods` attribute

    Returns
    -------
    :
        `True` if it takes a statistic over years

    Examples
    --------
    >>> is_multi_year_climatology("time: mean within years time: mean over years")
    True

    >>> is_multi_year_climatology(
    ...     "area: mean time: minimum within days time: mean over days"
    ... )
    False
    """
    return _OVER_YEARS.search(cell_methods) is not None


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


def parse_variable_values(
    values: FirstAndLastTimeBounds,
    variable: ParsedTimeVariable,
    *,
    cell_methods: str | None,
    reporting_interval: str,
    deltas: Mapping[str, TimeDelta] = DELTA_BY_REPORTING_INTERVAL,
) -> ParsedTimeValues:
    """
    Parse a file's first and last time bounds into the range its data spans

    Parameters
    ----------
    values
        The first and last values and bounds of the file's time variable, as
        [esmporium.headers][] read them

    variable
        What the same time variable's attributes say, which gives the axis the bounds
        are on

    cell_methods
        The data variable's `cell_methods` attribute, or `None` if it has none

        Needed only when the bounds are climatology bounds, to say which kind of
        climatology the data is. See
        [is_multi_year_climatology][(m).is_multi_year_climatology]. Passed every time
        rather than defaulted, so that a caller cannot forget it for a file which needs
        it.

    reporting_interval
        The dataset's reporting interval, which picks the step of the natural range.
        See [natural_step_for][(m).natural_step_for].

    deltas
        How wide one step of each reporting interval is

    Returns
    -------
    :
        The dated range, the natural range, or why there is neither

    Raises
    ------
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
    ...     values,
    ...     variable,
    ...     cell_methods="area: mean time: mean",
    ...     reporting_interval="mon",
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
    ...     stops_mid_month,
    ...     variable,
    ...     cell_methods="area: mean time: mean",
    ...     reporting_interval="day",
    ... )
    >>> str(parsed.natural.range.extent), parsed.natural.assumption.value
    ('1980 months', 'part_steps_count_as_covered')

    A multi-year climatology gives no range, because its bounds are the period averaged
    over rather than a run of data:

    >>> averaged = FirstAndLastTimeBounds(
    ...     time_variable="time",
    ...     n_steps=12,
    ...     first_value=15.5,
    ...     last_value=3299.5,
    ...     bounds_attribute=BoundsAttribute.CLIMATOLOGY,
    ...     bounds_variable="climatology_bnds",
    ...     first_bounds=(0.0, 3315.0),
    ...     last_bounds=(334.0, 3650.0),
    ... )
    >>> parse_variable_values(
    ...     averaged,
    ...     variable,
    ...     cell_methods="time: mean within years time: mean over years",
    ...     reporting_interval="monC",
    ... ).no_range_because.value
    'climatology'
    """
    if values.first_bounds is None or values.last_bounds is None:
        return ParsedTimeValues(
            dated=None, natural=None, no_range_because=NoRangeReason.NO_BOUNDS
        )

    if values.bounds_attribute is BoundsAttribute.CLIMATOLOGY:
        if cell_methods is None:
            return ParsedTimeValues(
                dated=None,
                natural=None,
                no_range_because=NoRangeReason.CLIMATOLOGY_OF_AN_UNSTATED_KIND,
            )

        if is_multi_year_climatology(cell_methods):
            return ParsedTimeValues(
                dated=None, natural=None, no_range_because=NoRangeReason.CLIMATOLOGY
            )

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
    seconds_per_step = _SECONDS_PER_DATED_STEP.get(instant.axis.units.step)
    tolerance = (
        0.0
        if seconds_per_step is None
        else ON_A_STEP_EDGE_WITHIN_SECONDS / seconds_per_step
    )

    return abs(edge.value - instant.value) <= tolerance
