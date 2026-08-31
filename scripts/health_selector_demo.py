"""
Show how the health-based search API selector ranks the ESGF nodes by speed

Sits beside `first_search_cmipx_full.py`, and builds on it: that script shows
recording search-API health; this one *uses* the recorded health to rank the
nodes for the next search.

What it does:

1. Runs the example CMIP5/6/7 searches a few times each, with
   `stop_at_first_result=False` so every node in each project's pool is hit and
   gets a health record (real network, like the search demo).
2. Prints a per-host health table (calls, success rate, median time).
3. For each project, prints the order `build_health_selector` (which ranks by
   speed) would hand to `search()`.

It hits the live nodes, so it needs a network and takes a little while.

Run it:  uv run python scripts/health_selector_demo.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from sqlmodel import create_engine

from esmporium.db import (
    DEFAULT_SEARCH_APIS_BY_PROJECT,
    HostHealth,
    aggregate_host_health,
    build_health_selector,
    get_median_response_time_for_ranking,
    record_search_api_calls,
)
from esmporium.db.migrate import upgrade_to_head
from esmporium.query import (
    QueryCMIP5,
    QueryCMIP6,
    QueryCMIP7,
    QueryProtocol,
    to_canonical,
)
from esmporium.search import search

# One broad-ish query per project, so several nodes have data to compare.
EXAMPLE_QUERIES: dict[str, QueryProtocol] = {
    "CMIP5": QueryCMIP5(experiment="historical", variable="tas", time_frequency="mon"),
    "CMIP6": QueryCMIP6(experiment_id="historical", variable_id="tas", frequency="mon"),
    "CMIP7": QueryCMIP7(variable_id="tas"),
}

REPEATS = 3
"""How many times to run each search, so timings are more than a single sample"""


def gather_health(engine) -> None:
    """Run every example search a few times, recording health as we go."""
    observer = record_search_api_calls(engine)
    for run in range(1, REPEATS + 1):
        for project, query in EXAMPLE_QUERIES.items():
            print(f"  run {run}/{REPEATS}: {project} ...", flush=True)
            # `stop_at_first_result=False` so every node in the pool is asked,
            # otherwise only the first node would ever get a health record.
            search(query, limit=2, stop_at_first_result=False, observer=observer)


def print_health_table(health: dict[str, HostHealth]) -> None:
    """Print one row per host: how it has behaved across all the runs."""
    print("\nper-host health (rolled up across all runs):")
    print(f"  {'host':24} {'calls':>5} {'ok%':>5} {'med.time':>9}")
    print(f"  {'-' * 24} {'-' * 5} {'-' * 5} {'-' * 9}")
    # Fastest first, the same order the selector would use.
    for h in sorted(health.values(), key=get_median_response_time_for_ranking):
        time = (
            "-"
            if h.median_response_time_seconds == float("inf")
            else f"{h.median_response_time_seconds:.2f}s"
        )
        print(f"  {h.host:24} {h.n_calls:>5} {h.success_rate * 100:>4.0f}% {time:>9}")


def ranked_hosts(engine, project: str) -> list[str]:
    """Return the host order the speed-ranked selector would hand `search()`."""
    selector = build_health_selector(engine, rank=get_median_response_time_for_ranking)
    canonical = to_canonical(EXAMPLE_QUERIES[project])
    hosts: list[str] = []
    attempt = 0
    while (api := selector(canonical, attempt)) is not None:
        hosts.append(api.host)
        attempt += 1
    return hosts


def print_rankings(engine) -> None:
    """For each project, print the speed-ranked order the selector would try."""
    print("\nspeed-ranked order per project (fastest first):")
    for project in EXAMPLE_QUERIES:
        print(f"  {project}: {' > '.join(ranked_hosts(engine, project))}")


def main() -> None:
    """Gather live health, then print the table and the speed-ranked order."""
    with tempfile.TemporaryDirectory() as tmp:
        engine = create_engine(f"sqlite:///{Path(tmp) / 'esmporium-demo.db'}")
        upgrade_to_head(engine)

        print("gathering health from live nodes (this takes a little while):")
        gather_health(engine)

        pool_hosts = {
            api.host for pool in DEFAULT_SEARCH_APIS_BY_PROJECT.values() for api in pool
        }
        health = aggregate_host_health(engine, pool_hosts)

        print_health_table(health)
        print_rankings(engine)


if __name__ == "__main__":
    main()
