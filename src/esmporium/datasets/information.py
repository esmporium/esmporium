"""
Where a piece of information about a dataset came from, and what it rests on

Every fact esmporium records about a dataset's time names its source, because the
sources are not equally trustworthy and an answer built on a cheap one should say how
to check it. A fact that rests on a guess names the guess too.

Here rather than beside the parsers which produce these values because
[Covers][esmporium.requirements.Covers] names a source in its answers *at solve time*,
and `esmporium.requirements` may import `esmporium.datasets` and not
`esmporium.enrich`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from esmporium.datasets.time import TimeRange


class InformationSource(str, Enum):
    """
    Where a piece of information about a dataset came from

    A source is what was looked at, not how it was fetched: one read of a file's first
    few kilobytes gives both its [FILE_HEADER][(c).FILE_HEADER] and its
    [VARIABLE_ATTRIBUTES][(c).VARIABLE_ATTRIBUTES].
    """

    SEARCH_RECORD = "search_record"
    """
    The dataset version's own record in a search index

    Gives the first and last time *value* of the version, which are points rather than
    bounds, so a range read off them rests on
    [Assumption.POINTS_LIE_IN_STEPS_OF_THE_REPORTING_INTERVAL][(m).Assumption.POINTS_LIE_IN_STEPS_OF_THE_REPORTING_INTERVAL].
    Free: the records are stored when a search is saved.
    """

    FILENAME = "filename"
    """
    The name of one of the version's files

    Gives the first and last stamp of each file, so it is the one cheap source of gaps
    between files. Its half-open end rests on
    [Assumption.LAST_STEP_IS_ONE_REPORTING_INTERVAL][(m).Assumption.LAST_STEP_IS_ONE_REPORTING_INTERVAL].
    """

    FILE_HEADER = "file_header"
    """
    A file's global attributes

    Gives the parent and the branch times. No start or end: no CMIP5 or CMIP6 file
    sampled carries one.
    """

    VARIABLE_ATTRIBUTES = "variable_attributes"
    """
    The attributes of a file's time variable

    Gives the time units, the calendar and the name of the bounds variable. Nothing
    else in a file or an index records the calendar, so there is nothing to check this
    against and it is believed unless fixed.
    """

    VARIABLE_VALUES = "variable_values"
    """
    The first and last values of a file's time coordinate and its bounds

    The data itself: the first lower bound is where a file's data starts and the last
    upper bound is where it ends, with no assumption about how wide a step is.
    """

    FIX = "fix"
    """
    A correction we wrote ourselves, with a reason and evidence

    Outranks everything published.
    """


TIME_RANGE_TRUST_ORDER: tuple[InformationSource, ...] = (
    InformationSource.FIX,
    InformationSource.VARIABLE_VALUES,
    InformationSource.FILENAME,
    InformationSource.SEARCH_RECORD,
)
"""
The sources which can say what a version's data spans, most trusted first

A fix beats the data, and the data beats what was published about it. Of the two
published sources the filename comes first because it can see a gap between files,
which a version's search record cannot.

[FILE_HEADER][(m).InformationSource.FILE_HEADER] and
[VARIABLE_ATTRIBUTES][(m).InformationSource.VARIABLE_ATTRIBUTES] are absent because
neither says anything about a range.
"""


class NotATimeRangeSourceError(ValueError):
    """
    Raised when a source which says nothing about a range is ranked as though it did
    """

    def __init__(self, source: InformationSource) -> None:
        """
        Initialise the error

        Parameters
        ----------
        source
            The source which was ranked
        """
        self.source = source
        ranked = ", ".join(each.value for each in TIME_RANGE_TRUST_ORDER)
        super().__init__(
            f"{source.value!r} does not say what a version's data spans, so it has no "
            f"place in the trust order for time ranges. The sources which do are, "
            f"most trusted first: {ranked}."
        )


def time_range_trust_rank(source: InformationSource) -> int:
    """
    Get how trusted a source is about a time range

    Parameters
    ----------
    source
        The source

    Returns
    -------
    :
        Its position in [TIME_RANGE_TRUST_ORDER][(m).TIME_RANGE_TRUST_ORDER], so a
        smaller number is more trusted

    Raises
    ------
    NotATimeRangeSourceError
        `source` says nothing about a range

    Examples
    --------
    >>> time_range_trust_rank(InformationSource.VARIABLE_VALUES) < (
    ...     time_range_trust_rank(InformationSource.FILENAME)
    ... )
    True
    """
    try:
        return TIME_RANGE_TRUST_ORDER.index(source)
    except ValueError as error:
        raise NotATimeRangeSourceError(source) from error


class Assumption(str, Enum):
    """
    A guess a value rests on, named so that every answer built on it can say so

    A cheap source gives a value only with one of these attached, and a more trusted
    source is what checks it.
    """

    LAST_STEP_IS_ONE_REPORTING_INTERVAL = "last_step_is_one_reporting_interval"
    """
    The data runs to the end of the step its last stamp names

    How a filename's inclusive last stamp becomes a half-open end: a monthly file
    ending `201412` is taken to run to the start of January 2015. True when the time
    bounds are contiguous and each as wide as the reporting interval says; only a
    convention for point data (`...Pt`), whose values are instants. Checked by the last
    upper time bound.
    """

    POINTS_LIE_IN_STEPS_OF_THE_REPORTING_INTERVAL = (
        "points_lie_in_steps_of_the_reporting_interval"
    )
    """
    Each published time value lies inside its own step, one reporting interval wide

    How a search record's first and last time *values* become a range: a first value of
    `1850-01-16T12:00:00Z` on monthly data is taken to mean the data starts in January
    1850. Checked by the first lower and last upper time bound.
    """

    PART_STEPS_COUNT_AS_COVERED = "part_steps_count_as_covered"
    """
    A step the data covers only part of is counted as covered

    How time bounds which do not fall on the edges of a month (or a year) become a
    range of whole months (or years): the range is widened to the edges, so a run
    whose last upper bound is 16 December 2014 is taken to cover December. Only named
    on a range where widening actually happened. The dated range read off the same
    bounds says exactly where the data starts and ends.
    """


class NoRangeReason(str, Enum):
    """
    Why a source which can say what some data spans did not, this time
    """

    FINER_THAN_A_MONTH = "finer_than_a_month"
    """
    The source is written to the day or finer and carries no calendar

    Only years and months have a calendar-free count to place a range on, and two
    dates are a length only once the calendar is known.
    """

    CLIMATOLOGY = "climatology"
    """
    The data is a multi-year climatology, so its time range is the period averaged over

    E.g. the average January, February and so on over 1850 to 1859: twelve values
    describing a typical year, which are not a ten-year run of data. Marked in the file
    by climatology bounds and a `cell_methods` taking a statistic over years, and in
    CMIP filenames by a `-clim` suffix.

    Not every statistic with climatology bounds is one of these. A monthly mean of
    daily minima has climatology bounds too, one month wide each, and is an ordinary
    run of monthly data.
    """

    CLIMATOLOGY_OF_AN_UNSTATED_KIND = "climatology_of_an_unstated_kind"
    """
    The time bounds are climatology bounds, and nothing says which kind of climatology

    A multi-year climatology's bounds are the period averaged over, and another
    climatological statistic's are the time its data covers, so the bounds cannot be
    read as a range until the data variable's `cell_methods` says which it is.
    """

    NO_BOUNDS = "no_bounds"
    """
    The time variable has no bounds, so where its data starts and ends is not recorded

    Usual for data reported at instants rather than over intervals. Making a range out
    of the values would need a guess about how wide a step is, and the time values are
    read precisely to avoid one.
    """

    UNREADABLE = "unreadable"
    """
    The source has a value where the range should be, and it is not one we read
    """


@dataclass(frozen=True)
class SourcedTimeRange:
    """
    A time range, with where it came from and what it rests on
    """

    range: TimeRange
    """The range"""

    source: InformationSource
    """Where it came from"""

    assumption: Assumption | None = None
    """
    The guess it rests on, or `None` when it rests on none

    `None` for a range read off time bounds whose ends fall on the edges of the
    range's steps, and for a fix.
    """
