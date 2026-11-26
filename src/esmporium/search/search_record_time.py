"""
Parsing what a dataset version's search record says about time

Every search API we query publishes the first and last time *value* of a version on its
record. They are points, not bounds (`1850-01-16T12:00:00Z` is the middle of January
1850), and they carry no calendar, so the range read off them rests on an assumption
and is counted in whole months or years only.

Which fields carry them is the format's own convention, so it is looked up by the
format tag every stored record carries
([DatasetRawDoc.raw_docs_format_tag][esmporium.db.schema.DatasetRawDoc.raw_docs_format_tag]),
which means a record stored long ago parses the same way as one just fetched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from esmporium.datasets.information import (
    Assumption,
    InformationSource,
    NoRangeReason,
    SourcedTimeRange,
)
from esmporium.datasets.time import (
    TimeAxis,
    TimePoint,
    TimeRange,
    TimeStep,
    TimeUnits,
)
from esmporium.search.result_normalisation import (
    DEFAULT_NORMALISERS,
    SOLR_FORMAT_TAG,
    STAC_FORMAT_TAG,
    normalise_stored_document,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from esmporium.datasets.time import TimeDelta
    from esmporium.search.result_normalisation import NormaliseFunc


@dataclass(frozen=True)
class SearchRecordTimeFields:
    """
    Which fields of a search record carry its first and last time value
    """

    start: tuple[str, ...]
    """
    The fields which may carry the first time value, tried in order
    """

    end: tuple[str, ...]
    """
    The fields which may carry the last time value, tried in order

    More than one where one format is written by more than one index that spells the
    field differently.
    """


SEARCH_RECORD_TIME_FIELDS_BY_FORMAT_TAG: Mapping[str, SearchRecordTimeFields] = {
    SOLR_FORMAT_TAG: SearchRecordTimeFields(
        start=("datetime_start",),
        # ESGF-1's Solr writes `datetime_stop` and the ESGF-1.5 bridge writes
        # `datetime_end`. Both are stored with the Solr format tag, so both are read.
        end=("datetime_stop", "datetime_end"),
    ),
    STAC_FORMAT_TAG: SearchRecordTimeFields(
        start=("start_datetime",),
        end=("end_datetime",),
    ),
}
"""
Which fields carry a record's first and last time value, by its format tag

Read after the record has been flattened with
[normalise_stored_document][esmporium.search.normalise_stored_document], so a STAC
field is named without its `properties.` path. Pass a mapping of your own to add a
format, e.g. `{**SEARCH_RECORD_TIME_FIELDS_BY_FORMAT_TAG, "my_format": ...}`.
"""


class UnknownSearchRecordTimeFormatError(KeyError):
    """
    Raised when a search record's format has no time fields registered
    """

    def __init__(self, tag: str, fields: Mapping[str, SearchRecordTimeFields]) -> None:
        """
        Initialise the error

        Parameters
        ----------
        tag
            The format tag

        fields
            The formats which do have time fields registered
        """
        self.tag = tag
        self.known = tuple(sorted(fields))
        known = ", ".join(repr(name) for name in self.known) or "(none)"
        super().__init__(
            f"No time fields are registered for raw_docs_format_tag {tag!r}. Known "
            f"tags: {known}. Pass `fields={{**SEARCH_RECORD_TIME_FIELDS_BY_FORMAT_TAG, "
            f"{tag!r}: SearchRecordTimeFields(...)}}`."
        )


@dataclass(frozen=True)
class ParsedSearchRecordTime:
    """
    What a dataset version's search record says about time
    """

    start_raw: str | None
    """The first time value, exactly as the record wrote it, or `None` if absent"""

    end_raw: str | None
    """The last time value, exactly as the record wrote it, or `None` if absent"""

    natural: SourcedTimeRange | None
    """
    The range on a natural axis of the reporting step, half-open

    From the step the first value lies in to the step after the one the last value
    lies in. Rests on
    [Assumption.POINTS_LIE_IN_STEPS_OF_THE_REPORTING_INTERVAL][esmporium.datasets.Assumption.POINTS_LIE_IN_STEPS_OF_THE_REPORTING_INTERVAL].
    `None` when there is no range to give, and `no_range_because` says why.
    """

    no_range_because: NoRangeReason | None
    """
    Why `natural` is `None`, or `None` when it is not

    [FINER_THAN_A_MONTH][esmporium.datasets.NoRangeReason.FINER_THAN_A_MONTH] when the
    data is reported daily or more often, and
    [UNREADABLE][esmporium.datasets.NoRangeReason.UNREADABLE] when a value is not one
    we read.
    """


_YEAR_AND_MONTH = re.compile(r"^\s*(?P<year>\d{4,})-(?P<month>\d{2})")
"""
The year and month at the start of an ISO 8601 date

Matched as text rather than parsed as a date, because a model's calendar may hold dates
the proleptic Gregorian calendar does not, e.g. 30 February on a `360_day` calendar.
"""


def parse_search_record(
    record: dict[str, Any],
    *,
    raw_docs_format_tag: str,
    reports_every: TimeDelta,
    fields: Mapping[str, SearchRecordTimeFields] = (
        SEARCH_RECORD_TIME_FIELDS_BY_FORMAT_TAG
    ),
    normalisers: Mapping[str, NormaliseFunc] = DEFAULT_NORMALISERS,
) -> ParsedSearchRecordTime | None:
    """
    Parse what a dataset version's search record says about time

    Parameters
    ----------
    record
        One raw search record, as the search API returned it

    raw_docs_format_tag
        The format the record is in, as
        [DatasetRawDoc.raw_docs_format_tag][esmporium.db.schema.DatasetRawDoc.raw_docs_format_tag]
        records it

    reports_every
        How wide one step of the dataset's reporting interval is, e.g.
        `TimeDelta(1, TimeStep.MONTHS)`

    fields
        Which fields carry the first and last time value, per format

    normalisers
        How to flatten a record, per format

    Returns
    -------
    :
        What the record says, or `None` if it carries neither value

        A value that cannot be read costs the range, not the record: this is metadata
        written by many indexes over many years.

    Raises
    ------
    UnknownSearchRecordTimeFormatError
        `raw_docs_format_tag` has no time fields in `fields`

    UnknownRawDocFormatTagError
        `raw_docs_format_tag` has no flattener in `normalisers`

    Examples
    --------
    >>> from esmporium.datasets import TimeDelta, TimeStep
    >>> parsed = parse_search_record(
    ...     {
    ...         "datetime_start": "1850-01-16T12:00:00Z",
    ...         "datetime_stop": "2014-12-16T12:00:00Z",
    ...     },
    ...     raw_docs_format_tag="solr",
    ...     reports_every=TimeDelta(1, TimeStep.MONTHS),
    ... )
    >>> str(parsed.natural.range.extent), parsed.natural.assumption.value
    ('1980 months', 'points_lie_in_steps_of_the_reporting_interval')

    Daily data keeps its values and gives no range, because there is no calendar to
    count days in:

    >>> daily = parse_search_record(
    ...     {
    ...         "datetime_start": "1850-01-01T12:00:00Z",
    ...         "datetime_end": "2014-12-31T12:00:00Z",
    ...     },
    ...     raw_docs_format_tag="solr",
    ...     reports_every=TimeDelta(1, TimeStep.DAYS),
    ... )
    >>> daily.end_raw, daily.no_range_because.value
    ('2014-12-31T12:00:00Z', 'finer_than_a_month')
    """
    if raw_docs_format_tag not in fields:
        raise UnknownSearchRecordTimeFormatError(raw_docs_format_tag, fields)

    flat = normalise_stored_document(record, raw_docs_format_tag, normalisers)
    start_raw = _first_present(flat, fields[raw_docs_format_tag].start)
    end_raw = _first_present(flat, fields[raw_docs_format_tag].end)
    if start_raw is None and end_raw is None:
        return None

    step = reports_every.step
    if not step.is_calendar_free:
        return ParsedSearchRecordTime(
            start_raw=start_raw,
            end_raw=end_raw,
            natural=None,
            no_range_because=NoRangeReason.FINER_THAN_A_MONTH,
        )

    axis = TimeAxis(TimeUnits(step))
    start = None if start_raw is None else _step_of_date(start_raw, axis)
    last = None if end_raw is None else _step_of_date(end_raw, axis)
    if start is None or last is None or last.plus(reports_every).value <= start.value:
        return ParsedSearchRecordTime(
            start_raw=start_raw,
            end_raw=end_raw,
            natural=None,
            no_range_because=NoRangeReason.UNREADABLE,
        )

    return ParsedSearchRecordTime(
        start_raw=start_raw,
        end_raw=end_raw,
        natural=SourcedTimeRange(
            range=TimeRange(start=start, end_exclusive=last.plus(reports_every)),
            source=InformationSource.SEARCH_RECORD,
            assumption=Assumption.POINTS_LIE_IN_STEPS_OF_THE_REPORTING_INTERVAL,
        ),
        no_range_because=None,
    )


def _first_present(flat: Mapping[str, Any], names: tuple[str, ...]) -> str | None:
    """
    Get the first of several fields which has a value, as text

    Parameters
    ----------
    flat
        The flattened record

    names
        The fields to try, in order

    Returns
    -------
    :
        The value, or `None` if none of them has one
    """
    for name in names:
        value = flat.get(name)
        if value is not None and str(value).strip():
            return str(value)

    return None


def _step_of_date(text: str, axis: TimeAxis) -> TimePoint | None:
    """
    Get the step of a natural axis an ISO 8601 date lies in

    Parameters
    ----------
    text
        The date, e.g. `"1850-01-16T12:00:00Z"`

    axis
        The natural axis, yearly or monthly

    Returns
    -------
    :
        The step, or `None` if `text` does not start with a year and a month
    """
    matched = _YEAR_AND_MONTH.match(text)
    if matched is None:
        return None

    year, month = int(matched["year"]), int(matched["month"])
    if not 1 <= month <= 12:  # noqa: PLR2004 - the months of a year
        return None

    return TimePoint.from_year_and_month(
        axis, year=year, month=month if axis.units.step == TimeStep.MONTHS else None
    )
