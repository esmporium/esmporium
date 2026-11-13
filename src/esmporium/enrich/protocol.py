"""
What a filename says about time, and the shape of a reader which reads it

The raw range and the one conversion off it. The conversion is the only place in
esmporium where a published, inclusive range becomes a half-open one, so it is the
only place the `+ 1` lives and the only place to look when an extent is wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from esmporium.datasets.calendars import advance, position_of_date
from esmporium.datasets.time import Span, TimeAxis, TimeRange, TimeStep, TimeUnits

STEP_BY_STAMP_LENGTH: dict[int, TimeStep] = {
    4: TimeStep.YEARS,
    6: TimeStep.MONTHS,
    8: TimeStep.DAYS,
    10: TimeStep.HOURS,
    12: TimeStep.MINUTES,
}
"""
How precisely a filename's timestamp is written, by how many digits it has

`YYYY`, `YYYYMM`, `YYYYMMDD`, `YYYYMMDDHH`, `YYYYMMDDHHmm`. Pooled across projects
rather than keyed by one: a truncated datetime is the shared spelling, so the
precision is read off the string's own length and no project has to declare which
lengths it writes. Twelve digits is the ordinary spelling for sub-daily data, not an
edge case -- a `3hr` file is stamped `185001010300-185101010000` whoever published
it. A project which spells its stamps some other way needs its own
`FilenameTimeReader`, not another entry here.

Fourteen digits -- seconds, which `subhr` data uses -- is deliberately absent, because
we do not know how wide one `subhr` step is either, so such a file cannot have its
half-open end worked out anyway. Reading it as minutes would turn that into a wrong
answer presented as a right one.

A six-digit stamp is read as a year and a month rather than a six-digit year, which is
the only ambiguity here. CMIP piControl runs do use year numbers in the thousands
(`320001` is January 3200), and none has been seen past four digits.
"""


class UnreadableTimestampError(ValueError):
    """
    Raised when a filename's timestamp is not a shape we read
    """

    def __init__(self, stamp: str) -> None:
        """
        Initialise the error

        Parameters
        ----------
        stamp
            The timestamp which could not be read
        """
        self.stamp = stamp
        lengths = ", ".join(str(length) for length in sorted(STEP_BY_STAMP_LENGTH))
        super().__init__(
            f"{stamp!r} is not a timestamp this reads: expected {lengths} digits, "
            f"i.e. YYYY up to YYYYMMDDHHmm, and got {len(stamp)}."
        )


class CalendarRequiredError(ValueError):
    """
    Raised when a range cannot be made half-open without knowing the calendar
    """

    def __init__(self, published: PublishedRange) -> None:
        """
        Initialise the error

        Parameters
        ----------
        published
            The range which cannot be converted
        """
        self.published = published
        super().__init__(
            f"{published.start_raw}-{published.end_raw} is written to the "
            f"{published.precision.value.rstrip('s')}, so turning it into a count "
            "needs the calendar, and no axis was given. Years and months are the "
            "only steps with a calendar-free ordinal. Read the file header for the "
            "calendar and time units, then pass the axis they describe."
        )


@dataclass(frozen=True)
class PublishedRange:
    """
    The time range a filename publishes, exactly as it publishes it

    Inclusive at both ends, because that is what a filename means: a monthly file
    named `..._320001-325012.nc` contains December 325012. The strings are kept rather
    than only the numbers, because the string is the observation and the number is our
    parse of it -- two different facts, and keeping the string lets a wrong parser be
    corrected without re-reading anything.
    """

    start_raw: str
    """The start of the range, as the filename spells it"""

    end_raw: str
    """The end of the range, as the filename spells it, and inclusive"""

    precision: TimeStep
    """
    How precisely the two stamps are written

    What the *filename* says, which is not the same as how often the data reports: a
    `3hr` dataset is stamped to the hour.
    """

    reports_every: Span
    """
    How often the data reports, i.e. how wide the last step of the range is

    This is what makes the half-open end exact at every frequency:
    `end_exclusive` is `end_raw` advanced by one of these. For monthly data that is
    one month, for `3hr` data three hours.
    """

    is_climatology: bool = False
    """
    Whether the file is a climatology, which a `-clim` suffix on the range marks
    """

    def to_half_open(self, *, axis: TimeAxis | None = None) -> TimeRange:
        """
        Convert to a half-open range

        **The only place the `+ 1` lives.** Everything downstream reads
        `end_exclusive` and subtracts.

        Parameters
        ----------
        axis
            The axis to place the range on

            `None` asks for the natural axis, i.e. a 1-based ordinal count of this
            range's own precision with no epoch and no calendar, which exists for
            years and months and not for anything finer. Pass the axis a file header
            described to place a finer range, or to place any range on a dated axis.

        Returns
        -------
        :
            The same range, half-open, as
            [TimeRange][esmporium.datasets.TimeRange]

        Raises
        ------
        CalendarRequiredError
            The range is written to the day or finer and no `axis` was given

        UnreadableTimestampError
            One of the stamps is not a shape we read

        Examples
        --------
        Yearly data published as 1850 to 1860 holds eleven years:

        >>> published = PublishedRange(
        ...     "1850", "1860", TimeStep.YEARS, Span(1, TimeStep.YEARS)
        ... )
        >>> half_open = published.to_half_open()
        >>> half_open.start, half_open.end_exclusive, half_open.extent
        (1850, 1861, 11)

        A single month holds one month, where subtracting the published values would
        give none:

        >>> single = PublishedRange(
        ...     "320001", "320001", TimeStep.MONTHS, Span(1, TimeStep.MONTHS)
        ... )
        >>> single.to_half_open().extent
        1

        A daily range says what it needs rather than guessing:

        >>> daily = PublishedRange(
        ...     "18500101", "18591231", TimeStep.DAYS, Span(1, TimeStep.DAYS)
        ... )
        >>> try:
        ...     daily.to_half_open()
        ... except CalendarRequiredError as error:
        ...     error.published.precision
        <TimeStep.DAYS: 'days'>
        """
        on = axis if axis is not None else self._natural_axis()

        return TimeRange(
            start=_position_of_stamp(self.start_raw, on),
            end_exclusive=advance(
                _position_of_stamp(self.end_raw, on), self.reports_every, on
            ),
            axis=on,
        )

    def _natural_axis(self) -> TimeAxis:
        """
        Get the natural axis this range's own precision implies

        Returns
        -------
        :
            A 1-based ordinal axis of this range's precision, with no epoch and no
            calendar

        Raises
        ------
        CalendarRequiredError
            The precision is finer than a month, which has no calendar-free ordinal
        """
        if not self.precision.is_calendar_free:
            raise CalendarRequiredError(self)

        return TimeAxis(TimeUnits(self.precision))


def _read_stamp(stamp: str) -> tuple[int, int, int, int, int]:
    """
    Read a filename timestamp into a year, month, day, hour and minute

    Parameters
    ----------
    stamp
        The timestamp, e.g. `"185001"`

    Returns
    -------
    :
        Year, month, day, hour and minute, with anything the stamp does not say
        defaulted to the start of the period it names

    Raises
    ------
    UnreadableTimestampError
        The stamp is not all digits, or is not a length we read

    Examples
    --------
    >>> _read_stamp("185001")
    (1850, 1, 1, 0, 0)

    >>> _read_stamp("185001010300")
    (1850, 1, 1, 3, 0)
    """
    if not stamp.isdigit() or len(stamp) not in STEP_BY_STAMP_LENGTH:
        raise UnreadableTimestampError(stamp)

    return (
        int(stamp[:4]),
        int(stamp[4:6] or 1),
        int(stamp[6:8] or 1),
        int(stamp[8:10] or 0),
        int(stamp[10:12] or 0),
    )


def _position_of_stamp(stamp: str, axis: TimeAxis) -> int:
    """
    Get where a filename timestamp falls on a time axis

    Parameters
    ----------
    stamp
        The timestamp, e.g. `"185001"`

    axis
        The axis to place it on

    Returns
    -------
    :
        Its position on `axis`

    Raises
    ------
    UnreadableTimestampError
        The stamp is not a shape we read
    """
    year, month, day, hour, minute = _read_stamp(stamp)

    return position_of_date(
        axis, year=year, month=month, day=day, hour=hour, minute=minute
    )


def step_of_stamp(stamp: str) -> TimeStep:
    """
    Get how precisely a filename timestamp is written

    Parameters
    ----------
    stamp
        The timestamp, e.g. `"185001"`

    Returns
    -------
    :
        The finest step the stamp names

    Raises
    ------
    UnreadableTimestampError
        The stamp is not all digits, or is not a length we read

    Examples
    --------
    >>> step_of_stamp("185001")
    <TimeStep.MONTHS: 'months'>
    """
    if not stamp.isdigit() or len(stamp) not in STEP_BY_STAMP_LENGTH:
        raise UnreadableTimestampError(stamp)

    return STEP_BY_STAMP_LENGTH[len(stamp)]


class FilenameTimeReader(Protocol):
    """
    Reads the time range a project's filenames publish

    One per project, registered in
    [FILENAME_TIME_READERS_BY_PROJECT][esmporium.enrich.FILENAME_TIME_READERS_BY_PROJECT],
    because where a filename puts its range -- and whether it has one at all -- is a
    project's own convention.
    """

    def __call__(
        self, filename: str, *, reporting_interval: str
    ) -> PublishedRange | None:
        """
        Read the time range out of a filename

        Parameters
        ----------
        filename
            The filename, with its extension, and no directories

        reporting_interval
            The dataset's reporting interval, e.g. `mon`, `3hr`

            Needed because it is how wide the last step of the range is, which is what
            makes the half-open end exact. It is a facet we already hold, so asking
            for it costs nothing.

        Returns
        -------
        :
            What the filename published, or `None` if it carries no range at all

            `None` is the ordinary answer for a dataset with no time axis, e.g. an
            `fx` file, and is not an error.
        """
        ...
