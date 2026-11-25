"""
Tests of reading a time range out of a filename
"""

from __future__ import annotations

import pytest

from esmporium.datasets.no_time_axis import NO_TIME_AXIS_INTERVALS_BY_PROJECT
from esmporium.datasets.time import TimeDelta, TimeRange, TimeStep
from esmporium.enrich.known_readers import (
    CLIMATOLOGY_SUFFIX,
    FILENAME_TIME_READERS_BY_PROJECT,
    UnknownProjectFilenameTimeReaderError,
    UnknownReportingIntervalError,
    delta_of_reporting_interval,
    read_trailing_range,
    reader_for_project,
    step_of_stamp,
)
from esmporium.enrich.protocol import (
    CalendarRequiredError,
    PublishedRange,
    UnreadableTimestampError,
)


def half_open(read: PublishedRange, reporting_interval: str) -> TimeRange:
    """
    Get the natural half-open range of what a filename published

    Parameters
    ----------
    read
        What the filename published

    reporting_interval
        The dataset's reporting interval, which gives the width of the last step

    Returns
    -------
    :
        The range, at the precision its start stamp is written to
    """
    return read.to_half_open(
        precision=step_of_stamp(read.start_raw),
        reports_every=delta_of_reporting_interval(reporting_interval),
    )


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
    read = reader_for_project(project)(filename)

    assert (read.start_raw, read.end_raw) == published
    assert half_open(read, reporting_interval).extent.count == extent


def test_a_single_month_file_holds_one_month():
    """
    The case which catches a conversion that subtracted the published values
    """
    read = read_trailing_range(
        "tas_Amon_GFDL-ESM4_piControl_r1i1p1f1_gr1_320001-320001.nc"
    )

    assert half_open(read, "mon").extent == TimeDelta(1, TimeStep.MONTHS)


def test_a_climatology_keeps_its_range_and_its_suffix():
    """
    A CMIP climatology appends `-clim`, which is not part of the range
    """
    read = read_trailing_range(
        "tas_Amon_ACCESS-ESM1-5_historical_r1i1p1f1_gn_185001-185012-clim.nc"
    )

    assert (read.start_raw, read.end_raw) == ("185001", "185012")
    assert read.suffix == CLIMATOLOGY_SUFFIX
    assert half_open(read, "monC").extent == TimeDelta(12, TimeStep.MONTHS)


def test_a_cmip5_climatology_is_read_the_same_way():
    """
    One reader strips `-clim` for every project

    CMIP5 spells the interval `monClim` where CMIP6 spells it `monC`, but the suffix
    on the filename is the same.
    """
    read = read_trailing_range(
        "difvho_Oclim_HadGEM2-ES_historical_r1i1p1_185001-185012-clim.nc"
    )

    assert (read.start_raw, read.end_raw) == ("185001", "185012")
    assert read.suffix == CLIMATOLOGY_SUFFIX
    assert half_open(read, "monClim").extent == TimeDelta(12, TimeStep.MONTHS)


def test_a_monthly_file_has_no_suffix():
    """
    `None` when nothing follows the range
    """
    read = read_trailing_range(
        "tas_Amon_ACCESS-ESM1-5_historical_r1i1p1f1_gn_185001-185012.nc"
    )

    assert read.suffix is None


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
    assert reader_for_project(project)(filename) is None


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
            "_GERICS_REMO2020_v1_fx.nc"
        )
        is None
    )


# ------------------------------------------------------------------ sub-daily data


@pytest.mark.parametrize(
    ("filename", "reporting_interval", "published", "precision"),
    [
        (
            "tasmax_day_HadGEM2-ES_historical_r1i1p1_19591201-19841130.nc",
            "day",
            ("19591201", "19841130"),
            TimeStep.DAYS,
        ),
        (
            "pr_3hr_ACCESS-ESM1-5_historical_r1i1p1f1_gn_185001010300-185101010000.nc",
            "3hr",
            ("185001010300", "185101010000"),
            TimeStep.MINUTES,
        ),
        (
            # The same twelve-digit stamp in CMIP5's spelling, which has no grid
            # label and no `f` in its ensemble member. The digit ladder is read off
            # the string, so one reader serves both.
            "pr_3hr_IPSL-CM5A-LR_historical_r1i1p1_197901010300-198001010000.nc",
            "3hr",
            ("197901010300", "198001010000"),
            TimeStep.MINUTES,
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
            ("18500101", "18601231"),
            TimeStep.DAYS,
        ),
        (
            # A CORDEX-shaped name, here because its half-hour stamps and
            # hyphen-laden fields are the hardest thing the parser is asked to cope
            # with. Called directly rather than through `reader_for_project`, which
            # has no reader for that project.
            "pr_EUR-12_MPI-M-MPI-ESM1-2-HR_historical_r1i1p1f1_GERICS_REMO2020"
            "_v1_1hr_195001010030-195001312330.nc",
            "1hr",
            ("195001010030", "195001312330"),
            TimeStep.MINUTES,
        ),
    ],
)
def test_sub_monthly_filenames_are_read_and_not_placed_on_an_axis(
    filename, reporting_interval, published, precision
):
    """
    The stamps are read as written, and there is no natural axis to put them on

    An exact dated range comes from a file's time bounds, so the filename is not
    padded out to a date to stand in for them.
    """
    read = read_trailing_range(filename)

    assert (read.start_raw, read.end_raw) == published
    assert step_of_stamp(read.start_raw) is precision
    with pytest.raises(CalendarRequiredError):
        half_open(read, reporting_interval)


# ----------------------------------------------------------------- stamp precision


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


# --------------------------------------------------------------- what we refuse


def test_an_interval_whose_width_we_do_not_know_is_refused():
    """
    `subhr` says the data is finer than hourly without saying how much finer
    """
    with pytest.raises(UnknownReportingIntervalError, match="subhr"):
        delta_of_reporting_interval("subhr")


def test_a_range_written_to_a_precision_we_do_not_read_is_kept_raw():
    """
    The reader keeps the stamps as written, and reading their precision refuses them
    rather than reading to the minute and being wrong by whatever the rest said
    """
    read = read_trailing_range(
        "pr_1hr_X_historical_r1i1p1f1_gn_18500101000000-18500102000000.nc"
    )

    assert read.start_raw == "18500101000000"
    with pytest.raises(UnreadableTimestampError):
        step_of_stamp(read.start_raw)


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

    def read_mip1(filename):
        """Pretend MIP1 writes its range first"""
        start_raw, end_raw = filename.split("_")[0].split("-")

        return PublishedRange(start_raw, end_raw)

    reader = reader_for_project(
        "MIP1", readers={**FILENAME_TIME_READERS_BY_PROJECT, "MIP1": read_mip1}
    )
    read = reader("185001-185012_tas_MIP1.nc")

    assert half_open(read, "mon").extent == TimeDelta(12, TimeStep.MONTHS)


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


# ---------------------------------------------------------- reporting intervals


@pytest.mark.parametrize(
    ("reporting_interval", "expected"),
    [
        ("yr", TimeDelta(1, TimeStep.YEARS)),
        ("dec", TimeDelta(10, TimeStep.YEARS)),
        ("mon", TimeDelta(1, TimeStep.MONTHS)),
        ("monC", TimeDelta(1, TimeStep.MONTHS)),
        ("monClim", TimeDelta(1, TimeStep.MONTHS)),
        ("day", TimeDelta(1, TimeStep.DAYS)),
        ("6hr", TimeDelta(6, TimeStep.HOURS)),
        ("3hr", TimeDelta(3, TimeStep.HOURS)),
        ("1hr", TimeDelta(1, TimeStep.HOURS)),
    ],
)
def test_how_wide_one_reporting_step_is(reporting_interval, expected):
    """
    The assumption a filename's half-open end rests on, checked by the time bounds
    """
    assert delta_of_reporting_interval(reporting_interval) == expected


def test_cmip5_and_cmip6_spell_a_monthly_climatology_differently():
    """
    `monClim` and `monC` are the same width, and both have to be known
    """
    assert delta_of_reporting_interval("monClim") == delta_of_reporting_interval("monC")
