"""
Parsing everything a filename says about time
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from esmporium.datasets.information import (
    Assumption,
    InformationSource,
    NoRangeReason,
    SourcedTimeRange,
)
from esmporium.enrich.known_parsers import (
    CLIMATOLOGY_INTERVALS_BY_PROJECT,
    CLIMATOLOGY_SUFFIX,
    DELTA_BY_REPORTING_INTERVAL,
    FILENAME_TIME_PARSERS_BY_PROJECT,
    STEP_BY_STAMP_LENGTH,
    delta_of_reporting_interval,
    is_climatology_interval,
    parser_for_project,
    step_of_stamp,
)
from esmporium.enrich.protocol import StampPrecisionMismatchError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from esmporium.datasets.time import TimeDelta, TimeStep
    from esmporium.enrich.protocol import FilenameTimeParser, PublishedRange


@dataclass(frozen=True)
class ParsedFilename:
    """
    Everything a filename says about time
    """

    published: PublishedRange
    """The stamps and any suffix, exactly as the filename wrote them"""

    precision: TimeStep
    """How precisely the stamps are written, read off their length"""

    natural: SourcedTimeRange | None
    """
    The range on the natural axis of `precision`, half-open

    Rests on
    [Assumption.LAST_STEP_IS_ONE_REPORTING_INTERVAL][esmporium.datasets.Assumption.LAST_STEP_IS_ONE_REPORTING_INTERVAL].
    `None` when there is no natural range to give, and `no_range_because` says why.
    """

    no_range_because: NoRangeReason | None
    """
    Why `natural` is `None`, or `None` when it is not

    [FINER_THAN_A_MONTH][esmporium.datasets.NoRangeReason.FINER_THAN_A_MONTH] for a
    daily or finer stamp, and
    [CLIMATOLOGY][esmporium.datasets.NoRangeReason.CLIMATOLOGY] for a multi-year
    climatology, labelled by a `-clim` suffix or by its reporting interval, whose
    stamps are the period averaged over rather than a run of data.
    """


def parse_filename(  # noqa: PLR0913 - one argument per injection seam
    filename: str,
    *,
    project: str,
    reporting_interval: str,
    parsers: Mapping[str, FilenameTimeParser] = FILENAME_TIME_PARSERS_BY_PROJECT,
    deltas: Mapping[str, TimeDelta] = DELTA_BY_REPORTING_INTERVAL,
    steps: Mapping[int, TimeStep] = STEP_BY_STAMP_LENGTH,
    climatology_intervals: Mapping[str, frozenset[str]] = (
        CLIMATOLOGY_INTERVALS_BY_PROJECT
    ),
) -> ParsedFilename | None:
    """
    Parse everything a filename says about time

    Parameters
    ----------
    filename
        The filename, with its extension, and no directories

    project
        The project the file belongs to, which decides where its filenames put a range

    reporting_interval
        The dataset's reporting interval, e.g. `mon`, which says how wide the last
        step is taken to be

    parsers
        Which parser reads each project's filenames

    deltas
        How wide one step of each reporting interval is taken to be

    steps
        The precision each length of stamp is written to

    climatology_intervals
        Which reporting intervals mean a multi-year climatology, per project

    Returns
    -------
    :
        What the filename says, or `None` if it carries no range at all

        `None` is the ordinary answer for a file of fixed fields, and is not an error.

    Raises
    ------
    UnknownProjectFilenameTimeParserError
        We have no parser for `project`

    UnknownProjectClimatologyIntervalsError
        We do not know which of `project`'s reporting intervals are climatologies

    UnknownReportingIntervalError
        We do not know how wide one step of `reporting_interval` is

    UnreadableTimestampError
        A stamp is not a shape we read

    StampPrecisionMismatchError
        The two stamps are written to different precisions

    Examples
    --------
    >>> parsed = parse_filename(
    ...     "tas_Amon_HadGEM2-ES_historical_r1i1p1_185912-188411.nc",
    ...     project="CMIP5",
    ...     reporting_interval="mon",
    ... )
    >>> str(parsed.natural.range.extent), parsed.natural.assumption.value
    ('300 months', 'last_step_is_one_reporting_interval')

    A daily filename keeps its stamps, and says why it gives no range:

    >>> daily = parse_filename(
    ...     "tas_day_CanESM5_historical_r1i1p1f1_gn_18500101-18601231.nc",
    ...     project="CMIP6",
    ...     reporting_interval="day",
    ... )
    >>> daily.published.start_raw, daily.natural, daily.no_range_because.value
    ('18500101', None, 'finer_than_a_month')

    >>> print(
    ...     parse_filename(
    ...         "areacella_fx_HadGEM2-ES_historical_r0i0p0.nc",
    ...         project="CMIP5",
    ...         reporting_interval="fx",
    ...     )
    ... )
    None
    """
    published = parser_for_project(project, parsers)(filename)
    if published is None:
        return None

    precision = step_of_stamp(published.start_raw, steps)
    if step_of_stamp(published.end_raw, steps) != precision:
        raise StampPrecisionMismatchError(published.end_raw, precision)

    if published.suffix == CLIMATOLOGY_SUFFIX or is_climatology_interval(
        project, reporting_interval, climatology_intervals
    ):
        return ParsedFilename(
            published=published,
            precision=precision,
            natural=None,
            no_range_because=NoRangeReason.CLIMATOLOGY,
        )

    if not precision.is_calendar_free:
        return ParsedFilename(
            published=published,
            precision=precision,
            natural=None,
            no_range_because=NoRangeReason.FINER_THAN_A_MONTH,
        )

    natural = published.to_half_open(
        precision=precision,
        reports_every=delta_of_reporting_interval(reporting_interval, deltas),
    )

    return ParsedFilename(
        published=published,
        precision=precision,
        natural=SourcedTimeRange(
            range=natural,
            source=InformationSource.FILENAME,
            assumption=Assumption.LAST_STEP_IS_ONE_REPORTING_INTERVAL,
        ),
        no_range_because=None,
    )
