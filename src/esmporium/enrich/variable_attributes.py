"""
Parsing what a file's time variable says about itself

Project-independent, and deliberately so: `units`, `calendar`, `bounds` and
`climatology` are CF attributes, so the formats are per-file, not per-project

Note what the input is. These are the attributes of the file's **time variable**, not
its global attributes: a CMIP file does not publish its own calendar globally.
[esmporium.headers][] reads them
([read_variable_attributes][esmporium.headers.read_variable_attributes], or
[FileHeader.variable_attributes][esmporium.headers.FileHeader.variable_attributes]);
[parse_variable_attributes][(m).parse_variable_attributes] is the pure half, and takes
whatever that returns.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from esmporium.datasets.time import TimeAxis, TimeUnits

if TYPE_CHECKING:
    from collections.abc import Mapping

CF_UNITS_ATTRIBUTE = "units"
"""The CF attribute naming what a time coordinate's values count"""

CF_CALENDAR_ATTRIBUTE = "calendar"
"""The CF attribute naming which calendar a time coordinate's values are in"""

CF_BOUNDS_ATTRIBUTE = "bounds"
"""The CF attribute naming the variable which holds a coordinate's cell bounds"""

CF_CLIMATOLOGY_ATTRIBUTE = "climatology"
"""
The CF attribute naming the variable which holds a time coordinate's climatology
bounds

Carried instead of `bounds` by a statistic computed in two stages over time (CF
conventions, section 7.4), of which there are two kinds. A multi-year climatology, e.g.
the average January over 1961 to 1990, has bounds spanning the whole period averaged
over. A statistic taken within a shorter period, e.g. a monthly mean of daily minima,
has bounds which are each month's own. The data variable's `cell_methods` says which.
"""


@dataclass(frozen=True)
class ParsedTimeVariable:
    """
    What a file's time variable says about itself
    """

    axis: TimeAxis
    """The units and calendar its values are on"""

    bounds_variable: str | None
    """
    The variable holding its cell bounds, from its `bounds` attribute

    `None` when it has none, which is usual for data reported at instants rather than
    over intervals.
    """

    climatology_variable: str | None
    """
    The variable holding its climatology bounds, from its `climatology` attribute

    `None` unless the data is a statistic computed in two stages over time. See
    [CF_CLIMATOLOGY_ATTRIBUTE][(m).CF_CLIMATOLOGY_ATTRIBUTE].
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


class NoCalendarError(ValueError):
    """
    Raised when a time variable's attributes do not say which calendar it is on
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
            f"These attributes do not include a {CF_CALENDAR_ATTRIBUTE!r}, so they do "
            f"not say which calendar the time values are in. Attributes found: "
            f"{found}. CF reads a missing calendar as 'standard', but that is a rule "
            f"for reading a file rather than evidence of what the model ran, so it is "
            f"not assumed here. If the calendar is known from elsewhere, record it as "
            f"a fix."
        )


def parse_variable_attributes(attributes: Mapping[str, object]) -> ParsedTimeVariable:
    """
    Parse everything a time variable's attributes say about it

    Parameters
    ----------
    attributes
        The attributes of the file's time variable, names and values untouched

    Returns
    -------
    :
        The axis its values are on, and which variables hold its bounds

    Raises
    ------
    NoTimeUnitsError
        The attributes do not include `units`

    NoCalendarError
        The attributes do not include `calendar`, or it is empty

    UnknownTimeUnitsError
        The `units` attribute is not a string we read

    Examples
    --------
    >>> parsed = parse_variable_attributes(
    ...     {
    ...         "units": "days since 1850-01-01",
    ...         "calendar": "noleap",
    ...         "bounds": "time_bnds",
    ...     }
    ... )
    >>> str(parsed.axis.units), parsed.axis.calendar, parsed.bounds_variable
    ('days since 1850-01-01', 'noleap', 'time_bnds')

    A file which does not name a calendar says so, listing what it did name:

    >>> try:
    ...     parse_variable_attributes({"units": "days since 1850-01-01", "axis": "T"})
    ... except NoCalendarError as error:
    ...     sorted(error.attributes)
    ['axis', 'units']
    """
    if CF_UNITS_ATTRIBUTE not in attributes:
        raise NoTimeUnitsError(attributes)

    units = TimeUnits.parse(_as_text(attributes[CF_UNITS_ATTRIBUTE]) or "")

    calendar = _as_text(attributes.get(CF_CALENDAR_ATTRIBUTE))
    if not calendar:
        raise NoCalendarError(attributes)

    return ParsedTimeVariable(
        axis=TimeAxis(units=units, calendar=calendar),
        bounds_variable=_as_text(attributes.get(CF_BOUNDS_ATTRIBUTE)) or None,
        climatology_variable=(
            _as_text(attributes.get(CF_CLIMATOLOGY_ATTRIBUTE)) or None
        ),
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
