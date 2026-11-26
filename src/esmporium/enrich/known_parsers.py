"""
The projects whose filenames we know how to parse a time range out of, and how

These parsers are specific to obtaining time ranges from the filename,
which can be wrong. Our assumptions about the data are explicit in this
module.

Every public function takes its mapping as a defaulted argument, so a caller can add a
project, or override one of ours, without forking anything:

```python
parser_for_project(
    "MIP1", parsers={**FILENAME_TIME_PARSERS_BY_PROJECT, "MIP1": my_parser}
)
```
"""

from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING

from esmporium.datasets.time import TimeDelta, TimeStep
from esmporium.enrich.protocol import PublishedRange, UnreadableTimestampError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from esmporium.enrich.protocol import FilenameTimeParser

STEP_BY_STAMP_LENGTH: Mapping[int, TimeStep] = {
    4: TimeStep.YEARS,
    6: TimeStep.MONTHS,
    8: TimeStep.DAYS,
    10: TimeStep.HOURS,
    12: TimeStep.MINUTES,
}
"""
How precisely a filename's timestamp is written, by how many digits it has

`YYYY`, `YYYYMM`, `YYYYMMDD`, `YYYYMMDDHH`, `YYYYMMDDHHmm`: how the projects in
[FILENAME_TIME_PARSERS_BY_PROJECT][(m).FILENAME_TIME_PARSERS_BY_PROJECT] write a
stamp, so the precision is read off the string's own length. Twelve digits is the
ordinary spelling for sub-daily data, not an edge case -- a `3hr` file is stamped
`185001010300-185101010000`. A project which spells its stamps some other way needs
its own reading, not another entry here.

A six-digit stamp is read as a year and a month rather than a six-digit year, which is
the only ambiguity here. CMIP piControl runs do use year numbers in the thousands
(`320001` is January 3200), and none has been seen past four digits.
"""

CLIMATOLOGY_SUFFIX = "-clim"
"""
What CMIP filenames append to the time range of a climatology, e.g.
`185001-185012-clim`

Every project in
[FILENAME_TIME_PARSERS_BY_PROJECT][(m).FILENAME_TIME_PARSERS_BY_PROJECT] writes it this
way. Another project may mark a climatology differently, or not in the filename at all.
The suffix is not part of the range, so it comes off before the stamps are read -- but
it is kept, as published, on
[PublishedRange.suffix][esmporium.enrich.PublishedRange.suffix], because twelve
monthly values labelled 1850-1850 are not a twelve-month run.
"""

# This is a guess.
# Also support using the time bounds to figure this out
# as a "higher tier" source.
DELTA_BY_REPORTING_INTERVAL: Mapping[str, TimeDelta] = {
    "dec": TimeDelta(10, TimeStep.YEARS),
    "yr": TimeDelta(1, TimeStep.YEARS),
    "yrPt": TimeDelta(1, TimeStep.YEARS),
    "mon": TimeDelta(1, TimeStep.MONTHS),
    "monC": TimeDelta(1, TimeStep.MONTHS),
    "monClim": TimeDelta(1, TimeStep.MONTHS),
    "monPt": TimeDelta(1, TimeStep.MONTHS),
    "day": TimeDelta(1, TimeStep.DAYS),
    "6hr": TimeDelta(6, TimeStep.HOURS),
    "6hrPt": TimeDelta(6, TimeStep.HOURS),
    "6hrLev": TimeDelta(6, TimeStep.HOURS),
    "3hr": TimeDelta(3, TimeStep.HOURS),
    "3hrPt": TimeDelta(3, TimeStep.HOURS),
    "1hr": TimeDelta(1, TimeStep.HOURS),
    "1hrPt": TimeDelta(1, TimeStep.HOURS),
    "1hrCM": TimeDelta(1, TimeStep.HOURS),
}
"""
How wide one reporting step is taken to be, by the reporting interval which names it

**An assumption**, and the fast path: a filename's half-open end is its last stamp
advanced by one of these, so a `3hr` file whose last stamp is `...2100` is taken to
run to midnight, not to 22:00. That holds when the data's time bounds are contiguous
and each as wide as its interval says, and it is what the first lower and last upper
time bound of a file check.

The point intervals -- `yrPt`, `monPt`, `6hrPt`, `3hrPt`, `1hrPt` -- are a convention
rather than a fact. Their values are instants, not intervals, so "the last step is one
interval wide" says where the next instant would be rather than where the data ends.

Pooled across projects rather than keyed by one, because the vocabulary is shared --
`mon` means the same thing wherever it appears. `monClim` is CMIP5's spelling of what
CMIP6 calls `monC`, which is why both are here. An interval a project uses and we do
not list is an [UnknownReportingIntervalError][(m).UnknownReportingIntervalError],
which is the point: there is no width to guess at.
"""


CLIMATOLOGY_INTERVALS_BY_PROJECT: Mapping[str, frozenset[str]] = {
    "CMIP5": frozenset({"monClim"}),
    "CMIP6": frozenset({"monC", "1hrCM"}),
    # CMIP6Plus's frequency vocabulary is CMIP6's, checked on both ESGF-NG mirrors on
    # 2026-11-27. No CMIP6Plus climatology dataset was published to read, so how one
    # is labelled in its files is not confirmed.
    "CMIP6Plus": frozenset({"monC", "1hrCM"}),
    # TODO: check the CMIP7 vocabulary for a frequency or temporal label meaning a
    # climatology. Until then a CMIP7 climatology is told apart by whether its
    # climatology bounds meet end to end.
    # Checked and CMIP7 doesn't have a climatology frequency, but the 'statistical
    # climatology' as per CF (contiguous) could be caught in processing_id.
    # Decision to make about if we include here or not.
    "CMIP7": frozenset(),
}
"""
Which reporting intervals mean a dataset is a multi-year climatology, per project

A multi-year climatology, e.g. the average January over 1961 to 1990, holds values
describing a typical year, so the period it is labelled with is the period averaged
over and not a run of data, and no time range is read off it. These projects label one
in three ways at once: this reporting interval, a `-clim` suffix on the filename
([CLIMATOLOGY_SUFFIX][(m).CLIMATOLOGY_SUFFIX]) and `climatology` bounds on the time
variable. Seen on CMIP6 `monC` (E3SM-1-0 `phalf`) and `1hrCM` (HadGEM3-GC31-LL `rsut`)
files. The label is believed over the bounds: an E3SM-1-0 `monC` file carries bounds a
month wide which meet end to end, and is still published as a climatology.

Pass a mapping of your own to add a project, or override one of ours, e.g.
`{**CLIMATOLOGY_INTERVALS_BY_PROJECT, "MIP1": frozenset({"monC"})}`.
"""


class UnknownProjectClimatologyIntervalsError(NotImplementedError):
    """
    Raised when we cannot say which of a project's reporting intervals are climatologies
    """

    def __init__(self, project: str, intervals: Mapping[str, frozenset[str]]) -> None:
        """
        Initialise the error

        Parameters
        ----------
        project
            The project we know nothing about

        intervals
            The per-project vocabularies which were available
        """
        self.project = project
        self.intervals = intervals
        known = ", ".join(sorted(intervals)) or "none"
        super().__init__(
            f"Do not know which of project {project!r}'s reporting intervals mean a "
            f"dataset is a multi-year climatology. Known projects: {known}. Answering "
            f"'none of them' would let a climatology's averaging period be read as a "
            f"run of data, so pass your own `intervals` mapping instead."
        )


def is_climatology_interval(
    project: str,
    reporting_interval: str,
    intervals: Mapping[str, frozenset[str]] = CLIMATOLOGY_INTERVALS_BY_PROJECT,
) -> bool:
    """
    Determine whether a reporting interval means a dataset is a multi-year climatology

    Parameters
    ----------
    project
        The project the dataset belongs to

    reporting_interval
        The dataset's reporting interval

    intervals
        Which intervals mean a climatology, per project

    Returns
    -------
    :
        `True` if the project uses this interval only for multi-year climatologies

    Raises
    ------
    UnknownProjectClimatologyIntervalsError
        We know nothing about that project, so we cannot say

    Examples
    --------
    >>> (
    ...     is_climatology_interval("CMIP6", "monC"),
    ...     is_climatology_interval("CMIP6", "mon"),
    ... )
    (True, False)
    """
    if project not in intervals:
        raise UnknownProjectClimatologyIntervalsError(project, intervals)

    return reporting_interval in intervals[project]


class UnknownProjectFilenameTimeParserError(NotImplementedError):
    """
    Raised when we have no filename time parser for a project
    """

    def __init__(self, project: str, parsers: Mapping[str, FilenameTimeParser]) -> None:
        """
        Initialise the error

        Parameters
        ----------
        project
            The project we have no parser for

        parsers
            The parsers which were available
        """
        self.project = project
        self.parsers = parsers
        known = ", ".join(sorted(parsers)) or "none"
        super().__init__(
            f"No filename time parser for project {project!r}. Known projects: "
            f"{known}. Where a filename puts its time range is a project's own "
            f"convention, so there is no generic fallback -- pass your own `parsers` "
            f"mapping, e.g. "
            f"{{**FILENAME_TIME_PARSERS_BY_PROJECT, {project!r}: my_parser}}."
        )


class UnknownReportingIntervalError(NotImplementedError):
    """
    Raised when we do not know how wide one step of a reporting interval is
    """

    def __init__(self, reporting_interval: str) -> None:
        """
        Initialise the error

        Parameters
        ----------
        reporting_interval
            The interval whose width we do not know
        """
        self.reporting_interval = reporting_interval
        known = ", ".join(sorted(DELTA_BY_REPORTING_INTERVAL))
        super().__init__(
            f"Do not know how long one {reporting_interval!r} step is, so the "
            f"half-open end of its range cannot be worked out. Known intervals: "
            f"{known}. `subhr` is absent on purpose: it says the data is finer than "
            f"hourly without saying how much finer."
        )


def delta_of_reporting_interval(
    reporting_interval: str,
    deltas: Mapping[str, TimeDelta] = DELTA_BY_REPORTING_INTERVAL,
) -> TimeDelta:
    """
    Get how wide one reporting step is

    Parameters
    ----------
    reporting_interval
        The reporting interval, e.g. `mon`, `3hr`

    deltas
        The widths to look it up in

    Returns
    -------
    :
        How long one step of that interval is

    Raises
    ------
    UnknownReportingIntervalError
        The interval is not one whose width we know

    Examples
    --------
    >>> str(delta_of_reporting_interval("3hr"))
    '3 hours'
    """
    try:
        return deltas[reporting_interval]
    except KeyError as error:
        raise UnknownReportingIntervalError(reporting_interval) from error


def step_of_stamp(
    stamp: str, steps: Mapping[int, TimeStep] = STEP_BY_STAMP_LENGTH
) -> TimeStep:
    """
    Get how precisely a filename timestamp is written

    Parameters
    ----------
    stamp
        The timestamp, e.g. `"185001"`

    steps
        The precision each length of stamp is written to

    Returns
    -------
    :
        The finest step the stamp names

    Raises
    ------
    UnreadableTimestampError
        The stamp is not all digits, or is not a length in `steps`

    Examples
    --------
    >>> step_of_stamp("185001")
    <TimeStep.MONTHS: 'months'>
    """
    if not stamp.isdigit() or len(stamp) not in steps:
        raise UnreadableTimestampError(stamp)

    return steps[len(stamp)]


def parse_trailing_range(filename: str) -> PublishedRange | None:
    """
    Parse a time range written as the last underscore-separated field of a filename

    The convention every project in `FILENAME_TIME_PARSERS_BY_PROJECT` shares.

    Parameters
    ----------
    filename
        The filename, with its extension and no directories

    Returns
    -------
    :
        What the filename published, or `None` if its last field is not a range

        The stamps are returned as written, whatever their length: whether they are
        a shape that can be read is the conversion's question.

    Examples
    --------
    >>> published = parse_trailing_range(
    ...     "tas_Amon_HadGEM2-ES_historical_r1i1p1_185912-188411.nc"
    ... )
    >>> published.start_raw, published.end_raw, published.suffix
    ('185912', '188411', None)

    A climatology keeps its range, and the suffix it was marked with:

    >>> clim = parse_trailing_range(
    ...     "tas_Amon_ACCESS-ESM1-5_historical_r1i1p1f1_gn_185001-185012-clim.nc"
    ... )
    >>> clim.start_raw, clim.suffix
    ('185001', '-clim')

    A fixed field has no range, and says so quietly:

    >>> parse_trailing_range("areacella_fx_HadGEM2-ES_historical_r0i0p0.nc") is None
    True
    """
    field = pathlib.PurePath(filename).stem.rsplit("_", 1)[-1]

    suffix = None
    if field.endswith(CLIMATOLOGY_SUFFIX):
        suffix = CLIMATOLOGY_SUFFIX
        field = field[: -len(CLIMATOLOGY_SUFFIX)]

    stamps = field.split("-")
    # A range is a start and an end, so two is the only number it could be.
    if len(stamps) != 2 or not all(  # noqa: PLR2004
        stamp.isdigit() for stamp in stamps
    ):
        return None

    start_raw, end_raw = stamps

    return PublishedRange(start_raw=start_raw, end_raw=end_raw, suffix=suffix)


FILENAME_TIME_PARSERS_BY_PROJECT: Mapping[str, FilenameTimeParser] = {
    "CMIP5": parse_trailing_range,
    "CMIP6": parse_trailing_range,
    "CMIP6Plus": parse_trailing_range,
    "CMIP7": parse_trailing_range,
}
"""
Which [FilenameTimeParser][esmporium.enrich.FilenameTimeParser] parses each project

Every project we know shares one convention today -- the range is the last
underscore-separated field -- so they share one parser rather than four copies of it.
That holds across a change of DRS: a CMIP7 filename looks nothing like a CMIP6 one
(`tas_tavg-h2m-hxy-u_day_glb_g120_CanESM5-1_1pctCO2_r1i1p2f1_18500101-18601231.nc`)
and its range is still the last field.
"""


def parser_for_project(
    project: str,
    parsers: Mapping[str, FilenameTimeParser] = FILENAME_TIME_PARSERS_BY_PROJECT,
) -> FilenameTimeParser:
    """
    Get the filename time parser for a project

    Parameters
    ----------
    project
        The project whose filenames are to be parsed

    parsers
        The parsers to choose from

    Returns
    -------
    :
        The parser for that project

    Raises
    ------
    UnknownProjectFilenameTimeParserError
        We have no parser for that project, and there is no generic fallback

    Examples
    --------
    >>> parser_for_project("CMIP6") is parse_trailing_range
    True

    >>> try:
    ...     parser_for_project("MIP1")
    ... except UnknownProjectFilenameTimeParserError as error:
    ...     error.project
    'MIP1'
    """
    try:
        return parsers[project]
    except KeyError as error:
        raise UnknownProjectFilenameTimeParserError(project, parsers) from error
