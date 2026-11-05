"""
A runnable example of the ancestry step: a query -> headers read -> parents linked

What this is for is making the *saving* visible. A simulation is published as hundreds
of datasets and every one of them has the same parent, so the whole design turns on
reading one file per simulation rather than one per dataset. That is easy to assert in
a test and hard to believe until you watch it: this prints how many datasets were
found, how many headers were actually fetched, and how many bytes that cost.

Three cases, because they fail in different ways:

- **CMIP6** (`--project cmip6`, the default) is the well-behaved one. Its headers
  publish the parent completely -- era, activity, model, experiment, variant and both
  branch times -- so nothing has to be inferred.
- **CMIP5** (`--project cmip5`) is the hard one. Its headers publish three attributes
  and no more, so the parent's model has to be taken from the child; its `branch_time`
  carries at least five mutually incompatible encodings and is recorded without being
  interpreted; and some models publish no usable parent variant at all, which is what
  the fixes are for.
- **CMIP7** (`--project cmip7`) needs no header at all -- its parent is in the search
  index -- and is here so the contrast is visible: the same walk, with the header
  reading costing nothing because nothing asks for it.
"""

from __future__ import annotations

import argparse
import logging
import tempfile
import warnings
from pathlib import Path

from sqlmodel import Session, create_engine, select

from esmporium.db import (
    DatasetParentLink,
    DatasetVersionParent,
    HeaderReadCallRecord,
    ParentInfoSource,
    aggregate_header_read_health,
    build_header_facts_provider,
    build_parent_searcher_with_file_access,
    build_result_processor_with_file_access,
    configure_sqlite_for_concurrency,
    find_parents,
    plan_header_reads,
)
from esmporium.db.migrate import upgrade_to_head
from esmporium.db.schema import Dataset, DatasetVersion, File
from esmporium.fixes import PARENT_FIXES, fixes_from_mapping
from esmporium.query import QueryCMIP5, QueryCMIP6, QueryCMIP7
from esmporium.search import search_single_project

# One simulation per project, narrowed to two variables. Two rather than one so that
# the one-header-per-simulation saving is visible at all, and not twenty because the
# point is the ratio, not the size.
CASES = {
    # `UKESM1-0-LL abrupt-4xCO2` is `r1i1p1f2`, and reading its header is the only way
    # to learn that its parent is `piControl r1i1p1f2` rather than the `r1i1p1f1` a
    # reader might assume. Measured 2026-11-05: parent variant `r1i1p1f2`,
    # `branch_time_in_parent` 39600.0.
    "cmip6": (
        QueryCMIP6(
            experiment_id="abrupt-4xCO2",
            variable_id=("tas", "rlut"),
            frequency="mon",
            source_id="UKESM1-0-LL",
        ),
        ("piControl",),
    ),
    "cmip5": (
        QueryCMIP5(
            experiment="abrupt4xCO2",
            variable=("tas", "rlut"),
            time_frequency="mon",
            # Spelled as CMIP5's *search index* spells it, which is not how the files
            # spell it (`bcc-csm1-1-m`). See `read_cmip5_parent_header`.
            model="BCC-CSM1.1(m)",
            ensemble="r1i1p1",
        ),
        ("piControl",),
    ),
    "cmip7": (
        QueryCMIP7(
            experiment_id="abrupt-4xCO2",
            variable_id=("tas", "rlut"),
            frequency="mon",
            source_id="UKESM1-3-LL",
        ),
        ("piControl", "esm-piControl"),
    ),
}


def heading(text: str) -> None:
    """
    Print a section heading

    Parameters
    ----------
    text
        The heading
    """
    print(f"\n{text}")
    print("-" * len(text))


def main() -> None:
    """Run the demo"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project",
        choices=sorted(CASES),
        default="cmip6",
        help="which project's ancestry to walk (default: cmip6)",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=None,
        help="read this many headers at once (default: one at a time)",
    )
    parser.add_argument(
        "--no-fixes",
        action="store_true",
        help="do not apply the corrections in esmporium.fixes",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="show every request at DEBUG"
    )
    arguments = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if arguments.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )
    for noisy in ("httpx", "httpcore", "alembic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    query, until = CASES[arguments.project]

    root = Path(tempfile.mkdtemp(prefix="esmporium-parents-"))
    engine = configure_sqlite_for_concurrency(
        create_engine(f"sqlite:///{root / 'esmporium.db'}")
    )
    upgrade_to_head(engine)
    print(f"database: {root / 'esmporium.db'}")

    with Session(engine) as session:
        heading(f"1. searching for {arguments.project.upper()}")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            search_single_project(
                query,
                # Not the plain `build_result_processor`: on Solr a dataset document
                # carries no file URLs, so without this there would be no file to read
                # a header out of.
                processor=build_result_processor_with_file_access(session),
                warn_on_pagination=False,
            )
        session.commit()

        datasets = session.exec(select(Dataset)).all()
        files = session.exec(select(File)).all()
        # Read now, inside the session: a `File` row handed out of one cannot refresh
        # itself and raises `DetachedInstanceError` when asked for a column later.
        n_files = len(files)
        file_bytes = sum(one.size or 0 for one in files)
        version_ids = [one.id for one in session.exec(select(DatasetVersion)).all()]
        print(f"found {len(datasets)} dataset(s) and {n_files} file(s)")
        if not datasets:
            print("nothing to walk; the search returned no datasets")

            return

        heading("2. what reading their parents will cost")
        plan = plan_header_reads(session, version_ids)
        print(
            f"{len(plan.to_read)} header(s) to read, for {len(version_ids)} version(s)"
        )
        for item in plan.to_read:
            print(
                f"  {item.simulation}: {len(item.dataset_version_ids)} version(s) "
                f"answered by 1 header, first trying {item.filename}"
            )
        if plan.reusable:
            print(f"{len(plan.reusable)} version(s) already answered, for free")
        if plan.unreadable:
            print(f"{len(plan.unreadable)} simulation(s) have no fetchable file")
        if not plan.to_read:
            print("(CMIP7 publishes its parent in the index, so no header is needed)")

        heading("3. walking the ancestry")
        result = find_parents(
            session,
            version_ids,
            until_experiment=until,
            searcher=build_parent_searcher_with_file_access(),
            facts_provider=build_header_facts_provider(
                fixes=None if arguments.no_fixes else fixes_from_mapping(PARENT_FIXES),
                max_workers=arguments.max_workers,
            ),
            check_until_exists=False,
        )
        session.commit()
        print(
            f"{result.hops} hop(s), {len(result.linked)} link(s), "
            f"{len(result.searched)} parent search(es)"
        )
        # Deduplicated: the walk records a terminal per dataset that reached one, and
        # a simulation's datasets all reach the same one.
        for terminal in sorted({str(each) for each in result.terminals}):
            print(f"  reached {terminal}")
        for gap in sorted({str(each) for each in result.variable_gaps}):
            print(f"  variable gap: {gap}")

    report(engine, file_bytes=file_bytes)


def report(engine, *, file_bytes: int) -> None:
    """
    Print what the walk recorded, and what the headers cost

    Parameters
    ----------
    engine
        The database the walk wrote into

    file_bytes
        How many bytes of file the search found, for comparing the header cost against
    """
    with Session(engine) as session:
        heading("4. what was recorded")

        rows = session.exec(select(DatasetVersionParent)).all()
        by_claim: dict[tuple, int] = {}
        for row in rows:
            claim = (
                row.source,
                row.parent_model,
                row.parent_experiment,
                row.parent_variant_label,
                row.branch_time_in_parent,
                row.resolution,
            )
            by_claim[claim] = by_claim.get(claim, 0) + 1

        print(f"{len(rows)} parent assertion(s), {len(by_claim)} distinct:")
        for claim, count in sorted(by_claim.items(), key=lambda each: str(each[0])):
            source, model, experiment, variant, branch_time, resolution = claim
            marker = " <-- a fix" if source == ParentInfoSource.FIX else ""
            print(
                f"  x{count:<4} [{source}] -> {model} {experiment} {variant}"
                f"  branch_time={branch_time}  ({resolution}){marker}"
            )

        links = session.exec(select(DatasetParentLink)).all()
        print(f"\n{len(links)} resolved link(s) to a parent dataset we hold")

        heading("5. what the headers cost")
        calls = session.exec(select(HeaderReadCallRecord)).all()
        if not calls:
            print("no header was read at all")
        else:
            moved = sum(call.bytes_transferred for call in calls)
            print(
                f"{len(calls)} attempt(s), {moved:,} bytes read in total "
                f"-- against {file_bytes:,} bytes of file"
            )
            for call in calls:
                print(
                    f"  {call.outcome:<18} http={call.response_code} "
                    f"{call.bytes_transferred:>7,}B in {call.seconds:5.2f}s "
                    f"range_honoured={call.range_honoured}"
                )

    # Outside the session above, deliberately. `aggregate_header_read_health` opens a
    # session of its own, and on SQLite a session takes the single write lock when its
    # transaction opens -- `configure_sqlite_for_concurrency` uses `BEGIN IMMEDIATE` --
    # so asking for this while another session is open deadlocks against it. The same
    # trap `esmporium.db.header_health.record_header_read_calls` warns about.
    for node, health in sorted(aggregate_header_read_health(engine).items()):
        print(
            f"  {node}: {health.n_success}/{health.n_calls} header read(s) "
            f"succeeded, median {health.median_seconds:.2f}s"
            if health.median_seconds is not None
            else f"  {node}: {health.n_success}/{health.n_calls} header read(s) "
            "succeeded, none timed"
        )


if __name__ == "__main__":
    main()
