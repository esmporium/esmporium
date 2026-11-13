"""
Catalogues the requirements tests need beyond what esmporium ships
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from esmporium.datasets import (
    BranchPoint,
    TimeAxis,
    TimeRange,
    TimeStep,
    TimeUnits,
    VersionTimeRange,
)
from esmporium.requirements import CatalogueEntry, InMemoryCatalogue

MONTHLY_AXIS = TimeAxis(TimeUnits(TimeStep.MONTHS))
"""
The axis these catalogues measure on, unless a test says otherwise

Monthly because the analyses in `use_cases.py` ask for `reporting_interval="mon"`, so
a monthly axis is what their data would really be measured on -- and because it is the
case which exercises the one conversion that matters: a requirement stated in years,
judged against an axis counted in months.
"""

YEARLY_AXIS = TimeAxis(TimeUnits(TimeStep.YEARS))
"""
A yearly axis, for the tests whose arithmetic is easier to read a year at a time
"""


def measured(
    start: int,
    end_exclusive: int,
    axis: TimeAxis = MONTHLY_AXIS,
    *,
    contiguous: bool = True,
) -> VersionTimeRange:
    """
    Get a measured range, for a test which does not care how it was measured

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
        range=TimeRange(start, end_exclusive, axis),
        is_contiguous=contiguous,
        n_files=1,
    )


def branched_at(position: int, axis: TimeAxis = MONTHLY_AXIS) -> BranchPoint:
    """
    Get a branch point on a parent's axis

    Parameters
    ----------
    position
        Where the child's data starts, as a position on the parent's axis

    axis
        The parent's axis

    Returns
    -------
    :
        The branch point
    """
    return BranchPoint(in_parent=position, parent_axis=axis)


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
    """Entry ID -> what its files were measured to add up to"""

    branch_times_in_parent: Mapping[int, BranchPoint] = field(default_factory=dict)
    """Entry ID -> where it branched from its parent, on the parent's axis"""

    def get_time_range(self, entry: CatalogueEntry) -> VersionTimeRange | None:
        """Get the span of a dataset's data, or `None` if nothing measured it"""
        return self.time_ranges.get(entry.id)

    def get_branch_time_in_parent(self, entry: CatalogueEntry) -> BranchPoint | None:
        """Get where a dataset branched, or `None` if nothing recorded it"""
        return self.branch_times_in_parent.get(entry.id)
