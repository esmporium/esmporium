"""
Tests of reading global attributes out of a classic netCDF header
"""

from __future__ import annotations

import struct

import pytest

from esmporium.headers import (
    TruncatedHeaderError,
    UnreadableHeaderError,
    read_netcdf3_global_attributes,
)

NC_ATTRIBUTE = b"\x00\x00\x00\x0c"
NC_DIMENSION = b"\x00\x00\x00\x0a"
ABSENT = b"\x00\x00\x00\x00" * 2
NC_CHAR = b"\x00\x00\x00\x02"
NC_DOUBLE = b"\x00\x00\x00\x06"
NC_INT = b"\x00\x00\x00\x04"


def word(value: int) -> bytes:
    """
    Render a four-byte big-endian unsigned integer, as the format stores every count

    Parameters
    ----------
    value
        The number to render

    Returns
    -------
    :
        Its four bytes
    """
    return value.to_bytes(4, "big")


def padded(raw: bytes) -> bytes:
    """
    Pad bytes out to a four-byte boundary, as the format pads names and values

    Parameters
    ----------
    raw
        The bytes to pad

    Returns
    -------
    :
        `raw`, followed by enough NULs to reach a multiple of four
    """
    return raw + bytes(-len(raw) % 4)


def named(name: str) -> bytes:
    """
    Render a length-prefixed, padded name

    Parameters
    ----------
    name
        The name to render

    Returns
    -------
    :
        Its bytes, as the format stores a name
    """
    return word(len(name)) + padded(name.encode())


def header(
    *attributes: bytes, dimensions: bytes = ABSENT, magic: bytes = b"CDF\x01"
) -> bytes:
    """
    Build a classic netCDF header carrying the given global attributes

    Parameters
    ----------
    attributes
        Already-rendered attribute entries

    dimensions
        The dimension list, absent by default

    magic
        The file's first four bytes

    Returns
    -------
    :
        A header, up to and including its global attributes
    """
    if attributes:
        gatt_list = NC_ATTRIBUTE + word(len(attributes)) + b"".join(attributes)
    else:
        gatt_list = ABSENT

    return magic + word(0) + dimensions + gatt_list


def text_attribute(name: str, value: str) -> bytes:
    """
    Render one character-valued global attribute

    Parameters
    ----------
    name
        The attribute's name

    value
        Its value

    Returns
    -------
    :
        The attribute's bytes
    """
    encoded = value.encode()

    return named(name) + NC_CHAR + word(len(encoded)) + padded(encoded)


def test_a_character_attribute_reads_as_text():
    attributes = read_netcdf3_global_attributes(
        header(text_attribute("parent_experiment_id", "piControl"))
    )

    assert dict(attributes) == {"parent_experiment_id": "piControl"}


def test_both_classic_magics_are_accepted():
    """
    The two versions differ only after the global attributes

    `CDF\\x02` widens a *variable's* data offset, and variables come after the global
    attributes, so this parser never reaches the difference. Worth pinning, because
    the two looking interchangeable here is a conclusion rather than an accident.
    """
    for magic in (b"CDF\x01", b"CDF\x02"):
        attributes = read_netcdf3_global_attributes(
            header(text_attribute("title", "hi"), magic=magic)
        )

        assert dict(attributes) == {"title": "hi"}


def test_trailing_nuls_are_padding_rather_than_content():
    """
    A writer may declare a round number of characters and NUL-fill the rest

    Those NULs are inside the declared length, so they are not the four-byte padding
    and have to be stripped separately.
    """
    attributes = read_netcdf3_global_attributes(
        header(named("model_id") + NC_CHAR + word(8) + b"bcc\x00\x00\x00\x00\x00")
    )

    assert dict(attributes) == {"model_id": "bcc"}


def test_a_single_numeric_value_is_not_wrapped_in_a_tuple():
    """
    A scalar comes back as a number

    CMIP5's `branch_time` is one of these, and a caller that had to unwrap a
    one-element tuple is a caller that will one day forget to.
    """
    attributes = read_netcdf3_global_attributes(
        header(named("branch_time") + NC_DOUBLE + word(1) + struct.pack(">d", 240.0))
    )

    assert dict(attributes) == {"branch_time": 240.0}


def test_several_numeric_values_come_back_as_a_tuple():
    attributes = read_netcdf3_global_attributes(
        header(named("counts") + NC_INT + word(3) + word(1) + word(2) + word(3))
    )

    assert dict(attributes) == {"counts": (1, 2, 3)}


def test_dimensions_are_stepped_over_to_reach_the_attributes():
    """
    A dimension's name is variable-length, so it has to be parsed to get past it

    Every real file has dimensions, so a parser that only worked without them would
    pass every hand-made test and nothing else.
    """
    dimensions = (
        NC_DIMENSION + word(2) + named("time") + word(0) + named("lat") + word(64)
    )

    attributes = read_netcdf3_global_attributes(
        header(text_attribute("title", "hi"), dimensions=dimensions)
    )

    assert dict(attributes) == {"title": "hi"}


def test_no_global_attributes_is_an_empty_answer_not_a_failure():
    assert dict(read_netcdf3_global_attributes(header())) == {}


def test_a_short_prefix_says_how_far_it_got():
    """
    Too few bytes is a different problem from bad bytes

    One is fixed by fetching more and the other by trying another mirror, so a parser
    that conflated them would send the caller to do the wrong thing.
    """
    whole = header(text_attribute("title", "hi"))

    with pytest.raises(TruncatedHeaderError) as caught:
        read_netcdf3_global_attributes(whole[:10])

    assert caught.value.bytes_given == 10
    assert caught.value.bytes_needed > 10
    assert "lower bound" in str(caught.value)


def test_a_file_that_is_not_classic_netcdf_is_refused():
    with pytest.raises(UnreadableHeaderError, match="not a classic netCDF file"):
        read_netcdf3_global_attributes(b"\x89HDF\r\n\x1a\n" + bytes(64))


def test_an_undefined_attribute_type_is_refused_rather_than_guessed():
    with pytest.raises(UnreadableHeaderError, match="does not define"):
        read_netcdf3_global_attributes(
            header(named("odd") + word(99) + word(1) + word(0))
        )


def test_a_mangled_attribute_list_tag_is_refused():
    broken = b"CDF\x01" + word(0) + ABSENT + word(7) + word(2)

    with pytest.raises(UnreadableHeaderError, match="global attribute list"):
        read_netcdf3_global_attributes(broken)


def test_a_real_cmip5_header_reads_its_parent_facts(cmip5_netcdf3_prefix):
    """
    The whole point, against bytes a data node really served
    """
    attributes = read_netcdf3_global_attributes(cmip5_netcdf3_prefix)

    assert attributes["parent_experiment_id"] == "piControl"
    assert attributes["parent_experiment_rip"] == "r1i1p1"
    assert attributes["branch_time"] == 240.0
    assert attributes["model_id"] == "bcc-csm1-1-m"
    assert attributes["experiment_id"] == "abrupt4xCO2"
    assert attributes["project_id"] == "CMIP5"

    assert not [name for name in attributes if name.startswith("parent_source")]
    assert not [name for name in attributes if name.startswith("parent_mip")]
    assert not [name for name in attributes if name.startswith("parent_activity")]
