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
from esmporium.enrich.consistency import (
    BranchTimeInChildDisagreement,
    NotComparable,
    NotLessTrustedError,
    TimeRangeDisagreement,
    compare_branch_time_in_child,
    compare_time_ranges,
)
from esmporium.enrich.file_header import (
    FILE_HEADER_PARSERS_BY_PROJECT,
    NOT_SAID,
    FileHeaderParser,
    UnknownProjectFileHeaderParserError,
    file_header_parser_for_project,
    parse_cmip5_file_header,
    parse_cmip6_file_header,
    parse_file_header,
)
from esmporium.enrich.filename import ParsedFilename, parse_filename
from esmporium.enrich.known_parsers import (
    CLIMATOLOGY_INTERVALS_BY_PROJECT,
    CLIMATOLOGY_SUFFIX,
    DELTA_BY_REPORTING_INTERVAL,
    FILENAME_TIME_PARSERS_BY_PROJECT,
    STEP_BY_STAMP_LENGTH,
    UnknownProjectClimatologyIntervalsError,
    UnknownProjectFilenameTimeParserError,
    UnknownReportingIntervalError,
    delta_of_reporting_interval,
    is_climatology_interval,
    parse_trailing_range,
    parser_for_project,
    step_of_stamp,
)
from esmporium.enrich.protocol import (
    CalendarRequiredError,
    FilenameTimeParser,
    PublishedRange,
    StampPrecisionMismatchError,
    UnreadableTimestampError,
)
from esmporium.enrich.variable_attributes import (
    NoCalendarError,
    NoTimeUnitsError,
    ParsedTimeVariable,
    parse_variable_attributes,
)
from esmporium.enrich.variable_values import (
    ON_A_STEP_EDGE_WITHIN_SECONDS,
    ParsedTimeValues,
    natural_step_for,
    parse_variable_values,
)

__all__ = [
    "CLIMATOLOGY_INTERVALS_BY_PROJECT",
    "CLIMATOLOGY_SUFFIX",
    "DELTA_BY_REPORTING_INTERVAL",
    "FILENAME_TIME_PARSERS_BY_PROJECT",
    "FILE_HEADER_PARSERS_BY_PROJECT",
    "NOT_SAID",
    "ON_A_STEP_EDGE_WITHIN_SECONDS",
    "STEP_BY_STAMP_LENGTH",
    "BranchTimeInChildDisagreement",
    "CalendarRequiredError",
    "FileHeaderParser",
    "FilenameTimeParser",
    "NoCalendarError",
    "NoFilesToCombineError",
    "NoTimeUnitsError",
    "NotComparable",
    "NotLessTrustedError",
    "ParsedFilename",
    "ParsedTimeValues",
    "ParsedTimeVariable",
    "PublishedRange",
    "StampPrecisionMismatchError",
    "TimeRangeDisagreement",
    "UnknownProjectClimatologyIntervalsError",
    "UnknownProjectFileHeaderParserError",
    "UnknownProjectFilenameTimeParserError",
    "UnknownReportingIntervalError",
    "UnreadableTimestampError",
    "combine_file_ranges",
    "compare_branch_time_in_child",
    "compare_time_ranges",
    "delta_of_reporting_interval",
    "file_header_parser_for_project",
    "is_climatology_interval",
    "natural_step_for",
    "parse_cmip5_file_header",
    "parse_cmip6_file_header",
    "parse_file_header",
    "parse_filename",
    "parse_trailing_range",
    "parse_variable_attributes",
    "parse_variable_values",
    "parser_for_project",
    "step_of_stamp",
]
