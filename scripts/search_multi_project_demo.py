"""
A runnable example of multi-project `search`, run serially and then in parallel

`search` takes a `Requirement`: a tree of leaves, each carrying a query, and each query
may name one or more projects. It works out one query per leaf (adding the
requirement's `where`), splits each of those into one single-project query per project
(translating the shared facet names into each project's own dialect as it goes), and
runs each through `search_single_project`, saving results as they arrive through the
processor factory you give it.

This demo builds a requirement with ONE leaf naming three projects, so `search` splits
it into three sub-searches (CMIP5, CMIP6, CMIP7). It then runs the whole thing twice
against throwaway databases; serially, then in parallel in a thread pool.
"""

from __future__ import annotations

import argparse
import logging
import tempfile
import time
from pathlib import Path

from sqlmodel import Session, create_engine, select

from esmporium.db import (
    Dataset,
    SearchAPICallRecord,
    build_result_processor_factory,
    configure_sqlite_for_concurrency,
    record_search_api_calls,
)
from esmporium.db.migrate import upgrade_to_head
from esmporium.query import Query
from esmporium.requirements import leaf, requirement
from esmporium.search import search

# One leaf, three projects. `search` splits this into a CMIP5, a CMIP6 and a CMIP7
# search, translating the shared facet names into each project's own dialect. The
# facet *values* here (variable, reporting interval, experiment) are known to be spelt
# the same across projects, so one leaf is enough. `historical` bounds each project
# to a single page at the default limit, keeping the demo quick.
#
# The projects go on the requirement's `where` rather than on the leaf, which is the
# usual way round: `where` is added to every leaf, so a requirement that grew a second
# leaf would search both over the same three projects without repeating itself.
MULTI_PROJECT_REQUIREMENT = requirement(
    name="tas-historical-three-projects",
    tree=leaf(
        Query(variable="tas", reporting_interval="mon", experiment="historical"),
        "tas",
    ),
    group_by=("model",),
    where=Query(project=("CMIP5", "CMIP6", "CMIP7")),
)


def summarise(session: Session, elapsed: float) -> None:
    """
    Print how long a run took, how it overlapped, and what it saved

    Parameters
    ----------
    session
        A session on the database the run wrote into

    elapsed
        The wall-clock time the `search` call took, in seconds
    """
    calls = session.exec(select(SearchAPICallRecord)).all()
    response_time = sum(call.response_time_seconds for call in calls)
    print(
        f"  wall clock {elapsed:5.2f}s  |  {len(calls)} request(s) totalling "
        f"{response_time:5.2f}s of response time"
    )

    saved = session.exec(select(Dataset)).all()
    by_project: dict[str, int] = {}
    for dataset in saved:
        by_project[dataset.project] = by_project.get(dataset.project, 0) + 1
    breakdown = ", ".join(
        f"{project}={count}" for project, count in sorted(by_project.items())
    )
    print(f"  saved {len(saved)} dataset row(s): {breakdown or '(none)'}")


def run(label: str, *, max_workers: int | None, limit: int) -> None:
    """
    Run the multi-project search against a throwaway database and print a summary

    Parameters
    ----------
    label
        A human-readable name for this run, printed as a heading

    max_workers
        Passed straight to `search`: `None` runs the sub-queries serially, an integer
        greater than 1 runs that many at once in a thread pool

    limit
        The page size per request. Kept at or above each project's result count so the
        demo does not page (paging is deliberately never parallelised).
    """
    print(f"=== {label} ===")
    with tempfile.TemporaryDirectory() as tmp:
        engine = create_engine(f"sqlite:///{Path(tmp) / 'esmporium-demo.db'}")
        # Needed to save results into SQLite at all: it gives real transactions and
        # makes a parallel run's workers take turns writing instead of colliding.
        configure_sqlite_for_concurrency(engine)
        upgrade_to_head(engine)

        started = time.monotonic()
        search(
            MULTI_PROJECT_REQUIREMENT,
            limit=limit,
            api_call_observer=record_search_api_calls(engine),
            processor_factory=build_result_processor_factory(engine),
            max_workers=max_workers,
        )
        elapsed = time.monotonic() - started

        with Session(engine) as session:
            summarise(session, elapsed)
    print()


def main() -> None:
    """Parse arguments, then run the search serially and in parallel for comparison."""
    parser = argparse.ArgumentParser(
        description="Demo multi-project search, serial vs parallel."
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=3,
        help="workers for the parallel run (default: 3, one per project)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10_000,
        help="page size per request (default: 10000, big enough to avoid paging here)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="log each request at DEBUG (shows the thread each runs on)",
    )
    args = parser.parse_args()

    if args.verbose:
        # DEBUG logs each request with its thread id (t=...), so in the parallel run
        # you can see several threads sending at once (see the repo logging convention).
        logger = logging.getLogger("esmporium")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s t=%(thread)d %(name)s %(message)s"
            )
        )
        logger.addHandler(handler)

    print(f"requirement: {MULTI_PROJECT_REQUIREMENT.name}")
    print(f"its one leaf asks for: {MULTI_PROJECT_REQUIREMENT.tree.query!r}")
    print("this splits into one search per project: CMIP5, CMIP6, CMIP7\n")

    run("serial (max_workers=None)", max_workers=None, limit=args.limit)
    run(
        f"parallel (max_workers={args.max_workers})",
        max_workers=args.max_workers,
        limit=args.limit,
    )


if __name__ == "__main__":
    main()
