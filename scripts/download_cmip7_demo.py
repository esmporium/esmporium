"""
A runnable example of the download step: a CMIP7 query -> bytes on disk -> node health

Sizes were measured rather than guessed (2026-11-02):

- `areacella` on `CanESM5-1` is **52,593 bytes** per file, so the default run takes
  seconds;
- `tas` monthly for `1pctCO2 r1i1p2f1` is 11.8 MB and 23.1 MB per file, which is worth
  having because a 51 KB file never exercises streaming, the progress callback, or
  throughput-based health. That is `--with-tas`, and takes under a minute.
"""

from __future__ import annotations

import argparse
import logging
import tempfile
import threading
import time
from pathlib import Path

from sqlmodel import Session, create_engine, select

from esmporium.db import (
    DataNodeCallRecord,
    aggregate_data_node_health,
    build_data_node_health_selector,
    build_result_processor,
    configure_sqlite_for_concurrency,
    download,
    download_summary,
    plan_downloads,
    record_data_node_calls,
    routes_for_file,
)
from esmporium.db.migrate import upgrade_to_head
from esmporium.downloads import ProgressCallback, prepare_routes, totals
from esmporium.query import QueryCMIP7
from esmporium.search import search_single_project

# `areacella` is a time-invariant cell-area field: one small 2D array, so it is the
# cheapest real CMIP7 file there is (52,593 bytes). Narrowed to a single experiment
# and member because `areacella` is published per experiment per member -- without
# that this query matches 183 datasets.
#
# Note on the experiment: `piControl r1i1p2f1` is deliberately *not* used, even though
# it is the obvious choice. Its published size and checksum do not match the file the
# data node serves (52,597 bytes and one SHA256 published; a complete, valid 44,638
# byte netCDF-4 with a different one served, checked 2026-11-02), so a download of it
# correctly fails with
# [SIZE_MISMATCH][esmporium.datasets.DownloadOutcome.SIZE_MISMATCH]. That is a real
# ESGF publication fault and a good thing to have caught, but it makes for a confusing
# demonstration -- so this points at `esm-scen7-vl`, whose metadata agrees (ten of ten
# sibling files checked exactly).
AREACELLA = QueryCMIP7(
    variable_id="areacella",
    source_id="CanESM5-1",
    experiment_id="esm-scen7-vl",
    variant_label="r1i1p2f1",
)

# Real model output, in the tens of megabytes: enough to actually stream.
TAS = QueryCMIP7(
    variable_id="tas",
    frequency="mon",
    source_id="CanESM5-1",
    experiment_id="1pctCO2",
    variant_label="r1i1p2f1",
)


def search_and_store(engine, query) -> None:
    """
    Search live CMIP7 and save what comes back, so there is something to download

    Parameters
    ----------
    engine
        The database to save into

    query
        What to search for
    """
    print(f"searching CMIP7 for {query}")
    with Session(engine) as session:
        outcome = search_single_project(
            query, processor=build_result_processor(session)
        )
        session.commit()

    n_docs = sum(len(docs) for docs in outcome.parsed_docs.values())
    print(f"  {n_docs} document(s) from {len(outcome.parsed_docs)} facade(s)")
    for key, failure in outcome.failures.items():
        print(f"  ! {key[0]} failed: {failure}")


def print_plan(engine, query, root: Path) -> int:
    """
    Print what a download of `query` would fetch

    Parameters
    ----------
    engine
        The database to plan from

    query
        What to download

    root
        Where it would go

    Returns
    -------
    :
        How many files are planned
    """
    with Session(engine) as session:
        plan = plan_downloads(session, query, root)

        print(f"\nplanned {len(plan)} file(s):")
        for item in plan:
            routes = routes_for_file(session, item.file_id)
            fetchable = prepare_routes(routes)
            size = item.request.expected_size
            print(
                f"  {item.request.dest.name}"
                f"  {size if size is not None else '?':>12} bytes"
                f"  {len(fetchable)}/{len(routes)} route(s) fetchable"
            )
            for route in fetchable:
                print(f"      via {route.data_node}")

    return len(plan)


def print_results(results) -> None:
    """
    Print where each file landed and what we could verify about it

    Parameters
    ----------
    results
        What the download returned
    """
    print("\nresults:")
    for result in results:
        if result.fetch is not None:
            verified = (
                f"verified ({result.fetch.checksum_type_used})"
                if result.fetch.checksum_verified
                else "no checksum published"
            )
            print(
                f"  OK   {result.fetch.path.name}"
                f"  {result.fetch.size_on_disk:>12} bytes"
                f"  {verified}"
                f"  from {result.fetch.route.data_node}"
            )
        else:
            why = "given up" if result.give_up else "may retry"
            print(f"  FAIL {result.request.label}  ({why}) {result.last_error}")

        for failure in result.failures:
            print(f"         tried {failure.route.data_node}: {failure.outcome}")

    print(f"\n{totals(results)}")


def print_attempts(engine) -> None:
    """
    Print the per-attempt health log

    One row per HTTP attempt, which is what makes a retried or mirror-hopped download
    legible after the fact.

    Parameters
    ----------
    engine
        The database to read from
    """
    with Session(engine) as session:
        rows = session.exec(select(DataNodeCallRecord)).all()

    print(f"\n{len(rows)} recorded attempt(s):")
    print(
        f"  {'data node id':>12}  {'outcome':<10} {'code':>5} "
        f"{'bytes':>12} {'secs':>7} {'MB/s':>7}  attempt"
    )
    for row in rows:
        rate = (
            row.bytes_transferred / 1_000_000 / row.seconds
            if row.seconds > 0 and row.bytes_transferred > 0
            else None
        )
        print(
            f"  {row.data_node_id:>12}  {row.outcome:<10} "
            f"{row.response_code if row.response_code is not None else '-':>5} "
            f"{row.bytes_transferred:>12} {row.seconds:>7.2f} "
            f"{rate if rate is not None else float('nan'):>7.2f}  {row.attempt_number}"
        )


def print_health_and_ranking(engine, query, root: Path) -> None:
    """
    Print the health those attempts add up to, and the order it implies

    The point of the whole exercise: the download just done is what the *next* one
    will use to decide where to go first.

    Parameters
    ----------
    engine
        The database to read health from

    query
        The query whose files to rank routes for

    root
        The download root, so the plan can be rebuilt
    """
    health = aggregate_data_node_health(engine, since=None)

    print(f"\ndata node health ({len(health)} node(s)):")
    for node, each in sorted(health.items()):
        rate = each.median_throughput_mb_per_s
        print(
            f"  {node:<40} {each.n_success}/{each.n_calls} ok  "
            f"failure rate {each.failure_rate:.2f}  "
            f"{f'{rate:.2f} MB/s' if rate is not None else 'no rate'}"
        )

    selector = build_data_node_health_selector(engine, since=None)
    with Session(engine) as session:
        plan = plan_downloads(session, query, root, skip_done=False)
        if not plan:
            return

        routes = routes_for_file(session, plan[0].file_id)

    print("\nthe order the health-based selector would try next time:")
    attempt = 0
    while (route := selector(routes, attempt)) is not None:
        print(f"  {attempt + 1}. {route.data_node}  {route.url}")
        attempt += 1


def main() -> None:
    """Run the demo"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--with-tas",
        action="store_true",
        help="also download a ~12 MB tas file, to exercise streaming and throughput",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=1,
        help=(
            "how many files to download at once (default 1, i.e. one after another); "
            "no more than four at a time go to any one data node"
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="log every request as a URL and a curl command",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    query = TAS if args.with_tas else AREACELLA

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "downloads"
        engine = configure_sqlite_for_concurrency(
            create_engine(f"sqlite:///{Path(temporary) / 'demo.db'}")
        )
        upgrade_to_head(engine)

        search_and_store(engine, query)

        if not print_plan(engine, query, root):
            print("\nnothing to download -- the search found no files.")
            return

        in_parallel = args.max_workers > 1
        print(
            f"\ndownloading (max_workers={args.max_workers})..."
            if in_parallel
            else "\ndownloading..."
        )
        started = time.monotonic()
        results = download(
            engine,
            query,
            root,
            observer=record_data_node_calls(engine),
            progress=_progress(show_worker=in_parallel),
            max_workers=args.max_workers,
        )
        print(f"\ntook {time.monotonic() - started:.1f}s")

        print_results(results)
        print_attempts(engine)
        print_health_and_ranking(engine, query, root)

        with Session(engine) as session:
            print(f"\nwhat the database now holds: {download_summary(session)}")

        on_disk = sorted(path for path in root.rglob("*") if path.is_file())
        print(f"\n{len(on_disk)} file(s) on disk, under {root}:")
        for path in on_disk:
            print(f"  {path.relative_to(root)}  {path.stat().st_size} bytes")


def _progress(*, show_worker: bool = False) -> ProgressCallback:
    """
    Build a progress callback which prints at most once per 10% of each file

    Several files can be in flight at once, and a callback is told only how far one
    transfer has got, not which file it is. So the deciles are kept per thread, which
    is per file -- a worker fetches one file at a time -- and each line is printed
    while holding a lock, so two transfers reporting together cannot interleave one
    line into the middle of another.

    Parameters
    ----------
    show_worker
        Whether to label each line with the worker that printed it

        Worth it when several files are in flight, because the lines are otherwise
        indistinguishable; noise when there is only ever one.

    Returns
    -------
    :
        The callback
    """
    reached = threading.local()
    speaking = threading.Lock()

    def report(done: int, total: int | None) -> None:
        if total is None or total == 0:
            return

        decile = int(done / total * 10)
        if getattr(reached, "decile", None) == decile:
            return

        reached.decile = decile
        worker = f"[{threading.current_thread().name}] " if show_worker else ""
        with speaking:
            print(f"    {worker}{done:>12}/{total} bytes ({done / total:.0%})")

    return report


if __name__ == "__main__":
    main()
