"""
A runnable example of the search step: QueryCMIP{5,6,7} -> live ESGF -> saved rows

Everything that does the work now lives in `esmporium.search` and `esmporium.db`;
this is only a hand-run example of calling it. Logging is turned up to `DEBUG` so
that the URL- and `curl`-equivalent of each request (and the process/thread it went
out on) are printed as the search runs.

It shows two opt-in seams that hang off `search()`:

- an `api_call_observer` that records every request into a throwaway SQLite database
  (which host, what status, how many results, how long), printed after the searches;
- a `processor` from `build_result_processor(session)` that parses and saves each
  host's datasets the moment it answers. The rows it wrote are counted at the end.

Run it:  uv run python scripts/first_search_cmipx_full.py
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from sqlmodel import Session, create_engine, select

from esmporium.db import (
    Dataset,
    SearchAPICallRecord,
    build_result_processor,
    record_search_api_calls,
)
from esmporium.db.migrate import upgrade_to_head
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


def print_health(session: Session) -> None:
    """Print every recorded search-API call, in the order they happened."""
    print("\nsearch API health (one row per request):")
    records = session.exec(
        select(SearchAPICallRecord).order_by(SearchAPICallRecord.id)
    ).all()
    for record in records:
        status = "ok" if record.success else f"FAILED ({record.error})"
        code = record.response_code if record.response_code is not None else "-"
        results = record.num_results if record.num_results is not None else "-"
        print(
            f"  {record.host:22} {record.http_method:4} code={code!s:4} "
            f"results={results!s:8} {record.response_time_seconds:5.2f}s  {status}"
        )


def main() -> None:
    """Search each example query, save datasets as they arrive, then print a summary."""
    logging.basicConfig(
        level=logging.DEBUG,
        format=(
            "%(asctime)s %(levelname)s p=%(process)d t=%(thread)d %(name)s %(message)s"
        ),
    )

    # A throwaway database, migrated to the current schema, just for this demo.
    with tempfile.TemporaryDirectory() as tmp:
        engine = create_engine(f"sqlite:///{Path(tmp) / 'esmporium-demo.db'}")
        upgrade_to_head(engine)

        with Session(engine) as session:
            # Passing these is the whole opt-in: leave them off and nothing is recorded
            # or saved. The observer records every request; the processor saves each
            # host's datasets the moment it answers.
            observer = record_search_api_calls(engine)
            processor = build_result_processor(session)

            for query in (EXAMPLE_CMIP5, EXAMPLE_CMIP6, EXAMPLE_CMIP7):
                print(f"\nquery: {query!r}")
                outcome = search(
                    query, limit=2, api_call_observer=observer, processor=processor
                )
                for host, documents in outcome.datasets.items():
                    dataset_rows = sum(len(doc.datasets) for doc in documents)
                    print(
                        f"  {host:22} matched={outcome.n_matches[host]} "
                        f"documents={len(documents)} dataset_rows={dataset_rows}"
                    )

            saved = len(session.exec(select(Dataset)).all())
            print(f"\nsaved {saved} dataset row(s) to the database")

            print_health(session)


if __name__ == "__main__":
    main()
