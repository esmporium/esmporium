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
    NoCalendarError,
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


@pytest.mark.parametrize("calendar", [None, "", b""])
def test_a_file_which_names_no_calendar_says_so(calendar):
    """
    Rather than assuming CF's `standard`

    CF's rule is how to read a file, not evidence of what the model ran, and a silent
    default is how a day count comes out wrong unnoticed. The message lists what the
    attributes did say, so whoever reads it can see what was there.
    """
    attributes: dict[str, object] = {"units": "days since 1850-01-01", "axis": "T"}
    if calendar is not None:
        attributes["calendar"] = calendar

    with pytest.raises(NoCalendarError, match=r"axis, .*units") as error:
        read_time_axis(attributes)

    assert error.value.attributes is attributes


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
