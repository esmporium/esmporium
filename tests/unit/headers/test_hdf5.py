"""
Tests of reading global attributes out of a netCDF-4/HDF5 header
"""

from __future__ import annotations

import builtins

import pytest
from tests.conftest import CMIP6_FILE_SIZE

from esmporium.headers import (
    HeaderParserUnavailableError,
    TruncatedHeaderError,
    UnreadableHeaderError,
    read_hdf5_global_attributes,
)


def test_a_real_cmip6_header_reads_its_parent_facts(needs_h5py, cmip6_netcdf4_prefix):
    """
    The whole point, against bytes a data node really served

    Unlike CMIP5, CMIP6 publishes the parent simulation completely -- model, era,
    activity, experiment and variant -- so nothing here has to be inferred.
    """
    attributes = read_hdf5_global_attributes(
        cmip6_netcdf4_prefix, file_size=CMIP6_FILE_SIZE
    )

    assert attributes["parent_experiment_id"] == "piControl"
    assert attributes["parent_source_id"] == "TaiESM1"
    assert attributes["parent_variant_label"] == "r1i1p1f1"
    assert attributes["parent_activity_id"] == "CMIP"
    assert attributes["parent_mip_era"] == "CMIP6"
    assert attributes["parent_time_units"] == "days since 0701-01-01"
    assert attributes["branch_time_in_parent"] == 438000.0
    assert attributes["branch_time_in_child"] == 0.0


def test_no_numpy_types_escape(needs_h5py, cmip6_netcdf4_prefix):
    """
    h5py hands back numpy objects, and none of them may reach a caller

    This package does not depend on numpy, and a `numpy.bytes_` compares equal to
    `bytes` while a `numpy.float64` does not round-trip through JSON. Reducing them at
    this boundary is the only place that has to know they exist.
    """
    attributes = read_hdf5_global_attributes(
        cmip6_netcdf4_prefix, file_size=CMIP6_FILE_SIZE
    )

    assert {type(value) for value in attributes.values()} <= {str, int, float, tuple}
    for name, value in attributes.items():
        assert type(value).__module__ == "builtins", name


def test_the_whole_file_size_is_what_hdf5_is_told(needs_h5py, cmip6_netcdf4_prefix):
    """
    Passing the prefix's own length instead of the file's fails outright
    """
    with pytest.raises(UnreadableHeaderError, match="could not read a header"):
        read_hdf5_global_attributes(
            cmip6_netcdf4_prefix, file_size=len(cmip6_netcdf4_prefix)
        )


def test_a_short_prefix_says_how_far_it_got(needs_h5py, cmip6_netcdf4_prefix):
    """
    A read past the prefix is reported, never served as zeros
    """
    with pytest.raises(TruncatedHeaderError) as caught:
        read_hdf5_global_attributes(
            cmip6_netcdf4_prefix[:2000], file_size=CMIP6_FILE_SIZE
        )

    assert caught.value.bytes_given == 2000
    assert caught.value.bytes_needed > 2000


def test_the_attribute_names_are_readable_long_before_their_values(
    needs_h5py,
    cmip6_netcdf4_prefix,
):
    """
    The trap this fixture exists to pin

    HDF5 keeps attribute names in the root group's object header, near the start, and
    their values in a fractal heap much further in -- 1.9 kB versus 42.2 kB on this
    file. So "the prefix lists every attribute" is not evidence that it is long
    enough, and any check that the header is complete has to read a value.
    """
    with pytest.raises(TruncatedHeaderError):
        read_hdf5_global_attributes(
            cmip6_netcdf4_prefix[:4096], file_size=CMIP6_FILE_SIZE
        )


def test_bytes_that_are_not_hdf5_are_refused(needs_h5py):
    with pytest.raises(UnreadableHeaderError, match="could not read a header"):
        read_hdf5_global_attributes(b"CDF\x01" + bytes(256), file_size=260)


def test_a_missing_extra_says_how_to_install_it(monkeypatch, cmip6_netcdf4_prefix):
    """
    Without the `headers` extra, the error names the one command that fixes it

    The important part is that this is *not* reported as a corrupt file or an
    unreadable node. Nothing is wrong with the bytes or whoever served them; something
    is missing locally, and the person running this can fix it.
    """
    real_import = builtins.__import__

    def refuse_h5py(name, *args, **kwargs):
        if name == "h5py":
            msg = "No module named 'h5py'"
            raise ImportError(msg)

        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse_h5py)

    with pytest.raises(HeaderParserUnavailableError) as caught:
        read_hdf5_global_attributes(cmip6_netcdf4_prefix, file_size=CMIP6_FILE_SIZE)

    assert "esmporium[headers]" in str(caught.value)
    assert caught.value.magic == b"\x89HDF\r\n\x1a\n"
