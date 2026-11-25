"""
Tests of working out what a file is, and reading it accordingly
"""

from __future__ import annotations

import pytest
from tests.conftest import CMIP6_FILE_SIZE

from esmporium.headers import (
    PARSERS_BY_LAYOUT,
    HeaderLayout,
    HeldBytesSource,
    MissingVariableAttributesError,
    NoSuchVariableError,
    UnsupportedHeaderFormatError,
    read_file_header,
    read_first_and_last_time_bounds,
    read_global_attributes,
    read_variable_attributes,
    sniff_layout,
)


@pytest.mark.parametrize(
    ("magic", "expected"),
    [
        (b"CDF\x01", HeaderLayout.NETCDF3),
        (b"CDF\x02", HeaderLayout.NETCDF3),
        (b"\x89HDF\r\n\x1a\n", HeaderLayout.NETCDF4_HDF5),
    ],
)
def test_each_layout_is_recognised_by_its_magic(magic, expected):
    assert sniff_layout(magic + bytes(64)) == expected


@pytest.mark.parametrize(
    "magic",
    [
        pytest.param(b"CDF\x05", id="cdf5-is-refused-rather-than-misread"),
        pytest.param(b"GRIB", id="another-format-entirely"),
        pytest.param(b"", id="no-bytes-at-all"),
        pytest.param(b"CDF", id="fewer-bytes-than-a-magic-number"),
    ],
)
def test_anything_else_is_refused(magic):
    """
    Including CDF-5, which is the one worth being deliberate about

    CDF-5 is classic netCDF with eight-byte element counts. Treating it as `CDF\\x01`
    would not raise -- it would read the counts at the wrong width and return
    plausible-looking nonsense, which is worse than refusing.
    """
    with pytest.raises(UnsupportedHeaderFormatError):
        sniff_layout(magic)


def test_the_refusal_reports_the_bytes_it_saw():
    with pytest.raises(UnsupportedHeaderFormatError) as caught:
        sniff_layout(b"GRIB\x00\x00\x00\x02rest of the file")

    assert caught.value.magic == b"GRIB\x00\x00\x00\x02"


def test_every_recognisable_layout_has_a_parser():
    """
    Recognising a layout and being able to read it are the same list

    If these ever came apart, `read_file_header` would raise `KeyError` for a layout
    it had just successfully identified.
    """
    assert set(PARSERS_BY_LAYOUT) == set(HeaderLayout)


def test_a_real_cmip5_file_is_dispatched_to_the_classic_reader(recorded_source):
    attributes = read_global_attributes(recorded_source("cmip5-netcdf3-time-bounds"))

    assert attributes["parent_experiment_id"] == "piControl"
    assert attributes["project_id"] == "CMIP5"


def test_a_real_cmip6_file_is_dispatched_to_the_hdf5_reader(
    needs_h5netcdf, cmip6_netcdf4_prefix
):
    attributes = read_global_attributes(
        HeldBytesSource(cmip6_netcdf4_prefix, size=CMIP6_FILE_SIZE)
    )

    assert attributes["parent_experiment_id"] == "piControl"
    assert attributes["mip_era"] == "CMIP6"


def test_a_file_too_short_to_hold_a_magic_number_is_refused():
    with pytest.raises(UnsupportedHeaderFormatError):
        read_file_header(HeldBytesSource(b"CD", size=2))


class TestReadingVariableAttributes:
    def test_the_variable_s_attributes_come_back(self, recorded_source):
        attributes = read_variable_attributes(
            recorded_source("cmip5-netcdf3-time-bounds"),
            "time",
            wanted=("units", "calendar"),
        )

        assert attributes["calendar"] == "noleap"
        assert attributes["units"] == "days since 0240-01-01"
        assert attributes["bounds"] == "time_bnds"

    def test_a_missing_variable_lists_the_ones_there_are(self, recorded_source):
        with pytest.raises(NoSuchVariableError) as caught:
            read_variable_attributes(recorded_source("cmip5-netcdf3-time-bounds"), "t")

        assert caught.value.found[:2] == ("time", "time_bnds")
        assert "'t'" in str(caught.value)

    def test_a_missing_attribute_lists_the_ones_there_are(self, recorded_source):
        """
        The variable is there and does not say what was wanted

        Reported with everything it does say, so a surprising file can be seen for
        what it is rather than guessed about.
        """
        with pytest.raises(MissingVariableAttributesError) as caught:
            read_variable_attributes(
                recorded_source("cmip5-netcdf3-time-bounds"),
                "lat",
                wanted=("units", "calendar"),
            )

        assert caught.value.missing == ("calendar",)
        assert "units" in caught.value.found
        assert "Attributes found: " in str(caught.value)


class TestReadingFirstAndLastTimeBounds:
    def test_they_come_back(self, recorded_source):
        bounds = read_first_and_last_time_bounds(
            recorded_source("cmip5-netcdf3-time-bounds")
        )

        assert bounds.first_bounds == (0.0, 31.0)

    def test_a_file_without_the_time_variable_is_an_error_here(self, recorded_source):
        """
        Asking for time values by name and getting none is not an answer

        [read_file_header][esmporium.headers.read_file_header] says `None` instead,
        because there the time values are one thing among several.
        """
        with pytest.raises(NoSuchVariableError) as caught:
            read_first_and_last_time_bounds(
                recorded_source("cmip5-netcdf3-time-bounds"), time_variable="t"
            )

        assert "tas" in caught.value.found


def test_the_layout_is_read_from_the_bytes_and_not_assumed_from_the_project(
    cmip5_netcdf3_prefix, cmip6_netcdf4_prefix
):
    """
    The trap that makes sniffing mandatory rather than tidy

    Sampled live on 2026-11-04, CMIP5 was classic netCDF for 44 of 47 models and
    netCDF-4 for `EC-EARTH`'s rcp85. So a reader keyed on the project would work
    across most of an archive and then fail, and the failure would look like a corrupt
    file rather than a wrong guess. Both of these are CMIP-era files and they are not
    the same format.
    """
    assert sniff_layout(cmip5_netcdf3_prefix) == HeaderLayout.NETCDF3
    assert sniff_layout(cmip6_netcdf4_prefix) == HeaderLayout.NETCDF4_HDF5
