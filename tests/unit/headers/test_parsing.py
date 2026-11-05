"""
Tests of working out what a file is, and reading it accordingly
"""

from __future__ import annotations

import pytest
from tests.conftest import CMIP5_FILE_SIZE, CMIP6_FILE_SIZE

from esmporium.headers import (
    PARSERS_BY_LAYOUT,
    HeaderLayout,
    UnsupportedHeaderFormatError,
    read_global_attributes,
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

    If these ever came apart, `read_global_attributes` would raise `KeyError` for a
    layout it had just successfully identified.
    """
    assert set(PARSERS_BY_LAYOUT) == set(HeaderLayout)


def test_a_real_cmip5_file_is_dispatched_to_the_classic_reader(cmip5_netcdf3_prefix):
    attributes = read_global_attributes(cmip5_netcdf3_prefix, file_size=CMIP5_FILE_SIZE)

    assert attributes["parent_experiment_id"] == "piControl"
    assert attributes["project_id"] == "CMIP5"


def test_a_real_cmip6_file_is_dispatched_to_the_hdf5_reader(cmip6_netcdf4_prefix):
    attributes = read_global_attributes(cmip6_netcdf4_prefix, file_size=CMIP6_FILE_SIZE)

    assert attributes["parent_experiment_id"] == "piControl"
    assert attributes["mip_era"] == "CMIP6"


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
