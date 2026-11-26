"""
Tests of reading a netCDF-4/HDF5 file's header, and its first and last time values
"""

from __future__ import annotations

import builtins

import pytest
from tests.conftest import CMIP6_FILE_SIZE

from esmporium.headers import (
    BoundsAttribute,
    HeaderLayout,
    HeaderReaderUnavailableError,
    HeldBytesSource,
    TruncatedHeaderError,
    UnreadableHeaderError,
    read_hdf5_file_header,
)


def test_a_real_cmip6_header_reads_its_parent_facts(
    needs_h5netcdf, cmip6_netcdf4_prefix
):
    """
    The whole point, against bytes a data node really served

    Unlike CMIP5, CMIP6 publishes the parent simulation completely -- model, era,
    activity, experiment and variant -- so nothing here has to be inferred.
    """
    header = read_hdf5_file_header(
        HeldBytesSource(cmip6_netcdf4_prefix, size=CMIP6_FILE_SIZE)
    )

    attributes = header.global_attributes
    assert attributes["parent_experiment_id"] == "piControl"
    assert attributes["parent_source_id"] == "TaiESM1"
    assert attributes["parent_variant_label"] == "r1i1p1f1"
    assert attributes["parent_activity_id"] == "CMIP"
    assert attributes["parent_mip_era"] == "CMIP6"
    assert attributes["parent_time_units"] == "days since 0701-01-01"
    assert attributes["branch_time_in_parent"] == 438000.0
    assert attributes["branch_time_in_child"] == 0.0
    assert header.layout == HeaderLayout.NETCDF4_HDF5


def test_every_variable_s_attributes_are_in_the_same_bytes(
    needs_h5netcdf, cmip6_netcdf4_prefix
):
    """
    The calendar is an attribute of the time variable, and it costs no more to read

    And the netCDF data model's own attributes come back, not HDF5's bookkeeping
    (`CLASS`, `NAME`, `REFERENCE_LIST`, `_Netcdf4Dimid`).
    """
    header = read_hdf5_file_header(
        HeldBytesSource(cmip6_netcdf4_prefix, size=CMIP6_FILE_SIZE)
    )

    assert list(header.variable_attributes) == [
        "time",
        "time_bnds",
        "lat",
        "lat_bnds",
        "lon",
        "lon_bnds",
        "height",
        "tas",
    ]
    assert header.variable_attributes["time"] == {
        "bounds": "time_bnds",
        "units": "days since 0001-01-01 00:00:00",
        "calendar": "noleap",
        "axis": "T",
        "long_name": "time",
        "standard_name": "time",
    }
    assert header.variable_attributes["time_bnds"] == {}
    bookkeeping = {"CLASS", "NAME", "REFERENCE_LIST", "DIMENSION_LIST"}
    for attributes in header.variable_attributes.values():
        assert not bookkeeping & set(attributes)
        assert not [each for each in attributes if each.startswith("_Netcdf4")]


def test_no_numpy_types_escape(needs_h5netcdf, cmip6_netcdf4_prefix):
    """
    `h5netcdf` hands back numpy objects, and none of them may reach a caller

    This package does not depend on numpy, and a `numpy.bytes_` compares equal to
    `bytes` while a `numpy.float64` does not round-trip through JSON. Reducing them at
    this boundary is the only place that has to know they exist.
    """
    header = read_hdf5_file_header(
        HeldBytesSource(cmip6_netcdf4_prefix, size=CMIP6_FILE_SIZE)
    )

    values = list(header.global_attributes.values())
    for attributes in header.variable_attributes.values():
        values.extend(attributes.values())

    for value in values:
        assert type(value).__module__ == "builtins", repr(value)


def test_the_whole_file_size_is_what_hdf5_is_told(needs_h5netcdf, cmip6_netcdf4_prefix):
    """
    A size that disagrees with the superblock fails outright
    """
    with pytest.raises(UnreadableHeaderError, match="could not read a header"):
        read_hdf5_file_header(
            HeldBytesSource(cmip6_netcdf4_prefix, size=len(cmip6_netcdf4_prefix))
        )


def test_the_attribute_names_are_readable_long_before_their_values(
    needs_h5netcdf,
    cmip6_netcdf4_prefix,
):
    """
    The trap this fixture exists to pin

    HDF5 keeps attribute names in the root group's object header, near the start, and
    their values in a fractal heap much further in -- 1.9 kB versus 42.2 kB on this
    file. So "the prefix lists every attribute" is not evidence that it is long
    enough, and a read past what is held is reported, never served as zeros.
    """
    with pytest.raises(TruncatedHeaderError) as caught:
        read_hdf5_file_header(
            HeldBytesSource(cmip6_netcdf4_prefix[:4096], size=CMIP6_FILE_SIZE)
        )

    assert caught.value.bytes_needed > 4096


def test_bytes_that_are_not_hdf5_are_refused(needs_h5netcdf):
    raw = b"CDF\x01" + bytes(256)

    with pytest.raises(UnreadableHeaderError, match="could not read a header"):
        read_hdf5_file_header(HeldBytesSource(raw, size=len(raw)))


def test_a_missing_extra_says_how_to_install_it(monkeypatch, cmip6_netcdf4_prefix):
    """
    Without the `headers` extra, the error names the one command that fixes it

    The important part is that this is *not* reported as a corrupt file or an
    unreadable node. Nothing is wrong with the bytes or whoever served them; something
    is missing locally, and the person running this can fix it.
    """
    real_import = builtins.__import__

    def refuse_h5netcdf(name, *args, **kwargs):
        if name == "h5netcdf":
            msg = "No module named 'h5netcdf'"
            raise ImportError(msg)

        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse_h5netcdf)

    with pytest.raises(HeaderReaderUnavailableError) as caught:
        read_hdf5_file_header(
            HeldBytesSource(cmip6_netcdf4_prefix, size=CMIP6_FILE_SIZE)
        )

    assert "esmporium[headers]" in str(caught.value)
    assert caught.value.magic == b"\x89HDF\r\n\x1a\n"


class TestTimeBounds:
    def test_a_cmip6_file_as_cmor_wrote_it(self, needs_h5netcdf, recorded_source):
        """
        `000101-015012`: 150 years of months on a 365-day calendar

        CMOR writes one time value per chunk, so the two ends sit far apart in the
        file -- here 128 MB and 216 MB in -- and each is its own read.
        """
        bounds = read_hdf5_file_header(
            recorded_source("cmip6-netcdf4-time-bounds"), time_variable="time"
        ).time_bounds

        assert bounds is not None
        assert bounds.n_steps == 150 * 12
        assert bounds.bounds_attribute is BoundsAttribute.BOUNDS
        assert bounds.bounds_variable == "time_bnds"
        assert bounds.climatology_bounds is None
        assert bounds.first_bounds == (0.0, 31.0)
        assert bounds.last_bounds == (54719.0, 150 * 365.0)
        assert (bounds.first_value, bounds.last_value) == (15.5, 54734.5)

    def test_a_cmip7_file(self, needs_h5netcdf, recorded_source):
        """
        `18500101-18601231`: eleven years of days on a 365-day calendar
        """
        header = read_hdf5_file_header(
            recorded_source("cmip7-netcdf4-time-bounds"), time_variable="time"
        )

        assert header.variable_attributes["time"]["calendar"] == "365_day"
        bounds = header.time_bounds
        assert bounds is not None
        assert bounds.n_steps == 11 * 365
        assert bounds.first_bounds == (0.0, 1.0)
        assert bounds.last_bounds == (4014.0, 11 * 365.0)

    def test_a_climatological_statistic_has_its_climatology_bounds_read(
        self, needs_h5netcdf, recorded_source
    ):
        """
        A monthly mean of daily minima names its bounds with `climatology`

        `202201-204912` on a 360-day calendar: one value per month. CF names a
        statistic computed in two stages over time this way, and the reader reads its
        bounds all the same, saying which attribute named them, because what they mean
        is a question for whoever interprets them. Because these bounds are stored in
        one piece, every pair is read too, at no extra cost.
        """
        header = read_hdf5_file_header(
            recorded_source("cmip7-netcdf4-climatology"), time_variable="time"
        )

        assert header.variable_attributes["time"]["climatology"] == "climatology_bnds"
        assert "bounds" not in header.variable_attributes["time"]
        bounds = header.time_bounds
        assert bounds is not None
        assert bounds.bounds_attribute is BoundsAttribute.CLIMATOLOGY
        assert bounds.bounds_variable == "climatology_bnds"
        assert bounds.n_steps == 28 * 12
        # 2022-01-01 and 2050-01-01 on a 360-day calendar counting from 1850.
        assert bounds.first_bounds == (172 * 360.0, 172 * 360.0 + 30)
        assert bounds.last_bounds == (200 * 360.0 - 30, 200 * 360.0)
        # Stored in one chunk, so every pair came with the first and last: one month
        # each, meeting end to end.
        assert bounds.climatology_bounds == tuple(
            (172 * 360.0 + 30 * step, 172 * 360.0 + 30 * (step + 1))
            for step in range(28 * 12)
        )

    def test_no_time_values_are_read_unless_asked_for(
        self, needs_h5netcdf, cmip6_netcdf4_prefix
    ):
        """
        The prefix holds the header and not the time chunks, so asking would raise
        """
        header = read_hdf5_file_header(
            HeldBytesSource(cmip6_netcdf4_prefix, size=CMIP6_FILE_SIZE)
        )

        assert header.time_bounds is None

    def test_time_values_not_held_are_a_truncation(
        self, needs_h5netcdf, cmip6_netcdf4_prefix
    ):
        with pytest.raises(TruncatedHeaderError):
            read_hdf5_file_header(
                HeldBytesSource(cmip6_netcdf4_prefix, size=CMIP6_FILE_SIZE),
                time_variable="time",
            )
