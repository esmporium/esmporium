"""
Combining the files of one version into one answer about that version

A version is what a time range hangs off -- not a dataset, because a time range is a
per-version fact, and not a file, because a requirement asks about a run and not about
one of the pieces it was published in. So the files have to be combined, and the one
thing that must not be smoothed over while combining them is a gap.

The combining is here; the value it produces is
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


class NoFilesToCombineError(ValueError):
    """
    Raised when a version's time range is asked for and no files were given
    """

    def __init__(self) -> None:
        """
        Initialise the error
        """
        super().__init__(
            "A version with no files has no time range, not an empty one. There is "
            "nothing to combine, so there is nothing to record."
        )


def combine_file_ranges(ranges: Iterable[TimeRange]) -> VersionTimeRange:
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
    NoFilesToCombineError
        No ranges were given

    MismatchedTimeAxisError
        The ranges are not all on comparable axes, which means something is wrong with
        the publication rather than with the question

    Examples
    --------
    Two files which meet exactly:

    >>> from esmporium.datasets.time import TimeAxis, TimeStep, TimeUnits
    >>> monthly = TimeAxis(TimeUnits(TimeStep.MONTHS))
    >>> combined = combine_file_ranges(
    ...     [
    ...         TimeRange.from_values(0, 120, monthly),
    ...         TimeRange.from_values(120, 240, monthly),
    ...     ]
    ... )
    >>> str(combined.range.extent), combined.is_contiguous, combined.n_files
    ('240 months', True, 2)

    A month nobody published still has a span, and says it is not contiguous rather
    than quietly reporting 239 months of data as 240:

    >>> gapped = combine_file_ranges(
    ...     [
    ...         TimeRange.from_values(0, 120, monthly),
    ...         TimeRange.from_values(121, 240, monthly),
    ...     ]
    ... )
    >>> str(gapped.range.extent), gapped.is_contiguous
    ('240 months', False)

    One file is contiguous with itself:

    >>> combine_file_ranges([TimeRange.from_values(0, 1, monthly)]).is_contiguous
    True
    """
    # By value: the axes are checked when the ranges are spanned, just below.
    in_order = sorted(
        tuple(ranges), key=lambda each: (each.start.value, each.end_exclusive.value)
    )
    if not in_order:
        raise NoFilesToCombineError

    return VersionTimeRange(
        range=spanned(in_order),
        is_contiguous=all(
            earlier.is_adjacent_to(later)
            for earlier, later in itertools.pairwise(in_order)
        ),
        n_files=len(in_order),
    )
