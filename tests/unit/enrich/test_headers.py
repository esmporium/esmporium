"""
Tests of reading a time axis out of a time variable's attributes

Note what is being passed in throughout: the attributes of the file's **time
variable**. A CMIP file does not publish its own calendar globally, which is why these
tests never build a dict of global attributes and call it a header.
"""

from __future__ import annotations

import pytest

from esmporium.datasets.time import TimeStep, UnknownTimeUnitsError
from esmporium.enrich.headers import (
    CF_DEFAULT_CALENDAR,
    NoTimeUnitsError,
    read_time_axis,
)


@pytest.mark.parametrize(
    ("attributes", "units", "calendar"),
    [
        (
            {"units": "days since 1850-01-01", "calendar": "noleap"},
            "days since 1850-01-01",
            "noleap",
        ),
        (
            {"units": "days since 0001-01-01 00:00:00", "calendar": "360_day"},
            "days since 0001-01-01 00:00:00",
            "360_day",
        ),
        (
            {"units": "hours since 1850-1-1", "calendar": "standard"},
            "hours since 1850-1-1",
            "standard",
        ),
    ],
)
def test_reading_a_time_axis(attributes, units, calendar):
    """
    The units string is kept as the file wrote it, because that is the observation
    """
    axis = read_time_axis(attributes)

    assert str(axis.units) == units
    assert axis.calendar == calendar


def test_other_attributes_are_ignored():
    """
    A time variable carries bounds, axis labels and long names too
    """
    axis = read_time_axis(
        {
            "units": "days since 1850-01-01",
            "calendar": "noleap",
            "bounds": "time_bnds",
            "axis": "T",
            "long_name": "time",
            "standard_name": "time",
        }
    )

    assert axis.units.step == TimeStep.DAYS


def test_a_file_which_names_no_calendar_gets_cfs_own_default():
    """
    CF says a silent time coordinate is on the standard calendar

    Recorded as `standard` rather than left empty, because `None` is reserved for not
    having read a header at all -- a different thing from having read one which stayed
    silent.
    """
    assert read_time_axis({"units": "days since 1850-01-01"}).calendar == (
        CF_DEFAULT_CALENDAR
    )


def test_attributes_with_no_units_say_what_is_missing_and_why():
    """
    And point at the mistake most likely to have caused it

    Asking for global attributes and finding no `units` is exactly what happens if
    you read the wrong half of the header, so the message says so.
    """
    with pytest.raises(NoTimeUnitsError, match="global attributes"):
        read_time_axis({"long_name": "time", "axis": "T"})


def test_units_we_cannot_read_are_refused():
    """
    Rather than guessed at
    """
    with pytest.raises(UnknownTimeUnitsError):
        read_time_axis({"units": "fortnights since 1850-01-01"})


def test_a_character_attribute_stored_as_bytes_is_decoded():
    """
    Not stringified, which would give a calendar name of `b'noleap'`

    A classic netCDF `NC_CHAR` attribute is bytes on the wire, and `str()` of those
    is a name no calendar library has heard of -- arrived at silently, which is the
    worst way to be wrong.
    """
    axis = read_time_axis({"units": b"days since 1850-01-01", "calendar": b"noleap"})

    assert axis.calendar == "noleap"
    assert str(axis.units) == "days since 1850-01-01"
