"""
Combining the files of one version into one answer about that version

A version is what a time range hangs off -- not a dataset, because a time range is a
per-version fact, and not a file, because a requirement asks about a run and not about
one of the pieces it was published in. So the files have to be combined, and the one
thing that must not be smoothed over while combining them is a gap.

The measurement is here; the value it produces is
[VersionTimeRange][esmporium.datasets.VersionTimeRange], which lives in
`esmporium.datasets` because [Covers][esmporium.requirements.Covers] reads one at
solve time and `esmporium.requirements` may not import this package.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING

from esmporium.datasets.time import TimeRange, VersionTimeRange, spanned

if TYPE_CHECKING:
    from collections.abc import Iterable


class NoFilesToMeasureError(ValueError):
    """
    Raised when a version's time range is asked for and no files were given
    """

    def __init__(self) -> None:
        """
        Initialise the error
        """
        super().__init__(
            "A version with no files has no time range, not an empty one. Nothing "
            "was measured, so there is nothing to record -- which is what a status "
            "of 'nobody measured this' is for."
        )


def measure_version(ranges: Iterable[TimeRange]) -> VersionTimeRange:
    """
    Combine the ranges of one version's files into one range for the version

    Parameters
    ----------
    ranges
        One range per file, in any order, all on comparable axes

    Returns
    -------
    :
        The version's span, whether its files fill it, and how many there were

    Raises
    ------
    NoFilesToMeasureError
        No ranges were given

    MismatchedTimeAxisError
        The ranges are not all on comparable axes, which means something is wrong with
        the publication rather than with the question

    Examples
    --------
    Two files which meet exactly:

    >>> from esmporium.datasets.time import TimeAxis, TimeStep, TimeUnits
    >>> monthly = TimeAxis(TimeUnits(TimeStep.MONTHS))
    >>> measured = measure_version(
    ...     [TimeRange(0, 120, monthly), TimeRange(120, 240, monthly)]
    ... )
    >>> measured.range.extent, measured.is_contiguous, measured.n_files
    (240, True, 2)

    A month nobody published still has a span, and says it is not contiguous rather
    than quietly reporting 239 months of data as 240:

    >>> gapped = measure_version(
    ...     [TimeRange(0, 120, monthly), TimeRange(121, 240, monthly)]
    ... )
    >>> gapped.range.extent, gapped.is_contiguous
    (240, False)

    One file is contiguous with itself:

    >>> measure_version([TimeRange(0, 1, monthly)]).is_contiguous
    True
    """
    in_order = sorted(tuple(ranges), key=lambda each: (each.start, each.end_exclusive))
    if not in_order:
        raise NoFilesToMeasureError

    return VersionTimeRange(
        range=spanned(in_order),
        is_contiguous=all(
            earlier.is_adjacent_to(later)
            for earlier, later in itertools.pairwise(in_order)
        ),
        n_files=len(in_order),
    )
