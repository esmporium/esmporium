"""
A runnable example of the search step: QueryCMIP{5,6,7} -> live ESGF -> raw JSON

Everything that does the work now lives in `esmporium.search`; this is only a
hand-run example of calling it. Logging is turned up to `DEBUG` so that the
URL- and `curl`-equivalent of each request (and the process/thread it went out
on) are printed as the search runs.

Run it:  uv run python scripts/first_search_cmipx_full.py
"""

from __future__ import annotations

import logging
from typing import Any

from esmporium.query import QueryCMIP5, QueryCMIP6, QueryCMIP7
from esmporium.search import search

EXAMPLE_CMIP5 = QueryCMIP5(
    experiment="historical",
    variable="tas",
    time_frequency="mon",
    ensemble="r1i1p1",
)

# CMIP6 in its own dialect: experiment_id / variable_id / frequency.
EXAMPLE_CMIP6 = QueryCMIP6(
    experiment_id="historical",
    variable_id="tas",
    frequency="mon",
)

# CMIP7 in its own dialect. Data is sparse, so we keep the query broad.
EXAMPLE_CMIP7 = QueryCMIP7(
    variable_id="tas",
)


def node_count_summary(raw: dict[str, Any]) -> str:
    """Summarise a raw response's match count without knowing its generation."""
    if "response" in raw:  # Solr-shaped (ESGF1 esg-search or the ESGF-1.5 bridge)
        return f"numFound={raw['response'].get('numFound')}"
    return f"numberMatched={raw.get('numberMatched')}"


def main() -> None:
    """Search each example query and print the match count per answering node."""
    logging.basicConfig(
        level=logging.DEBUG,
        format=(
            "%(asctime)s %(levelname)s p=%(process)d t=%(thread)d %(name)s %(message)s"
        ),
    )

    for query in (EXAMPLE_CMIP5, EXAMPLE_CMIP6, EXAMPLE_CMIP7):
        print(f"\nquery: {query!r}")
        for host, raw in search(query, limit=2).results.items():
            print(f"  {host:22} {node_count_summary(raw)}")


if __name__ == "__main__":
    main()
