"""
Catalogues the requirements tests need beyond what esmporium ships
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from esmporium.datasets import (
    BranchPoint,
    TimeAxis,
    TimePoint,
    TimeRange,
    TimeStep,
    TimeUnits,
    VersionTimeRange,
)
from esmporium.requirements import CatalogueEntry, InMemoryCatalogue

MONTHLY_AXIS = TimeAxis(TimeUnits(TimeStep.MONTHS))
"""
The axis these catalogues record on, unless a test says otherwise

Monthly because the analyses in `use_cases.py` ask for `reporting_interval="mon"`, so
a monthly axis is what their data would really be recorded on -- and because it is the
case which exercises the one conversion that matters: a requirement stated in years,
judged against an axis counted in months.
"""

YEARLY_AXIS = TimeAxis(TimeUnits(TimeStep.YEARS))
"""
A yearly axis, for the tests whose arithmetic is easier to read a year at a time
"""


def recorded_range(
    start: int,
    end_exclusive: int,
    axis: TimeAxis = MONTHLY_AXIS,
    *,
    contiguous: bool = True,
) -> VersionTimeRange:
    """
    Get a recorded range, for a test which does not care how it was recorded

    Parameters
    ----------
    start
        First step in the data

    end_exclusive
        First step after it

    axis
        The axis the two are positions on

    contiguous
        Whether the version's files fill the span

    Returns
    -------
    :
        The range, with a file count which is only there because a real one has one
    """
    return VersionTimeRange(
        range=TimeRange.from_values(start, end_exclusive, axis),
        is_contiguous=contiguous,
        n_files=1,
    )


def branched_at(
    position: int,
    axis: TimeAxis = MONTHLY_AXIS,
    *,
    in_child: int | None = 0,
    child_axis: TimeAxis | None = None,
) -> BranchPoint:
    """
    Get a branch point, on the parent's axis and the child's

    Parameters
    ----------
    position
        Where the child branched, as a position on the parent's axis

    axis
        The parent's axis

    in_child
        Where the child branched, as a position on its own axis

        `0` by default, because the child's data in most tests starts at 0, and
        `None` for a branch point which does not say, as a CMIP5 one does not.

    child_axis
        The child's axis, or `None` for the same as `axis`

    Returns
    -------
    :
        The branch point
    """
    if in_child is None:
        return BranchPoint(in_parent=TimePoint(position, axis))

    return BranchPoint(
        in_parent=TimePoint(position, axis),
        in_child=TimePoint(in_child, axis if child_axis is None else child_axis),
    )


@dataclass(frozen=True)
class InMemoryCatalogueWithTimeInfo(InMemoryCatalogue):
    """
    An in-memory catalogue which also knows time ranges and branch points

    Satisfies [CatalogueWithTimeInfo][esmporium.requirements.CatalogueWithTimeInfo],
    so it can be used to solve requirements which use
    [Covers][esmporium.requirements.Covers].

    A dataset absent from either mapping answers `None`, which is what "nobody has
    enriched this yet" is: leaving one out is how a test says so, and is different
    from a dataset whose range says it is not covered.
    """

    time_ranges: Mapping[int, VersionTimeRange] = field(default_factory=dict)
    """Entry ID -> what its files were combined to add up to"""

    branch_points: Mapping[int, BranchPoint] = field(default_factory=dict)
    """Entry ID -> where it branched from its parent, on both runs' axes"""

    def get_time_range(self, entry: CatalogueEntry) -> VersionTimeRange | None:
        """Get the span of a dataset's data, or `None` if nothing recorded it"""
        return self.time_ranges.get(entry.id)

    def get_branch_point(self, entry: CatalogueEntry) -> BranchPoint | None:
        """Get where a dataset branched, or `None` if nothing recorded it"""
        return self.branch_points.get(entry.id)
