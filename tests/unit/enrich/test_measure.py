"""
Tests of combining one version's files into one answer about that version

A gap is the thing that must not be smoothed over, so most of this is gaps.
"""

from __future__ import annotations

import pytest

from esmporium.datasets.time import (
    MismatchedTimeAxisError,
    TimeAxis,
    TimeRange,
    TimeStep,
    TimeUnits,
    total_months,
)
from esmporium.enrich.measure import NoFilesToMeasureError, measure_version

MONTHLY = TimeAxis(TimeUnits(TimeStep.MONTHS))
YEARLY = TimeAxis(TimeUnits(TimeStep.YEARS))


def decade(first_year: int) -> TimeRange:
    """
    Get a monthly range covering ten years from the January of a year

    Parameters
    ----------
    first_year
        The first year in the range

    Returns
    -------
    :
        A monthly range of 120 months
    """
    return TimeRange(
        total_months(first_year, 1), total_months(first_year + 10, 1), MONTHLY
    )


def test_files_which_meet_make_a_contiguous_version():
    """
    Three files, each starting where the last one stopped
    """
    measured = measure_version([decade(1850), decade(1860), decade(1870)])

    assert measured.range.extent == 360
    assert measured.is_contiguous
    assert measured.n_files == 3


def test_the_order_the_files_arrive_in_does_not_matter():
    """
    A catalogue hands them over in whatever order it found them
    """
    measured = measure_version([decade(1870), decade(1850), decade(1860)])

    assert measured.range.start == total_months(1850, 1)
    assert measured.is_contiguous


def test_a_gap_is_reported_rather_than_smoothed_over():
    """
    The span is still the span, and the flag is what says not to trust it

    `Covers` refuses a gapped range on the strength of the flag, so recording both is
    the point: 240 months of data across a 360-month span is not 360 months of data.
    """
    measured = measure_version([decade(1850), decade(1870)])

    assert measured.range.extent == 360
    assert not measured.is_contiguous
    assert measured.n_files == 2


def test_a_single_missing_month_is_a_gap():
    """
    The smallest gap there is, which is the one worth catching
    """
    first = TimeRange(0, 120, MONTHLY)
    after_one_missing_month = TimeRange(121, 240, MONTHLY)

    assert not measure_version([first, after_one_missing_month]).is_contiguous


def test_an_overlap_is_a_failure_too():
    """
    Two files covering the same month is as much a sign of a broken publication as a
    month nobody published
    """
    measured = measure_version(
        [TimeRange(0, 120, MONTHLY), TimeRange(119, 240, MONTHLY)]
    )

    assert not measured.is_contiguous


def test_the_same_file_twice_is_not_contiguous():
    """
    Which is what a duplicated row in a catalogue looks like from here
    """
    assert not measure_version([decade(1850), decade(1850)]).is_contiguous


def test_one_file_is_contiguous_with_itself():
    """
    There is no gap in a single file, so the flag has to be true
    """
    measured = measure_version([decade(1850)])

    assert measured.is_contiguous
    assert measured.n_files == 1


def test_a_version_with_no_files_has_no_range():
    """
    Not an empty one: nothing was measured, so there is nothing to record
    """
    with pytest.raises(NoFilesToMeasureError):
        measure_version([])


def test_files_which_disagree_about_their_axis_are_refused():
    """
    Which is a fact about the publication rather than about the question
    """
    with pytest.raises(MismatchedTimeAxisError):
        measure_version([TimeRange(0, 120, MONTHLY), TimeRange(1850, 1860, YEARLY)])
