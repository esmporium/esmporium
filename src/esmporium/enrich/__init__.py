"""
Enrichment: recording the time information ESGF's search APIs may not publish

How long a dataset's data runs for, in what units and calendar, and where a run
branched from its parent. None of it is guaranteed to be published, so esmporium
fills the gap itself, between searching and solving:

```text
Requirement --> search --> database --> ENRICH --> solve --> outcome
```
"""

from esmporium.enrich.headers import (
    CF_DEFAULT_CALENDAR,
    NoTimeUnitsError,
    read_time_axis,
)
from esmporium.enrich.known_readers import (
    FILENAME_TIME_READERS_BY_PROJECT,
    SPAN_BY_REPORTING_INTERVAL,
    UnknownProjectFilenameTimeReaderError,
    UnknownReportingIntervalError,
    read_trailing_range,
    reader_for_project,
    span_of_reporting_interval,
)
from esmporium.enrich.measure import NoFilesToMeasureError, measure_version
from esmporium.enrich.protocol import (
    CalendarRequiredError,
    FilenameTimeReader,
    PublishedRange,
    UnreadableTimestampError,
    step_of_stamp,
)

__all__ = [
    "CF_DEFAULT_CALENDAR",
    "FILENAME_TIME_READERS_BY_PROJECT",
    "SPAN_BY_REPORTING_INTERVAL",
    "CalendarRequiredError",
    "FilenameTimeReader",
    "NoFilesToMeasureError",
    "NoTimeUnitsError",
    "PublishedRange",
    "UnknownProjectFilenameTimeReaderError",
    "UnknownReportingIntervalError",
    "UnreadableTimestampError",
    "measure_version",
    "read_time_axis",
    "read_trailing_range",
    "reader_for_project",
    "span_of_reporting_interval",
    "step_of_stamp",
]
