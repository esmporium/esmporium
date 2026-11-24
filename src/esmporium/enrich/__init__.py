"""
Enrichment: recording the time information ESGF's search APIs may not publish

How long a dataset's data runs for, in what units and calendar, and where a run
branched from its parent. None of it is guaranteed to be published, so esmporium
fills the gap itself, between searching and solving:

```text
Requirement --> search --> database --> ENRICH --> solve --> outcome
```
"""

from esmporium.enrich.combine_files_to_datasets import (
    NoFilesToCombineError,
    combine_file_ranges,
)
from esmporium.enrich.headers import (
    NoCalendarError,
    NoTimeUnitsError,
    read_time_axis,
)
from esmporium.enrich.known_readers import (
    CLIMATOLOGY_SUFFIX,
    FILENAME_TIME_READERS_BY_PROJECT,
    SPAN_BY_REPORTING_INTERVAL,
    STEP_BY_STAMP_LENGTH,
    UnknownProjectFilenameTimeReaderError,
    UnknownReportingIntervalError,
    read_trailing_range,
    reader_for_project,
    span_of_reporting_interval,
    step_of_stamp,
)
from esmporium.enrich.protocol import (
    CalendarRequiredError,
    FilenameTimeReader,
    PublishedRange,
    StampPrecisionMismatchError,
    UnreadableTimestampError,
)

__all__ = [
    "CLIMATOLOGY_SUFFIX",
    "FILENAME_TIME_READERS_BY_PROJECT",
    "SPAN_BY_REPORTING_INTERVAL",
    "STEP_BY_STAMP_LENGTH",
    "CalendarRequiredError",
    "FilenameTimeReader",
    "NoCalendarError",
    "NoFilesToCombineError",
    "NoTimeUnitsError",
    "PublishedRange",
    "StampPrecisionMismatchError",
    "UnknownProjectFilenameTimeReaderError",
    "UnknownReportingIntervalError",
    "UnreadableTimestampError",
    "combine_file_ranges",
    "read_time_axis",
    "read_trailing_range",
    "reader_for_project",
    "span_of_reporting_interval",
    "step_of_stamp",
]
