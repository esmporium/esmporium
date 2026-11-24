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

_STAMP_PART_WIDTHS = (4, 2, 2, 2, 2)
"""
How many digits each part of a stamp takes: year, month, day, hour, minute

The truncated ISO 8601 basic format, `YYYY[MM[DD[HH[mm]]]]`.
"""

_STAMP_LENGTHS = frozenset(sum(_STAMP_PART_WIDTHS[:n_parts]) for n_parts in range(1, 6))
"""How many digits a stamp in that format can have: 4, 6, 8, 10 or 12"""


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
        super().__init__(
            f"{stamp!r} is not a timestamp this reads: expected digits in the shape "
            f"YYYY[MM[DD[HH[mm]]]], and got {len(stamp)} characters."
        )


class StampPrecisionMismatchError(ValueError):
    """
    Raised when a timestamp is not written to the precision it is read at
    """

    def __init__(self, stamp: str, precision: TimeStep) -> None:
        """
        Initialise the error

        Parameters
        ----------
        stamp
            The timestamp

        precision
            The precision it was read at
        """
        self.stamp = stamp
        self.precision = precision
        super().__init__(
            f"{stamp!r} is not written to the {precision.value.rstrip('s')}, which is "
            "the precision it was read at. Reading it at another precision would "
            "either drop parts it states or fill in parts it does not, so it is "
            "refused instead."
        )


class CalendarRequiredError(ValueError):
    """
    Raised when a range is written finer than a month, so has no natural axis
    """

    def __init__(self, published: PublishedRange, precision: TimeStep) -> None:
        """
        Initialise the error

        Parameters
        ----------
        published
            The range which cannot be converted

        precision
            The precision its stamps are written to
        """
        self.published = published
        self.precision = precision
        super().__init__(
            f"{published.start_raw}-{published.end_raw} is written to the "
            f"{precision.value.rstrip('s')}, and only years and months have a "
            "calendar-free ordinal to place it on. Two dates are a length only "
            "once the calendar is known, and an exact dated range comes from the "
            "file's time bounds rather than from its name."
        )


@dataclass(frozen=True)
class PublishedRange:
    """
    The time range a filename publishes, exactly as it publishes it

    Inclusive at both ends, because that is what a filename means: a monthly file
    named `..._320001-325012.nc` contains December 325012. Only the text is kept,
    because the text is the observation. How precisely the stamps are written and how
    wide the last step is are readings of it, so they are passed to
    [to_half_open][(c).to_half_open] rather than stored here.
    """

    start_raw: str
    """The start of the range, as the filename spells it"""

    end_raw: str
    """The end of the range, as the filename spells it, and inclusive"""

    suffix: str | None = None
    """
    Anything the filename appends to the range, as it spells it, e.g. `"-clim"`

    `None` when the range is the whole field. What a suffix means is the project's
    convention, not this type's.
    """

    def to_half_open(self, *, precision: TimeStep, reports_every: Span) -> TimeRange:
        """
        Convert to a half-open range on the natural axis of `precision`

        **The only place the `+ 1` lives.** Everything downstream reads
        `end_exclusive` and subtracts.

        The half-open end rests on an assumption, which is why `reports_every` is
        passed rather than looked up: that the last step is one `reports_every` wide,
        i.e. that the data runs to the end of the step its last stamp names. That is
        true when the time bounds are contiguous and as wide as the reporting
        interval, and only a convention for point data, whose values are instants.
        The file's time bounds are what check it.

        Parameters
        ----------
        precision
            How precisely the two stamps are written, which is the step of the axis
            the range is placed on

        reports_every
            How wide the last step of the range is taken to be

        Returns
        -------
        :
            The same range, half-open, as
            [TimeRange][esmporium.datasets.TimeRange] on a natural axis of
            `precision`

        Raises
        ------
        CalendarRequiredError
            `precision` is finer than a month

        UnreadableTimestampError
            One of the stamps is not a shape we read

        StampPrecisionMismatchError
            One of the stamps is not written to `precision`

        Examples
        --------
        Yearly data published as 1850 to 1860 holds eleven years:

        >>> published = PublishedRange("1850", "1860")
        >>> half_open = published.to_half_open(
        ...     precision=TimeStep.YEARS, reports_every=Span(1, TimeStep.YEARS)
        ... )
        >>> half_open.start, half_open.end_exclusive, str(half_open.extent)
        (1850, 1861, '11 years')

        A single month holds one month, where subtracting the published values would
        give none:

        >>> single = PublishedRange("320001", "320001")
        >>> str(
        ...     single.to_half_open(
        ...         precision=TimeStep.MONTHS, reports_every=Span(1, TimeStep.MONTHS)
        ...     ).extent
        ... )
        '1 month'

        A daily range says what it needs rather than guessing:

        >>> daily = PublishedRange("18500101", "18591231")
        >>> try:
        ...     daily.to_half_open(
        ...         precision=TimeStep.DAYS, reports_every=Span(1, TimeStep.DAYS)
        ...     )
        ... except CalendarRequiredError as error:
        ...     error.precision
        <TimeStep.DAYS: 'days'>
        """
        if not precision.is_calendar_free:
            raise CalendarRequiredError(self, precision)

        axis = TimeAxis(TimeUnits(precision))

        return TimeRange(
            start=_position_of_stamp(self.start_raw, precision, axis),
            end_exclusive=advance(
                _position_of_stamp(self.end_raw, precision, axis), reports_every, axis
            ),
            axis=axis,
        )


def _read_stamp(stamp: str) -> tuple[int, ...]:
    """
    Read a filename timestamp into the parts it states

    Parameters
    ----------
    stamp
        The timestamp, e.g. `"185001"`

    Returns
    -------
    :
        Year, then month, day, hour and minute for as many of those as the stamp
        writes, and no more

    Raises
    ------
    UnreadableTimestampError
        The stamp is not all digits, or is not one of the lengths
        `YYYY[MM[DD[HH[mm]]]]` gives

    Examples
    --------
    >>> _read_stamp("185001")
    (1850, 1)

    >>> _read_stamp("185001010300")
    (1850, 1, 1, 3, 0)
    """
    if not stamp.isdigit() or len(stamp) not in _STAMP_LENGTHS:
        raise UnreadableTimestampError(stamp)

    parts = []
    at = 0
    for width in _STAMP_PART_WIDTHS:
        if at == len(stamp):
            break

        parts.append(int(stamp[at : at + width]))
        at += width

    return tuple(parts)


def _position_of_stamp(stamp: str, precision: TimeStep, axis: TimeAxis) -> int:
    """
    Get where a filename timestamp falls on a natural axis

    Parameters
    ----------
    stamp
        The timestamp, e.g. `"185001"`

    precision
        The precision the stamp is written to, years or months

    axis
        The natural axis of `precision`

    Returns
    -------
    :
        Its position on `axis`

    Raises
    ------
    UnreadableTimestampError
        The stamp is not a shape we read

    StampPrecisionMismatchError
        The stamp is not written to `precision`
    """
    parts = _read_stamp(stamp)
    wanted = 1 if precision == TimeStep.YEARS else 2
    if len(parts) != wanted:
        raise StampPrecisionMismatchError(stamp, precision)

    return position_of_date(
        axis, year=parts[0], month=parts[1] if precision == TimeStep.MONTHS else None
    )


class FilenameTimeReader(Protocol):
    """
    Reads the time range a project's filenames publish

    One per project, registered in
    [FILENAME_TIME_READERS_BY_PROJECT][esmporium.enrich.FILENAME_TIME_READERS_BY_PROJECT],
    because where a filename puts its range -- and whether it has one at all -- is a
    project's own convention.
    """

    def __call__(self, filename: str) -> PublishedRange | None:
        """
        Read the time range out of a filename

        Parameters
        ----------
        filename
            The filename, with its extension, and no directories

        Returns
        -------
        :
            What the filename published, or `None` if it carries no range at all

            `None` is the ordinary answer for a dataset with no time axis, e.g. an
            `fx` file, and is not an error.
        """
        ...
