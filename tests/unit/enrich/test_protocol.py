"""
Tests of the published range and the one conversion off it

`to_half_open` is the only place in esmporium where an inclusive range becomes a
half-open one, so it is the only place the `+ 1` lives -- and therefore the only place
these tests need to look for an off-by-one.
"""

from __future__ import annotations

import pytest

from esmporium.datasets.time import Span, TimeAxis, TimeStep, TimeUnits, total_months
from esmporium.enrich.protocol import (
    CalendarRequiredError,
    PublishedRange,
    UnreadableTimestampError,
    step_of_stamp,
)

MONTHLY = Span(1, TimeStep.MONTHS)
YEARLY = Span(1, TimeStep.YEARS)


def monthly(start_raw: str, end_raw: str) -> PublishedRange:
    """
    Get a monthly published range

    Parameters
    ----------
    start_raw
        The start, as a filename spells it

    end_raw
        The end, as a filename spells it

    Returns
    -------
    :
        The range
    """
    return PublishedRange(start_raw, end_raw, TimeStep.MONTHS, MONTHLY)


def test_both_ends_of_a_published_range_are_in_the_data():
    """
    Which is the fact every off-by-one here comes from

    A file named `..._185001-186012.nc` contains December 1860, so the first month
    *not* in it is January 1861.
    """
    half_open = monthly("185001", "186012").to_half_open()

    assert half_open.start == total_months(1850, 1)
    assert half_open.end_exclusive == total_months(1861, 1)
    assert half_open.extent == 132


def test_a_single_step_range_has_an_extent_of_one():
    """
    Where subtracting the published values gives nought
    """
    assert monthly("320001", "320001").to_half_open().extent == 1


def test_yearly_data_is_eleven_years_not_ten():
    """
    The example the design note records as having caught many people
    """
    published = PublishedRange("1850", "1860", TimeStep.YEARS, YEARLY)
    half_open = published.to_half_open()

    assert (half_open.start, half_open.end_exclusive) == (1850, 1861)
    assert half_open.extent == 11


def test_the_raw_strings_are_kept_beside_the_numbers():
    """
    The string is the observation and the number is our parse of it

    Keeping both is what lets a wrong parser be corrected without re-reading anything.
    """
    published = monthly("320001", "325012")

    assert (published.start_raw, published.end_raw) == ("320001", "325012")
    assert published.to_half_open().start == 38389


def test_a_natural_range_has_no_epoch_and_no_calendar():
    """
    Which is what makes it the same count in every calendar
    """
    axis = monthly("185001", "186012").to_half_open().axis

    assert axis.units.is_natural
    assert axis.calendar is None


def test_a_published_range_can_be_placed_on_a_dated_axis():
    """
    The rich row, once a header has been read

    Same filename, different axis: a monthly range on a daily axis is a count of days.
    """
    axis = TimeAxis(TimeUnits(TimeStep.DAYS, "1850-01-01"), "noleap")

    assert monthly("185001", "185012").to_half_open(axis=axis).extent == 365


def test_the_half_open_end_is_one_reporting_step_past_the_last_stamp():
    """
    Which is what makes it exact at a frequency coarser than its own stamps

    A `dec` file stamped to the year runs ten years past its last stamp.
    """
    published = PublishedRange("1850", "1950", TimeStep.YEARS, Span(10, TimeStep.YEARS))

    assert published.to_half_open().end_exclusive == 1960


@pytest.mark.parametrize(
    ("precision", "reports_every"),
    [
        (TimeStep.DAYS, Span(1, TimeStep.DAYS)),
        (TimeStep.HOURS, Span(3, TimeStep.HOURS)),
        (TimeStep.MINUTES, Span(3, TimeStep.HOURS)),
    ],
)
def test_a_sub_monthly_range_refuses_to_guess_a_calendar(precision, reports_every):
    """
    Two dates are not a length, and the error names the work to be done
    """
    published = PublishedRange("18500101", "18591231", precision, reports_every)

    with pytest.raises(CalendarRequiredError, match="calendar"):
        published.to_half_open()


def test_the_calendar_a_sub_monthly_range_needs_is_the_only_thing_missing():
    """
    So the error carries the range, ready to be asked again
    """
    published = PublishedRange(
        "18500101", "18591231", TimeStep.DAYS, Span(1, TimeStep.DAYS)
    )

    with pytest.raises(CalendarRequiredError) as caught:
        published.to_half_open()

    assert caught.value.published is published


@pytest.mark.parametrize(
    ("stamp", "expected"),
    [
        ("1850", TimeStep.YEARS),
        ("185001", TimeStep.MONTHS),
        ("18500101", TimeStep.DAYS),
        ("1850010103", TimeStep.HOURS),
        ("185001010300", TimeStep.MINUTES),
    ],
)
def test_how_precisely_a_stamp_is_written(stamp, expected):
    """
    Read off its length, which is all a filename gives us to go on
    """
    assert step_of_stamp(stamp) == expected


@pytest.mark.parametrize(
    "stamp", ["185", "18500101000000", "", "1850-01", "185x01", "18500101003000"]
)
def test_a_stamp_we_cannot_read_is_refused(stamp):
    """
    Fourteen digits -- seconds -- included: `subhr` has no step width either, so
    reading it as minutes would be a wrong answer presented as a right one
    """
    with pytest.raises(UnreadableTimestampError):
        step_of_stamp(stamp)


def test_converting_an_unreadable_stamp_is_refused():
    """
    The conversion re-reads the strings it kept, so it can still find them wrong

    Which is the point of keeping them: a range built from a bad parse says so when
    it is used, rather than quietly carrying a wrong number around.
    """
    published = PublishedRange("185", "186", TimeStep.MONTHS, MONTHLY)

    with pytest.raises(UnreadableTimestampError):
        published.to_half_open()


def test_a_climatology_is_recorded_rather_than_acted_on():
    """
    Its span is the climatology period its filename names, so the arithmetic is the
    same -- but a reader of the stored row should know the twelve months are not
    twelve months of a simulation
    """
    published = PublishedRange(
        "185001", "185012", TimeStep.MONTHS, MONTHLY, is_climatology=True
    )

    assert published.is_climatology
    assert published.to_half_open().extent == 12
