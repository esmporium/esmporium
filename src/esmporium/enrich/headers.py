"""
Reading a time axis out of a file's header attributes

Project-independent, and deliberately so: `units` and `calendar` are CF attributes,
so the formats are per-file, not per-project

Note what the input is. These are the attributes of the file's **time variable**, not
its global attributes: a CMIP file does not publish its own calendar globally.
Fetching variable attributes is a job for [esmporium.headers][], and lands with
the step which does the fetching; this function is the pure half, and takes whatever
that returns.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from esmporium.datasets.time import TimeAxis, TimeUnits

if TYPE_CHECKING:
    from collections.abc import Mapping

CF_UNITS_ATTRIBUTE = "units"
"""The CF attribute naming what a time coordinate's values count"""

CF_CALENDAR_ATTRIBUTE = "calendar"
"""The CF attribute naming which calendar a time coordinate's values are in"""

CF_DEFAULT_CALENDAR = "standard"
"""
The calendar CF says to assume when a time coordinate does not name one

Applied, because it is the convention rather than a guess of ours: CF states that a
time coordinate with no `calendar` attribute is on the standard (mixed
Julian/Gregorian) calendar. Recorded all the same, so the stored row says `standard`
rather than nothing -- what `NULL` is reserved for is *not having read a header at
all*, which is a different thing from having read one that stayed silent.
"""


class NoTimeUnitsError(ValueError):
    """
    Raised when a time variable's attributes do not say what its values count
    """

    def __init__(self, attributes: Mapping[str, object]) -> None:
        """
        Initialise the error

        Parameters
        ----------
        attributes
            The attributes which were read
        """
        self.attributes = attributes
        found = ", ".join(sorted(str(name) for name in attributes)) or "none"
        super().__init__(
            f"These attributes do not include {CF_UNITS_ATTRIBUTE!r}, so there is "
            f"nothing to say what the time values count. Attributes found: {found}. "
            f"Note that a file's calendar and time units are attributes of its time "
            f"*variable* and not global attributes, so a header read which asked for "
            f"globals will never find them."
        )


def read_time_axis(attributes: Mapping[str, object]) -> TimeAxis:
    """
    Read a time axis out of a time variable's attributes

    Parameters
    ----------
    attributes
        The attributes of the file's time variable, names and values untouched

    Returns
    -------
    :
        The axis they describe

    Raises
    ------
    NoTimeUnitsError
        The attributes do not include `units`

    UnknownTimeUnitsError
        The `units` attribute is not a string we read

    Examples
    --------
    >>> axis = read_time_axis({"units": "days since 1850-01-01", "calendar": "noleap"})
    >>> str(axis.units), axis.calendar
    ('days since 1850-01-01', 'noleap')

    A file which does not name a calendar gets CF's own default, recorded rather than
    left empty:

    >>> read_time_axis({"units": "days since 1850-01-01"}).calendar
    'standard'
    """
    if CF_UNITS_ATTRIBUTE not in attributes:
        raise NoTimeUnitsError(attributes)

    calendar = _as_text(attributes.get(CF_CALENDAR_ATTRIBUTE))

    return TimeAxis(
        units=TimeUnits.parse(_as_text(attributes[CF_UNITS_ATTRIBUTE]) or ""),
        calendar=calendar if calendar else CF_DEFAULT_CALENDAR,
    )


def _as_text(value: object) -> str | None:
    """
    Read an attribute value as text

    Parameters
    ----------
    value
        The value a header parser handed back, untouched

    Returns
    -------
    :
        The value as text, or `None` if there was no value

    Notes
    -----
    `bytes` is decoded rather than stringified, because a classic netCDF `NC_CHAR`
    attribute is bytes on the wire and `str(b"noleap")` is `"b'noleap'"` -- a calendar
    name no calendar library has heard of, arrived at silently.
    """
    if value is None:
        return None

    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")

    return str(value)
