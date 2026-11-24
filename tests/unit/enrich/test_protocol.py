"""
Tests of the published range and the one conversion off it

`to_half_open` is the only place in esmporium where an inclusive range becomes a
half-open one, so it is the only place the `+ 1` lives -- and therefore the only place
these tests need to look for an off-by-one.
"""

from __future__ import annotations

import pytest

from esmporium.datasets.time import Span, TimeRange, TimeStep, total_months
from esmporium.enrich.protocol import (
    CalendarRequiredError,
    PublishedRange,
    StampPrecisionMismatchError,
    UnreadableTimestampError,
)

ONE_MONTH = Span(1, TimeStep.MONTHS)
ONE_YEAR = Span(1, TimeStep.YEARS)


def monthly(start_raw: str, end_raw: str) -> TimeRange:
    """
    Get the half-open range of a monthly published range

    Parameters
    ----------
    start_raw
        The start, as a filename spells it

    end_raw
        The end, as a filename spells it

    Returns
    -------
    :
        The range, on a natural monthly axis
    """
    return PublishedRange(start_raw, end_raw).to_half_open(
        precision=TimeStep.MONTHS, reports_every=ONE_MONTH
    )


def test_both_ends_of_a_published_range_are_in_the_data():
    """
    Which is the fact every off-by-one here comes from

    A file named `..._185001-186012.nc` contains December 1860, so the first month
    *not* in it is January 1861.
    """
    half_open = monthly("185001", "186012")

    assert half_open.start == total_months(1850, 1)
    assert half_open.end_exclusive == total_months(1861, 1)
    assert half_open.extent == Span(132, TimeStep.MONTHS)


def test_a_single_step_range_has_an_extent_of_one():
    """
    Where subtracting the published values gives nought
    """
    assert monthly("320001", "320001").extent == ONE_MONTH


def test_yearly_data_is_eleven_years_not_ten():
    half_open = PublishedRange("1850", "1860").to_half_open(
        precision=TimeStep.YEARS, reports_every=ONE_YEAR
    )

    assert (half_open.start, half_open.end_exclusive) == (1850, 1861)
    assert half_open.extent == Span(11, TimeStep.YEARS)


def test_the_raw_strings_are_kept_beside_the_numbers():
    """
    The string is the observation and the number is our parse of it

    Keeping both is what lets a wrong parser be corrected without re-reading anything.
    """
    published = PublishedRange("320001", "325012")

    assert (published.start_raw, published.end_raw) == ("320001", "325012")
    assert monthly(published.start_raw, published.end_raw).start == 38389


def test_a_published_range_holds_only_what_the_filename_says():
    """
    How precisely it is written and how wide its last step is are readings of it,
    passed to the conversion rather than stored
    """
    published = PublishedRange("185001", "185012", suffix="-clim")

    assert (published.start_raw, published.end_raw, published.suffix) == (
        "185001",
        "185012",
        "-clim",
    )


def test_a_natural_range_has_no_epoch_and_no_calendar():
    """
    Which is what makes it the same count in every calendar
    """
    axis = monthly("185001", "186012").axis

    assert axis.units.is_natural
    assert axis.calendar is None


def test_the_half_open_end_is_one_reporting_step_past_the_last_stamp():
    """
    Which is what makes it work at a frequency coarser than its own stamps

    A `dec` file stamped to the year runs ten years past its last stamp.
    """
    half_open = PublishedRange("1850", "1950").to_half_open(
        precision=TimeStep.YEARS, reports_every=Span(10, TimeStep.YEARS)
    )

    assert half_open.end_exclusive == 1960


@pytest.mark.parametrize(
    ("start_raw", "end_raw", "precision", "reports_every"),
    [
        ("18500101", "18591231", TimeStep.DAYS, Span(1, TimeStep.DAYS)),
        ("1850010103", "1859123121", TimeStep.HOURS, Span(3, TimeStep.HOURS)),
        ("185001010300", "185101010000", TimeStep.MINUTES, Span(3, TimeStep.HOURS)),
    ],
)
def test_a_sub_monthly_range_has_no_natural_axis(
    start_raw, end_raw, precision, reports_every
):
    """
    Two dates are not a length without a calendar, and the stamps are not padded out
    to put them on a dated axis
    """
    published = PublishedRange(start_raw, end_raw)

    with pytest.raises(CalendarRequiredError, match="time bounds") as caught:
        published.to_half_open(precision=precision, reports_every=reports_every)

    assert caught.value.published is published
    assert caught.value.precision is precision


@pytest.mark.parametrize(
    ("start_raw", "end_raw", "precision"),
    [
        ("185001", "185012", TimeStep.YEARS),
        ("1850", "1860", TimeStep.MONTHS),
        ("1850", "186012", TimeStep.MONTHS),
    ],
)
def test_a_stamp_read_at_another_precision_is_refused(start_raw, end_raw, precision):
    """
    Reading a month stamp as a year would drop the month, and a year stamp as a month
    would fill one in
    """
    with pytest.raises(StampPrecisionMismatchError):
        PublishedRange(start_raw, end_raw).to_half_open(
            precision=precision, reports_every=ONE_MONTH
        )


@pytest.mark.parametrize("stamp", ["185", "1850-01", "185x01", "18500101003000"])
def test_converting_an_unreadable_stamp_is_refused(stamp):
    """
    The conversion reads the strings it was given, so it can still find them wrong

    A range built from a bad parse says so when it is used, rather than quietly
    carrying a wrong number around.
    """
    published = PublishedRange(stamp, stamp)

    with pytest.raises(UnreadableTimestampError):
        published.to_half_open(precision=TimeStep.MONTHS, reports_every=ONE_MONTH)
