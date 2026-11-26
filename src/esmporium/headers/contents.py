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

    bounds_variable: str | None
    """
    The name of the bounds variable, from the time variable's `bounds` attribute

    `None` when the time variable has no `bounds` attribute, which is usual for data
    reported at instants rather than over intervals.
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
