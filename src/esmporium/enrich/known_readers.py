"""
The projects we know how to read a time range out of, and how

Two kinds of project knowledge live here, and they are registries: every
public function takes the mapping as a defaulted argument, so a caller
can add a project, or override one of ours, without forking anything.

```python
reader_for_project(
    "MIP1", readers={**FILENAME_TIME_READERS_BY_PROJECT, "MIP1": my_reader}
)
```

What is *not* project-specific stays out: how a published range becomes a half-open
one is [esmporium.enrich.protocol][], and the calendar arithmetic is
[esmporium.datasets.calendars][].
"""

from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING

from esmporium.datasets.time import Span, TimeStep
from esmporium.enrich.protocol import PublishedRange, step_of_stamp

if TYPE_CHECKING:
    from collections.abc import Mapping

    from esmporium.enrich.protocol import FilenameTimeReader

CLIMATOLOGY_SUFFIX = "-clim"
"""
What a climatology appends to its time range, e.g. `185001-185012-clim`

Pooled across projects rather than keyed by one. The DRS defines this suffix
for CMIP5 and CMIP6 alike, and a project which never emits it simply never matches.
The suffix is not part of the range, so it comes off before the stamps are read
-- but it is recorded, because twelve monthly values labelled 1850-1850 are
not a twelve-month run.
"""

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
How wide one reporting step is, by the reporting interval which names it

This is what makes a half-open end exact at every frequency: `end_exclusive` is the
last stamp in the filename advanced by one of these. A `3hr` file whose last stamp is
`...2100` runs to midnight, not to 22:00.

Pooled across projects rather than keyed by one, because the vocabulary is shared --
`mon` means the same thing in CMIP5, CMIP6 and CORDEX. `monClim` is CMIP5's spelling
of what CMIP6 calls `monC`, which is why both are here.
"""

NO_TIME_AXIS_INTERVALS_BY_PROJECT: Mapping[str, frozenset[str]] = {
    "CMIP5": frozenset({"fx"}),
    "CMIP6": frozenset({"fx"}),
    "CMIP6Plus": frozenset({"fx"}),
    "CMIP7": frozenset({"fx"}),
    "CORDEX-CMIP6": frozenset({"fx"}),
}
"""
Which reporting intervals mean "this dataset has no time axis at all", per project

What [Covers][esmporium.requirements.Covers] refuses against: coverage is not a
question you can ask of a auxiliary variables, such as cell area.
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


class UnknownProjectTimeAxisError(NotImplementedError):
    """
    Raised when we cannot say whether a project's dataset has a time axis
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
            f"dataset has no time axis. Known projects: {known}. Answering 'it has "
            f"one' would be a claim we have no grounds for, so pass your own "
            f"`intervals` mapping instead."
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


def has_no_time_axis(
    project: str,
    reporting_interval: str,
    intervals: Mapping[str, frozenset[str]] = NO_TIME_AXIS_INTERVALS_BY_PROJECT,
) -> bool:
    """
    Determine whether a dataset has no time axis at all

    Parameters
    ----------
    project
        The project the dataset belongs to

    reporting_interval
        The dataset's reporting interval

    intervals
        Which intervals mean "no time axis", per project

    Returns
    -------
    :
        `True` if this dataset cannot have a time range, e.g. an `fx` dataset

    Raises
    ------
    UnknownProjectTimeAxisError
        We know nothing about that project, so we cannot say

    Notes
    -----
    Raising for an unknown project rather than answering `False` is deliberate: `False`
    would read as "this dataset does have a time axis", which we have no grounds to
    say, and the cost of being wrong is a header fetched for a file that has no time
    in it at all.

    Examples
    --------
    >>> has_no_time_axis("CMIP6", "fx"), has_no_time_axis("CMIP6", "mon")
    (True, False)
    """
    if project not in intervals:
        raise UnknownProjectTimeAxisError(project, intervals)

    return reporting_interval in intervals[project]


def read_trailing_range(
    filename: str, *, reporting_interval: str
) -> PublishedRange | None:
    """
    Read a time range written as the last underscore-separated field of a filename

    The convention CMIP5, CMIP6, CMIP6Plus, CMIP7 and CORDEX-CMIP6 all share.

    Parameters
    ----------
    filename
        The filename, with its extension and no directories

    reporting_interval
        The dataset's reporting interval, e.g. `mon`

    Returns
    -------
    :
        What the filename published, or `None` if its last field is not a range

    Raises
    ------
    UnknownReportingIntervalError
        The reporting interval is not one whose step width we know

    UnreadableTimestampError
        The last field looks like a range but its stamps are not a shape we read

    Examples
    --------
    >>> published = read_trailing_range(
    ...     "tas_Amon_HadGEM2-ES_historical_r1i1p1_185912-188411.nc",
    ...     reporting_interval="mon",
    ... )
    >>> published.start_raw, published.end_raw
    ('185912', '188411')

    A climatology keeps its range and is marked as one:

    >>> clim = read_trailing_range(
    ...     "tas_Amon_ACCESS-ESM1-5_historical_r1i1p1f1_gn_185001-185012-clim.nc",
    ...     reporting_interval="monC",
    ... )
    >>> clim.is_climatology, clim.start_raw
    (True, '185001')

    A fixed field has no range, and says so quietly:

    >>> read_trailing_range(
    ...     "areacella_fx_HadGEM2-ES_historical_r0i0p0.nc", reporting_interval="fx"
    ... ) is None
    True
    """
    field = pathlib.PurePath(filename).stem.rsplit("_", 1)[-1]

    is_climatology = field.endswith(CLIMATOLOGY_SUFFIX)
    if is_climatology:
        field = field[: -len(CLIMATOLOGY_SUFFIX)]

    stamps = field.split("-")
    # A range is a start and an end, so two is the only number it could be.
    if len(stamps) != 2 or not all(  # noqa: PLR2004
        stamp.isdigit() for stamp in stamps
    ):
        return None

    start_raw, end_raw = stamps

    # The reporting interval first, because an interval we do not know the width of --
    # `subhr` -- is also written to a precision we do not read, and "how long is one
    # step" is the more useful of the two complaints.
    reports_every = span_of_reporting_interval(reporting_interval)

    return PublishedRange(
        start_raw=start_raw,
        end_raw=end_raw,
        precision=step_of_stamp(start_raw),
        reports_every=reports_every,
        is_climatology=is_climatology,
    )


FILENAME_TIME_READERS_BY_PROJECT: Mapping[str, FilenameTimeReader] = {
    "CMIP5": read_trailing_range,
    "CMIP6": read_trailing_range,
    "CMIP6Plus": read_trailing_range,
    "CMIP7": read_trailing_range,
    "CORDEX-CMIP6": read_trailing_range,
}
"""
Which [FilenameTimeReader][esmporium.enrich.FilenameTimeReader] reads each project

Every project we know shares one convention today -- the range is the last
underscore-separated field -- so they share one reader rather than five copies of it.
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
