"""
Tests of reading a classic netCDF file's header, and its first and last time values
"""

from __future__ import annotations

import struct

import pytest

from esmporium.headers import (
    HeaderLayout,
    HeldBytesSource,
    TruncatedHeaderError,
    UnreadableHeaderError,
    read_netcdf3_file_header,
)

from .classic_netcdf import Variable, att_list, build, name, word

NC_ATTRIBUTE = b"\x00\x00\x00\x0c"
NC_DIMENSION = b"\x00\x00\x00\x0a"
ABSENT = b"\x00\x00\x00\x00" * 2
NC_CHAR = b"\x00\x00\x00\x02"
NC_DOUBLE = b"\x00\x00\x00\x06"
NC_INT = b"\x00\x00\x00\x04"


def header_bytes(
    *attributes: bytes, dimensions: bytes = ABSENT, magic: bytes = b"CDF\x01"
) -> bytes:
    """
    Build a classic netCDF header carrying the given global attributes and no variables

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
        A whole header
    """
    if attributes:
        gatt_list = NC_ATTRIBUTE + word(len(attributes)) + b"".join(attributes)
    else:
        gatt_list = ABSENT

    return magic + word(0) + dimensions + gatt_list + ABSENT


def read(raw: bytes, *, time_variable: str | None = None):
    """
    Read a file held whole in memory

    Parameters
    ----------
    raw
        The file

    time_variable
        Passed through

    Returns
    -------
    :
        What the reader returned
    """
    return read_netcdf3_file_header(
        HeldBytesSource(raw, size=len(raw)), time_variable=time_variable
    )


def text_attribute(attribute_name: str, value: str) -> bytes:
    """
    Render one character-valued attribute

    Parameters
    ----------
    attribute_name
        The attribute's name

    value
        Its value

    Returns
    -------
    :
        The attribute's bytes
    """
    encoded = value.encode()

    return (
        name(attribute_name)
        + NC_CHAR
        + word(len(encoded))
        + encoded
        + bytes(-len(encoded) % 4)
    )


def monthly_time(
    n_steps: int, *, type_name: str = "double", bounds: bool = True
) -> list[Variable]:
    """
    Build a monthly time coordinate on the record dimension, with bounds

    Parameters
    ----------
    n_steps
        How many months

    type_name
        The type of the time values

    bounds
        Whether to give it a bounds variable

    Returns
    -------
    :
        `time`, and `time_bnds` if asked for
    """
    attributes: dict[str, object] = {"units": "days since 1850-01-01"}
    if bounds:
        attributes["bounds"] = "time_bnds"

    variables = [
        Variable(
            "time",
            ("time",),
            type_name,
            [15 + 30 * step for step in range(n_steps)],
            attributes,
        )
    ]
    if bounds:
        variables.append(
            Variable(
                "time_bnds",
                ("time", "bnds"),
                type_name,
                [
                    value
                    for step in range(n_steps)
                    for value in (30 * step, 30 * (step + 1))
                ],
            )
        )

    return variables


class TestGlobalAttributes:
    def test_a_character_attribute_reads_as_text(self):
        read_back = read(header_bytes(text_attribute("parent_experiment_id", "pi")))

        assert dict(read_back.global_attributes) == {"parent_experiment_id": "pi"}
        assert read_back.layout == HeaderLayout.NETCDF3

    def test_trailing_nuls_are_padding_rather_than_content(self):
        """
        A writer may declare a round number of characters and NUL-fill the rest

        Those NULs are inside the declared length, so they are not the four-byte
        padding and have to be stripped separately.
        """
        read_back = read(
            header_bytes(name("model_id") + NC_CHAR + word(8) + b"bcc" + bytes(5))
        )

        assert dict(read_back.global_attributes) == {"model_id": "bcc"}

    def test_a_single_numeric_value_is_not_wrapped_in_a_tuple(self):
        """
        A scalar comes back as a number

        CMIP5's `branch_time` is one of these, and a caller that had to unwrap a
        one-element tuple is a caller that will one day forget to.
        """
        read_back = read(
            header_bytes(
                name("branch_time") + NC_DOUBLE + word(1) + struct.pack(">d", 240.0)
            )
        )

        assert dict(read_back.global_attributes) == {"branch_time": 240.0}

    def test_several_numeric_values_come_back_as_a_tuple(self):
        read_back = read(
            header_bytes(
                name("counts") + NC_INT + word(3) + word(1) + word(2) + word(3)
            )
        )

        assert dict(read_back.global_attributes) == {"counts": (1, 2, 3)}

    def test_dimensions_are_stepped_over_to_reach_the_attributes(self):
        dimensions = (
            NC_DIMENSION + word(2) + name("time") + word(0) + name("lat") + word(64)
        )

        read_back = read(
            header_bytes(text_attribute("title", "hi"), dimensions=dimensions)
        )

        assert dict(read_back.global_attributes) == {"title": "hi"}

    def test_no_global_attributes_is_an_empty_answer_not_a_failure(self):
        assert dict(read(header_bytes()).global_attributes) == {}


class TestVariables:
    def test_every_variable_s_attributes_are_read_in_order(self):
        """
        The time variable's `units` and `calendar` live here, not in the globals
        """
        built = build(
            {"time": 0, "lat": 2},
            [
                Variable(
                    "time",
                    ("time",),
                    "double",
                    [0.5],
                    {"units": "days since 1850-01-01", "calendar": "noleap"},
                ),
                Variable("lat", ("lat",), "float", [-45.0, 45.0], {"units": "deg"}),
                Variable("plain", (), "int", [7]),
            ],
            global_attributes={"title": "hi"},
        )

        read_back = read(built.whole())

        assert list(read_back.variable_attributes) == ["time", "lat", "plain"]
        assert read_back.variable_attributes["time"] == {
            "units": "days since 1850-01-01",
            "calendar": "noleap",
        }
        assert read_back.variable_attributes["plain"] == {}
        assert dict(read_back.global_attributes) == {"title": "hi"}

    def test_no_time_values_are_read_unless_asked_for(self):
        """
        Only the header is held here, so asking for a value would raise
        """
        built = build({"time": 0, "bnds": 2}, monthly_time(12))
        # Twelve records of one double and two doubles each, at the end of the file.
        held_header = built.whole()[: built.size - 12 * (8 + 16)]
        read_back = read_netcdf3_file_header(
            HeldBytesSource(held_header, size=built.size)
        )

        assert read_back.time_bounds is None

    def test_a_file_without_the_time_variable_has_no_time_bounds(self):
        built = build({"x": 2}, [Variable("orog", ("x",), "float", [1.0, 2.0])])

        assert read(built.whole(), time_variable="time").time_bounds is None

    @pytest.mark.parametrize(
        "variable_list",
        [
            pytest.param(word(0x0C) + word(1), id="an-attribute-tag"),
            pytest.param(word(0) + word(3), id="absent-with-entries"),
        ],
    )
    def test_a_mangled_variable_list_tag_is_refused(self, variable_list):
        broken = b"CDF\x01" + word(0) + ABSENT + ABSENT + variable_list

        with pytest.raises(UnreadableHeaderError, match="variable list"):
            read(broken)

    def test_a_variable_naming_a_missing_dimension_is_refused(self):
        variable = name("t") + word(1) + word(5) + att_list({}) + word(6) + word(8)
        broken = (
            b"CDF\x01"
            + word(0)
            + ABSENT
            + ABSENT
            + word(0x0B)
            + word(1)
            + variable
            + word(0)
        )

        with pytest.raises(UnreadableHeaderError, match="names dimension 5"):
            read(broken)


class TestTimeBounds:
    @pytest.mark.parametrize("magic", [b"CDF\x01", b"CDF\x02"])
    def test_first_and_last_come_from_the_ends_of_the_records(self, magic):
        """
        A record variable's values are spread across records

        Record `r` of a variable is at `begin + r * (record size)`. `CDF\\x02` differs
        only in `begin` being eight bytes, so both magics give the same answer.
        """
        built = build({"time": 0, "bnds": 2}, monthly_time(24), magic=magic)

        bounds = read(built.whole(), time_variable="time").time_bounds

        assert bounds is not None
        assert bounds.n_steps == 24
        assert (bounds.first_value, bounds.last_value) == (15.0, 15.0 + 30 * 23)
        assert bounds.bounds_variable == "time_bnds"
        assert bounds.first_bounds == (0.0, 30.0)
        assert bounds.last_bounds == (30.0 * 23, 30.0 * 24)

    def test_records_are_padded_when_there_are_several_record_variables(self):
        """
        Two-byte values make a slab that is not a multiple of four, so padding shows

        Each slab is padded to four bytes, so record `r` starts `r * 8` bytes in
        (four for `time`, four for `time_bnds`), not `r * 6`.
        """
        built = build({"time": 0, "bnds": 2}, monthly_time(5, type_name="short"))

        bounds = read(built.whole(), time_variable="time").time_bounds

        assert bounds is not None
        assert (bounds.first_value, bounds.last_value) == (15, 135)
        assert bounds.last_bounds == (120, 150)

    def test_a_single_record_variable_is_not_padded(self):
        """
        The specification drops record padding when there is one record variable

        So two-byte values sit two bytes apart, and reading as if padded would give
        the wrong last value.
        """
        built = build({"time": 0}, monthly_time(5, type_name="short", bounds=False))
        assert built.size == len(built.whole())

        bounds = read(built.whole(), time_variable="time").time_bounds

        assert bounds is not None
        assert (bounds.first_value, bounds.last_value) == (15, 135)
        assert bounds.first_bounds is None
        assert bounds.bounds_variable is None

    def test_a_fixed_length_time_dimension_is_read_too(self):
        built = build({"time": 3, "bnds": 2}, monthly_time(3))

        bounds = read(built.whole(), time_variable="time").time_bounds

        assert bounds is not None
        assert bounds.n_steps == 3
        assert bounds.last_bounds == (60.0, 90.0)

    def test_a_streaming_record_count_is_worked_out_from_the_file_size(self):
        built = build({"time": 0, "bnds": 2}, monthly_time(7), streaming=True)

        bounds = read(built.whole(), time_variable="time").time_bounds

        assert bounds is not None
        assert bounds.n_steps == 7
        assert bounds.last_value == 15.0 + 30 * 6

    def test_data_beyond_four_gigabytes_is_reached_with_a_64_bit_offset(self):
        """
        `CDF\\x02` exists for this: a `begin` too large for four bytes
        """
        beyond = 5 * 1024**3
        built = build(
            {"time": 0, "bnds": 2},
            monthly_time(4),
            magic=b"CDF\x02",
            data_starts_at=beyond,
        )
        assert min(built.pieces) == 0
        assert max(built.pieces) == beyond

        bounds = read_netcdf3_file_header(
            built.source(), time_variable="time"
        ).time_bounds

        assert bounds is not None
        assert bounds.last_bounds == (90.0, 120.0)

    def test_bounds_naming_a_missing_variable_are_refused(self):
        variables = monthly_time(3, bounds=False)
        variables[0].attributes["bounds"] = "nowhere"
        built = build({"time": 0}, variables)

        with pytest.raises(UnreadableHeaderError, match="'nowhere'"):
            read(built.whole(), time_variable="time")

    def test_a_time_variable_with_no_values_is_refused(self):
        built = build(
            {"time": 0},
            [Variable("time", ("time",), "double", [], {"units": "days since 1"})],
        )

        with pytest.raises(UnreadableHeaderError, match="has no values"):
            read(built.whole(), time_variable="time")

    def test_time_held_as_characters_is_refused(self):
        built = build(
            {"time": 0},
            [Variable("time", ("time",), "char", [b"a", b"b"])],
        )

        with pytest.raises(UnreadableHeaderError, match="holds characters"):
            read(built.whole(), time_variable="time")

    def test_reading_a_value_not_held_says_so(self):
        """
        The header is held and the last record is not, which is a truncation
        """
        built = build({"time": 0, "bnds": 2}, monthly_time(12))
        held = built.whole()[: built.size - 24]

        with pytest.raises(TruncatedHeaderError) as caught:
            read_netcdf3_file_header(
                HeldBytesSource(held, size=built.size), time_variable="time"
            )

        assert caught.value.bytes_needed > len(held)


class TestRefusals:
    def test_a_short_prefix_says_how_far_it_got(self):
        """
        Too few bytes is a different problem from bad bytes

        One is fixed by fetching more and the other by trying another mirror, so a
        parser that conflated them would send the caller to do the wrong thing.
        """
        whole = header_bytes(text_attribute("title", "hi"))

        with pytest.raises(TruncatedHeaderError) as caught:
            read_netcdf3_file_header(HeldBytesSource(whole[:10], size=len(whole)))

        assert caught.value.bytes_needed > 10
        assert "lower bound" in str(caught.value)

    def test_a_file_that_is_not_classic_netcdf_is_refused(self):
        with pytest.raises(UnreadableHeaderError, match="not a classic netCDF file"):
            read(b"\x89HDF\r\n\x1a\n" + bytes(64))

    def test_an_undefined_attribute_type_is_refused_rather_than_guessed(self):
        with pytest.raises(
            UnreadableHeaderError, match="is not one the classic netCDF format defines"
        ):
            read(header_bytes(name("odd") + word(99) + word(1) + word(0)))

    def test_a_mangled_attribute_list_tag_is_refused(self):
        broken = b"CDF\x01" + word(0) + ABSENT + word(7) + word(2)

        with pytest.raises(UnreadableHeaderError, match="global attribute list"):
            read(broken)


class TestARealCmip5File:
    def test_a_prefix_ending_with_the_globals_is_too_short(self, cmip5_netcdf3_prefix):
        """
        The globals end at 6,300 bytes and the variables follow, so a read which
        stops at the globals is a truncated read
        """
        with pytest.raises(TruncatedHeaderError):
            read_netcdf3_file_header(
                HeldBytesSource(cmip5_netcdf3_prefix, size=368702896)
            )

    def test_its_header(self, recorded_source):
        read_back = read_netcdf3_file_header(
            recorded_source("cmip5-netcdf3-time-bounds")
        )

        attributes = read_back.global_attributes
        assert attributes["parent_experiment_id"] == "piControl"
        assert attributes["parent_experiment_rip"] == "r1i1p1"
        assert attributes["branch_time"] == 240.0
        assert attributes["model_id"] == "bcc-csm1-1-m"
        assert attributes["project_id"] == "CMIP5"
        assert not [each for each in attributes if each.startswith("parent_source")]

        assert read_back.variable_attributes["time"]["calendar"] == "noleap"
        assert read_back.variable_attributes["time"]["units"] == (
            "days since 0240-01-01"
        )
        assert list(read_back.variable_attributes) == [
            "time",
            "time_bnds",
            "lat",
            "lat_bnds",
            "lon",
            "lon_bnds",
            "height",
            "tas",
        ]

    def test_its_first_and_last_time_bounds(self, recorded_source):
        """
        `024001-038912`: 150 years of months on a 365-day calendar

        So the data runs from day 0 to day 150 * 365 = 54,750 after 0240-01-01.
        """
        bounds = read_netcdf3_file_header(
            recorded_source("cmip5-netcdf3-time-bounds"), time_variable="time"
        ).time_bounds

        assert bounds is not None
        assert bounds.n_steps == 150 * 12
        assert bounds.first_bounds == (0.0, 31.0)
        assert bounds.last_bounds == (54719.0, 150 * 365.0)
        assert (bounds.first_value, bounds.last_value) == (15.5, 54734.5)
