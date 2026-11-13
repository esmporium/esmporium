"""
Tests of reading a time range out of a filename
"""

from __future__ import annotations

import pytest

from esmporium.datasets.no_time_axis import (
    NO_TIME_AXIS_INTERVALS_BY_PROJECT,
    UnknownProjectTimeAxisError,
    has_no_time_axis,
)
from esmporium.datasets.time import Span, TimeAxis, TimeStep, TimeUnits
from esmporium.enrich.known_readers import (
    FILENAME_TIME_READERS_BY_PROJECT,
    UnknownProjectFilenameTimeReaderError,
    UnknownReportingIntervalError,
    read_trailing_range,
    reader_for_project,
    span_of_reporting_interval,
)
from esmporium.enrich.protocol import CalendarRequiredError, UnreadableTimestampError

DAILY_NOLEAP = TimeAxis(TimeUnits(TimeStep.DAYS, "1850-01-01"), "noleap")
HOURLY_NOLEAP = TimeAxis(TimeUnits(TimeStep.HOURS, "1850-01-01"), "noleap")


# ---------------------------------------------------------------- real filenames


@pytest.mark.parametrize(
    ("project", "reporting_interval", "filename", "published", "extent"),
    [
        (
            "CMIP5",
            "mon",
            "tas_Amon_HadGEM2-ES_historical_r1i1p1_185912-188411.nc",
            ("185912", "188411"),
            # December 1859 to November 1884 inclusive, which is twenty-five years
            300,
        ),
        (
            "CMIP5",
            "yr",
            "co2_Ayr_CanESM2_historical_r1i1p1_1850-2005.nc",
            ("1850", "2005"),
            156,
        ),
        (
            "CMIP6",
            "mon",
            "tas_Amon_ACCESS-ESM1-5_historical_r1i1p1f1_gn_185001-201412.nc",
            ("185001", "201412"),
            # 1850 to 2014 inclusive is 165 years
            1980,
        ),
        (
            "CMIP6",
            "mon",
            "tas_Amon_GFDL-ESM4_piControl_r1i1p1f1_gr1_320001-325012.nc",
            ("320001", "325012"),
            612,
        ),
        (
            # A CMIP6-shaped name under its own experiment, because a CMIP6Plus
            # document *is* a CMIP6 document -- same DRS, same facet names, read by
            # CMIP6's naming table with nothing invented. That is pinned against a
            # live document in
            # `tests/integration/requirements/test_cmip6plus_registration.py`; what
            # is pinned here is only that the key reaches a reader.
            "CMIP6Plus",
            "mon",
            "tas_Amon_MIROC6_esm-hist_r1i1p1f1_gn_185001-202012.nc",
            ("185001", "202012"),
            # 1850 to 2020 inclusive is 171 years
            2052,
        ),
    ],
)
def test_reading_a_monthly_or_yearly_filename(
    project, reporting_interval, filename, published, extent
):
    """
    The calendar-free cases, which cost nothing and need nothing installed
    """
    read = reader_for_project(project)(filename, reporting_interval=reporting_interval)

    assert (read.start_raw, read.end_raw) == published
    assert read.to_half_open().extent == extent


def test_a_single_month_file_holds_one_month():
    """
    The case which catches a conversion that subtracted the published values
    """
    read = read_trailing_range(
        "tas_Amon_GFDL-ESM4_piControl_r1i1p1f1_gr1_320001-320001.nc",
        reporting_interval="mon",
    )

    assert read.to_half_open().extent == 1


def test_a_climatology_keeps_its_range_and_is_marked_as_one():
    """
    A climatology appends `-clim`, which is not part of the range
    """
    read = read_trailing_range(
        "tas_Amon_ACCESS-ESM1-5_historical_r1i1p1f1_gn_185001-185012-clim.nc",
        reporting_interval="monC",
    )

    assert (read.start_raw, read.end_raw) == ("185001", "185012")
    assert read.is_climatology
    assert read.to_half_open().extent == 12


def test_a_cmip5_climatology_is_read_the_same_way():
    """
    One reader strips `-clim` for every project

    CMIP5 spells the interval `monClim` where CMIP6 spells it `monC`, but the suffix
    on the filename is the same.
    """
    read = read_trailing_range(
        "difvho_Oclim_HadGEM2-ES_historical_r1i1p1_185001-185012-clim.nc",
        reporting_interval="monClim",
    )

    assert (read.start_raw, read.end_raw) == ("185001", "185012")
    assert read.is_climatology
    assert read.to_half_open().extent == 12


def test_a_monthly_file_is_not_a_climatology():
    """
    The flag says something, so it has to be false when it is false
    """
    read = read_trailing_range(
        "tas_Amon_ACCESS-ESM1-5_historical_r1i1p1f1_gn_185001-185012.nc",
        reporting_interval="mon",
    )

    assert not read.is_climatology


@pytest.mark.parametrize(
    ("project", "filename"),
    [
        ("CMIP5", "areacella_fx_HadGEM2-ES_historical_r0i0p0.nc"),
        ("CMIP6", "areacella_fx_ACCESS-ESM1-5_historical_r1i1p1f1_gn.nc"),
        ("CMIP6Plus", "areacella_fx_MIROC6_esm-hist_r1i1p1f1_gn.nc"),
        # No CMIP7 row: every CMIP7 filename recorded in `tests/test-data/search` is
        # daily, so there is no `fx` one to copy and inventing its branding would be
        # the guess this registry exists to avoid. That CMIP7 reads `fx` as having no
        # time axis is covered by
        # `test_fx_means_no_time_axis_in_every_project_we_know`, which needs no
        # filename.
    ],
)
def test_a_fixed_field_has_no_range_and_says_so_quietly(project, filename):
    """
    `None` rather than an error: there is nothing wrong with an `fx` file
    """
    assert reader_for_project(project)(filename, reporting_interval="fx") is None


def test_a_hyphenated_model_name_is_not_mistaken_for_a_range():
    """
    Only the last field is split on `-`, never the whole name

    `MPI-M-MPI-ESM1-2-HR` and `EUR-12` are both full of hyphens, and a parser which
    split the stem would find a "range" in either. A CORDEX-shaped name because it is
    the worst case to hand; this calls the reader directly, rather than through
    `reader_for_project`, which has no reader for that project.
    """
    assert (
        read_trailing_range(
            "orog_EUR-12_MPI-M-MPI-ESM1-2-HR_historical_r1i1p1f1"
            "_GERICS_REMO2020_v1_fx.nc",
            reporting_interval="fx",
        )
        is None
    )


# ------------------------------------------------------------------ sub-daily data


@pytest.mark.parametrize(
    ("filename", "reporting_interval", "axis", "extent"),
    [
        (
            "tasmax_day_HadGEM2-ES_historical_r1i1p1_19591201-19841130.nc",
            "day",
            DAILY_NOLEAP,
            # Twenty-five noleap years
            25 * 365,
        ),
        (
            "pr_3hr_ACCESS-ESM1-5_historical_r1i1p1f1_gn_185001010300-185101010000.nc",
            "3hr",
            HOURLY_NOLEAP,
            # One noleap year, and the last stamp is the *start* of a three-hour step
            365 * 24,
        ),
        (
            # The same twelve-digit stamp in CMIP5's spelling, which has no grid
            # label and no `f` in its ensemble member. The digit ladder is read off
            # the string, so one reader serves both.
            "pr_3hr_IPSL-CM5A-LR_historical_r1i1p1_197901010300-198001010000.nc",
            "3hr",
            HOURLY_NOLEAP,
            365 * 24,
        ),
        (
            # A real CMIP7 filename, off a recorded ESGF-NG response
            # (`tests/test-data/search/esgf-ng-stac-cmip7-east-search.json`). Its DRS
            # looks nothing like CMIP6's -- a branded variable, a region, a grid label
            # where CMIP6 puts none -- and the range is still the last field, which is
            # why one reader serves every project we know.
            "tas_tavg-h2m-hxy-u_day_glb_g120_CanESM5-1_1pctCO2_r1i1p2f1"
            "_18500101-18601231.nc",
            "day",
            DAILY_NOLEAP,
            # 1850 to 1860 inclusive is eleven noleap years
            11 * 365,
        ),
        (
            # A CORDEX-shaped name, here because its half-hour stamps and
            # hyphen-laden fields are the hardest thing the parser is asked to cope
            # with. Called directly rather than through `reader_for_project`, which
            # has no reader for that project.
            "pr_EUR-12_MPI-M-MPI-ESM1-2-HR_historical_r1i1p1f1_GERICS_REMO2020"
            "_v1_1hr_195001010030-195001312330.nc",
            "1hr",
            HOURLY_NOLEAP,
            31 * 24,
        ),
    ],
)
def test_sub_daily_filenames_measure_exactly_once_a_calendar_is_known(
    filename, reporting_interval, axis, extent
):
    """
    Daily and finer data is supported, it just has to be enriched first

    The half-open end is the last stamp advanced by one *reporting* step, which is
    what makes a `3hr` file whose last stamp is 21:00 run to midnight.
    """
    read = read_trailing_range(filename, reporting_interval=reporting_interval)

    assert read.to_half_open(axis=axis).extent == extent


def test_a_daily_filename_without_a_calendar_says_what_it_needs():
    """
    Two dates are not a length, and this is the ordinary path for daily data
    """
    read = read_trailing_range(
        "tas_day_ACCESS-ESM1-5_ssp245_r1i1p1f1_gn_20150101-21001231.nc",
        reporting_interval="day",
    )

    with pytest.raises(CalendarRequiredError):
        read.to_half_open()


def test_a_sub_daily_filename_is_read_to_the_minute():
    """
    Twelve-digit stamps are the ordinary spelling for sub-daily data
    """
    read = read_trailing_range(
        "pr_3hr_ACCESS-ESM1-5_historical_r1i1p1f1_gn_185001010300-185101010000.nc",
        reporting_interval="3hr",
    )

    assert read.precision == TimeStep.MINUTES


# --------------------------------------------------------------- what we refuse


def test_an_interval_whose_width_we_do_not_know_is_refused():
    """
    `subhr` says the data is finer than hourly without saying how much finer
    """
    with pytest.raises(UnknownReportingIntervalError, match="subhr"):
        read_trailing_range(
            "pr_subhr_X_historical_r1i1p1f1_gn_185001010000-185001020000.nc",
            reporting_interval="subhr",
        )


def test_a_range_written_to_a_precision_we_do_not_read_is_refused():
    """
    Rather than read to the minute and be wrong by whatever the rest said
    """
    with pytest.raises(UnreadableTimestampError):
        read_trailing_range(
            "pr_1hr_X_historical_r1i1p1f1_gn_18500101000000-18500102000000.nc",
            reporting_interval="1hr",
        )


def test_an_unknown_project_has_no_reader_and_no_fallback():
    """
    Where a filename puts its range is a project's own convention
    """
    with pytest.raises(UnknownProjectFilenameTimeReaderError, match="MIP1"):
        reader_for_project("MIP1")


def test_a_project_can_be_added_without_forking_anything():
    """
    The registry idiom `PARENT_HEADER_READERS_BY_PROJECT` already sets
    """

    def read_mip1(filename, *, reporting_interval):
        """Pretend MIP1 writes its range first"""
        start_raw, end_raw = filename.split("_")[0].split("-")

        return read_trailing_range(
            f"var_table_model_experiment_{start_raw}-{end_raw}.nc",
            reporting_interval=reporting_interval,
        )

    reader = reader_for_project(
        "MIP1", readers={**FILENAME_TIME_READERS_BY_PROJECT, "MIP1": read_mip1}
    )
    read = reader("185001-185012_tas_MIP1.nc", reporting_interval="mon")

    assert read.to_half_open().extent == 12


def test_the_two_registries_name_the_same_projects():
    """
    Or a project would read its filenames and then not know what `fx` means

    They are separate mappings because they answer separate questions, and a project
    in one and not the other is a half-registered project: the thing to notice is
    that adding one entry means adding both.
    """
    assert set(FILENAME_TIME_READERS_BY_PROJECT) == set(
        NO_TIME_AXIS_INTERVALS_BY_PROJECT
    )


# ----------------------------------------------------------------- no time axis


@pytest.mark.parametrize("project", sorted(FILENAME_TIME_READERS_BY_PROJECT))
def test_fx_means_no_time_axis_in_every_project_we_know(project):
    """
    Which is what `Covers` refuses against
    """
    assert has_no_time_axis(project, "fx")


@pytest.mark.parametrize("reporting_interval", ["mon", "yr", "day", "3hr", "monC"])
def test_everything_else_has_a_time_axis(reporting_interval):
    """
    Including a climatology, whose twelve values are still on a time axis
    """
    assert not has_no_time_axis("CMIP6", reporting_interval)


def test_an_unknown_project_cannot_be_asked_whether_it_has_a_time_axis():
    """
    `False` would be a claim we have no grounds for
    """
    with pytest.raises(UnknownProjectTimeAxisError, match="MIP1"):
        has_no_time_axis("MIP1", "mon")


# ---------------------------------------------------------- reporting intervals


@pytest.mark.parametrize(
    ("reporting_interval", "expected"),
    [
        ("yr", Span(1, TimeStep.YEARS)),
        ("dec", Span(10, TimeStep.YEARS)),
        ("mon", Span(1, TimeStep.MONTHS)),
        ("monC", Span(1, TimeStep.MONTHS)),
        ("monClim", Span(1, TimeStep.MONTHS)),
        ("day", Span(1, TimeStep.DAYS)),
        ("6hr", Span(6, TimeStep.HOURS)),
        ("3hr", Span(3, TimeStep.HOURS)),
        ("1hr", Span(1, TimeStep.HOURS)),
    ],
)
def test_how_wide_one_reporting_step_is(reporting_interval, expected):
    """
    What makes the half-open end exact at every frequency
    """
    assert span_of_reporting_interval(reporting_interval) == expected


def test_cmip5_and_cmip6_spell_a_monthly_climatology_differently():
    """
    `monClim` and `monC` are the same width, and both have to be known
    """
    assert span_of_reporting_interval("monClim") == span_of_reporting_interval("monC")
