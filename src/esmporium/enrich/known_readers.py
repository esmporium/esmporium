"""
The projects we know how to read a time range out of, and how

These readers are specific to obtaining time ranges from the filename,
which can be wrong. Our assumptions about the data are explicit in this
module.

Every public function takes its mapping as a defaulted argument, so a caller can add a
project, or override one of ours, without forking anything:

```python
reader_for_project(
    "MIP1", readers={**FILENAME_TIME_READERS_BY_PROJECT, "MIP1": my_reader}
)
```
"""

from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING

from esmporium.datasets.time import Span, TimeStep
from esmporium.enrich.protocol import PublishedRange, UnreadableTimestampError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from esmporium.enrich.protocol import FilenameTimeReader

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
[FILENAME_TIME_READERS_BY_PROJECT][(m).FILENAME_TIME_READERS_BY_PROJECT] write a
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
[FILENAME_TIME_READERS_BY_PROJECT][(m).FILENAME_TIME_READERS_BY_PROJECT] writes it this
way. Another project may mark a climatology differently, or not in the filename at all.
The suffix is not part of the range, so it comes off before the stamps are read -- but
it is kept, as published, on
[PublishedRange.suffix][esmporium.enrich.PublishedRange.suffix], because twelve
monthly values labelled 1850-1850 are not a twelve-month run.
"""

# This is a guess.
# Also support using the time bounds to figure this out
# as a "higher tier" source.
SPAN_BY_REPORTING_INTERVAL: Mapping[str, Span] = {
    "dec": Span(10, TimeStep.YEARS),
    "yr": Span(1, TimeStep.YEARS),
    "yrPt": Span(1, TimeStep.YEARS),
    "mon": Span(1, TimeStep.MONTHS),
    "monC": Span(1, TimeStep.MONTHS),
    "monClim": Span(1, TimeStep.MONTHS),
    "monPt": Span(1, TimeStep.MONTHS),
    "day": Span(1, TimeStep.DAYS),
    "6hr": Span(6, TimeStep.HOURS),
    "6hrPt": Span(6, TimeStep.HOURS),
    "6hrLev": Span(6, TimeStep.HOURS),
    "3hr": Span(3, TimeStep.HOURS),
    "3hrPt": Span(3, TimeStep.HOURS),
    "1hr": Span(1, TimeStep.HOURS),
    "1hrPt": Span(1, TimeStep.HOURS),
    "1hrCM": Span(1, TimeStep.HOURS),
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


class UnknownProjectFilenameTimeReaderError(NotImplementedError):
    """
    Raised when we have no filename time reader for a project
    """

    def __init__(self, project: str, readers: Mapping[str, FilenameTimeReader]) -> None:
        """
        Initialise the error

        Parameters
        ----------
        project
            The project we have no reader for

        readers
            The readers which were available
        """
        self.project = project
        self.readers = readers
        known = ", ".join(sorted(readers)) or "none"
        super().__init__(
            f"No filename time reader for project {project!r}. Known projects: "
            f"{known}. Where a filename puts its time range is a project's own "
            f"convention, so there is no generic fallback -- pass your own `readers` "
            f"mapping, e.g. "
            f"{{**FILENAME_TIME_READERS_BY_PROJECT, {project!r}: my_reader}}."
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
        known = ", ".join(sorted(SPAN_BY_REPORTING_INTERVAL))
        super().__init__(
            f"Do not know how long one {reporting_interval!r} step is, so the "
            f"half-open end of its range cannot be worked out. Known intervals: "
            f"{known}. `subhr` is absent on purpose: it says the data is finer than "
            f"hourly without saying how much finer."
        )


def span_of_reporting_interval(
    reporting_interval: str,
    spans: Mapping[str, Span] = SPAN_BY_REPORTING_INTERVAL,
) -> Span:
    """
    Get how wide one reporting step is

    Parameters
    ----------
    reporting_interval
        The reporting interval, e.g. `mon`, `3hr`

    spans
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
    >>> str(span_of_reporting_interval("3hr"))
    '3 hours'
    """
    try:
        return spans[reporting_interval]
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


def read_trailing_range(filename: str) -> PublishedRange | None:
    """
    Read a time range written as the last underscore-separated field of a filename

    The convention every project in `FILENAME_TIME_READERS_BY_PROJECT` shares.

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
    >>> published = read_trailing_range(
    ...     "tas_Amon_HadGEM2-ES_historical_r1i1p1_185912-188411.nc"
    ... )
    >>> published.start_raw, published.end_raw, published.suffix
    ('185912', '188411', None)

    A climatology keeps its range, and the suffix it was marked with:

    >>> clim = read_trailing_range(
    ...     "tas_Amon_ACCESS-ESM1-5_historical_r1i1p1f1_gn_185001-185012-clim.nc"
    ... )
    >>> clim.start_raw, clim.suffix
    ('185001', '-clim')

    A fixed field has no range, and says so quietly:

    >>> read_trailing_range("areacella_fx_HadGEM2-ES_historical_r0i0p0.nc") is None
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


FILENAME_TIME_READERS_BY_PROJECT: Mapping[str, FilenameTimeReader] = {
    "CMIP5": read_trailing_range,
    "CMIP6": read_trailing_range,
    "CMIP6Plus": read_trailing_range,
    "CMIP7": read_trailing_range,
}
"""
Which [FilenameTimeReader][esmporium.enrich.FilenameTimeReader] reads each project

Every project we know shares one convention today -- the range is the last
underscore-separated field -- so they share one reader rather than four copies of it.
That holds across a change of DRS: a CMIP7 filename looks nothing like a CMIP6 one
(`tas_tavg-h2m-hxy-u_day_glb_g120_CanESM5-1_1pctCO2_r1i1p2f1_18500101-18601231.nc`)
and its range is still the last field.
"""


def reader_for_project(
    project: str,
    readers: Mapping[str, FilenameTimeReader] = FILENAME_TIME_READERS_BY_PROJECT,
) -> FilenameTimeReader:
    """
    Get the filename time reader for a project

    Parameters
    ----------
    project
        The project whose filenames are to be read

    readers
        The readers to choose from

    Returns
    -------
    :
        The reader for that project

    Raises
    ------
    UnknownProjectFilenameTimeReaderError
        We have no reader for that project, and there is no generic fallback

    Examples
    --------
    >>> reader_for_project("CMIP6") is read_trailing_range
    True

    >>> try:
    ...     reader_for_project("MIP1")
    ... except UnknownProjectFilenameTimeReaderError as error:
    ...     error.project
    'MIP1'
    """
    try:
        return readers[project]
    except KeyError as error:
        raise UnknownProjectFilenameTimeReaderError(project, readers) from error
