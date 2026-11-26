r"""
What reading a file's header gives back
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from esmporium.downloads import Route

DEFAULT_TIME_VARIABLE = "time"
"""
The name of the time coordinate variable to read first and last values of

The name CMIP files give their time coordinate. A file which names it something else
can be read by passing that name.
"""


class HeaderLayout(str, Enum):
    """
    The on-disk layout of a file we can read a header out of
    """

    NETCDF3 = "netcdf3"
    r"""
    Classic netCDF or its 64-bit-offset variant: `CDF\x01` or `CDF\x02`

    Read by [esmporium.headers.netcdf3][], which needs nothing installed.
    """

    NETCDF4_HDF5 = "netcdf4_hdf5"
    r"""
    netCDF-4, which is HDF5: `\x89HDF\r\n\x1a\n`

    Read by [esmporium.headers.hdf5][], which needs the optional `headers` extra.
    """


class BoundsAttribute(str, Enum):
    """
    Which attribute of a time variable names the variable holding its bounds

    CF has two, and a time variable carries at most one of them. What the bounds mean
    is the same in both cases -- where each step starts and ends -- but which one was
    used says how the values were computed, so it is kept for whoever interprets them.
    """

    BOUNDS = "bounds"
    """
    `bounds`: each step's values describe that step

    E.g. a monthly mean, whose bounds are the start and end of its month.
    """

    CLIMATOLOGY = "climatology"
    """
    `climatology`: a statistic computed in two stages over time (CF section 7.4)

    The data variable's `cell_methods` says which two. A monthly mean of daily minima
    (`time: minimum within days time: mean over days`) still has one value per month,
    bounded by that month. A multi-year climatology
    (`time: mean within years time: mean over years`) has one value per month of a
    typical year, bounded by the whole period averaged over.
    """


def bounds_named_by(
    attributes: Mapping[str, object],
) -> tuple[BoundsAttribute, str] | None:
    """
    Get which variable holds a time variable's bounds, and which attribute said so

    Parameters
    ----------
    attributes
        The time variable's attributes

    Returns
    -------
    :
        The attribute and the variable it names, or `None` if it names neither kind

    Examples
    --------
    >>> bounds_named_by({"climatology": "climatology_bnds"})
    (<BoundsAttribute.CLIMATOLOGY: 'climatology'>, 'climatology_bnds')

    >>> print(bounds_named_by({"units": "days since 1850-01-01"}))
    None
    """
    for attribute in BoundsAttribute:
        name = attributes.get(attribute.value)
        if name is not None:
            return attribute, str(name)

    return None


@dataclass(frozen=True)
class FirstAndLastTimeBounds:
    """
    The first and last values of a file's time coordinate, and of its bounds

    Numbers exactly as the file stores them. What they count, and on which calendar,
    is said by the time variable's `units` and `calendar` attributes, which are in
    [FileHeader.variable_attributes][(m).FileHeader.variable_attributes].
    """

    time_variable: str
    """The name of the time coordinate variable these were read from"""

    n_steps: int
    """How many values the time coordinate has. At least one."""

    first_value: float | int
    """The time coordinate's first value"""

    last_value: float | int
    """The time coordinate's last value"""

    bounds_attribute: BoundsAttribute | None
    """
    Which of the time variable's attributes named its bounds variable

    `None` when it carries neither `bounds` nor `climatology`, which is usual for data
    reported at instants rather than over intervals.
    """

    bounds_variable: str | None
    """
    The name of the bounds variable, from the time variable's `bounds` or
    `climatology` attribute

    `None` when it carries neither.
    """

    first_bounds: tuple[float | int, float | int] | None
    """
    The first step's lower and upper bound, or `None` when there are no bounds

    The lower bound is where the file's data starts.
    """

    last_bounds: tuple[float | int, float | int] | None
    """
    The last step's lower and upper bound, or `None` when there are no bounds

    The upper bound is where the file's data ends.
    """


@dataclass(frozen=True)
class FileHeader:
    """
    Everything one read of a file's header found, and what the read cost
    """

    layout: HeaderLayout
    """Which on-disk layout the file's first bytes announced"""

    global_attributes: Mapping[str, object]
    """
    Every global attribute the file declares, exactly as it stores them

    Nothing is renamed or reinterpreted. What `parent_experiment_rip` means is a
    question about CMIP5, not about netCDF.
    """

    variable_attributes: Mapping[str, Mapping[str, object]]
    """
    Every variable's attributes, keyed by variable name, in the order the file
    declares them

    A variable with no attributes is present with an empty mapping, so this also says
    which variables the file has.
    """

    time_bounds: FirstAndLastTimeBounds | None
    """
    The first and last time values and bounds, when they were asked for

    `None` when they were not asked for, and when the file has no variable of the
    name asked for (a file of fixed fields has no time coordinate at all).
    """

    file_size: int
    """How long the whole file is, in bytes"""

    bytes_read: int
    """How many bytes of the file were fetched or read to produce this"""

    n_requests: int
    """
    How many separate fetches or reads of the file it took

    For a data node, one per HTTP range request that delivered bytes.
    """

    route: Route | None = None
    """
    The route that served the bytes, or `None` when they did not come from a data node
    """
