"""
A runnable example of the value/typo checker: wrong queries -> live sources -> a report

Sits BESIDE the search demo (`first_search_cmipx_full.py`), the same way the
checker sits beside `search()`: a few deliberately-wrong queries, each checked
against the right vocabulary source for its project and printed as a report.
The checker itself now lives in `esmporium.search`; this is only a hand-run
example of calling it.

Hits the live Solr nodes (CMIP5/6) and GitHub (the CMIP7 CV), like the search
demo does, so it needs a network.

Run it:  uv run python scripts/check_query_values_demo.py
"""

from __future__ import annotations

from esmporium.query import QueryCMIP5, QueryCMIP6, QueryCMIP7, QueryProtocol
from esmporium.search import ValueReport, check_query_values


def print_report(query: QueryProtocol, report: ValueReport) -> None:
    """Print one query's check as a short, human-readable report."""
    print(f"query   : {query!r}")
    print(f"project : {report.project}   source: {report.source or '(none)'}")
    if report.ok():
        print("result  : no problems found")
    for finding in report.findings:
        hint = (
            f"did you mean {', '.join(finding.suggestions)}?"
            if finding.suggestions
            else "no close match found"
        )
        print(f"  [{finding.kind:7}] {finding.facet}={finding.value!r} -> {hint}")
    if report.unchecked:
        print(f"unchecked: {', '.join(report.unchecked)}")
    print()


# CMIP5 experiment typo: hyphen + lower case; the real value is "abrupt4xCO2".
EXAMPLE_CMIP5 = QueryCMIP5(experiment="abrupt-4xco2", variable="tas")
# CMIP6 case slip: the real value is lower-case "historical".
EXAMPLE_CMIP6 = QueryCMIP6(experiment_id="Historical", variable_id="tas")
# CMIP7 (checked via the CV): truncated experiment; the real value is
# "abrupt-4xCO2" (hyphen + capital CO2 -- note it differs from CMIP5's spelling).
EXAMPLE_CMIP7 = QueryCMIP7(experiment_id="abrupt-4x")


def main() -> None:
    """Check each example query and print the report."""
    for example in (EXAMPLE_CMIP5, EXAMPLE_CMIP6, EXAMPLE_CMIP7):
        print_report(example, check_query_values(example))
        print("=" * 72 + "\n")


if __name__ == "__main__":
    main()
