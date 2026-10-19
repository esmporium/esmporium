"""
What a tracked search remembers: the same search, twice, and the difference between

The point of tracking is the second run. One search tells you what ESGF has; two runs
of the *same* search tell you what moved -- a dataset retracted, a new version
published, a node appearing -- and that is what a watch emails you about.

What this shows, in order:

1. A requirement is stored once per *question*, with its name as a label. Renaming it
   is not a new search, and the default refuses a rename rather than losing the old
   name silently.
2. Re-running by name alone is all a person needs, because the name is the thing they
   know.
3. A run which only half happened records what it managed, writes no changes, and is
   passed over as a baseline -- so one flaky host does not read as ESGF deleting data.

Needs no network. A mock ESGF is scripted so each run's answer is chosen rather than
hoped for, which is the only way to show a retraction without waiting for a real one.
`--database PATH` writes to a real store instead of a throwaway, which is how to run
this on the server against data that is already there.

Usage
-----
    python scripts/search_tracking_demo.py
    python scripts/search_tracking_demo.py --database ~/esmporium.db
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

import httpx
from sqlmodel import create_engine
from tenacity import Retrying, retry_if_exception, stop_after_attempt

from esmporium.db import (
    OnNameClash,
    RequirementNameClashError,
    RequirementNameInUseError,
    configure_sqlite_for_concurrency,
    get_changes,
    get_search_runs,
    get_stored_requirements,
    rerun_search_esgf,
    search_and_track,
)
from esmporium.db.migrate import upgrade_to_head
from esmporium.query import Query
from esmporium.requirements import Requirement, all_of, leaf, requirement
from esmporium.search import (
    ESGF1_CMIP6_FACADE_PARAMETERS,
    SearchAPIESGF1Solr,
    SearchAPIFacade,
    SolrSingleRowResultParser,
    build_list_selector,
)
from esmporium.search.retry import _is_transient

HOST = "demo.example.io"
"""The mock host every search in this demo goes to"""


def solr_document(
    doc_id: str, *, retracted: bool = False, data_node: str = "a.node"
) -> dict:
    """One Solr record, which parses into one dataset row"""
    return {
        "master_id": [f"CMIP6.{doc_id}"],
        "id": [doc_id],
        "project": ["CMIP6"],
        "source_id": ["ACCESS-ESM1-5"],
        "institution_id": ["CSIRO"],
        "experiment_id": ["historical"],
        "variant_label": ["r1i1p1f1"],
        "variable_id": ["tas"],
        "frequency": ["mon"],
        "table_id": ["Amon"],
        "grid_label": ["gn"],
        "version": ["20200101"],
        "latest": [True],
        "retracted": [retracted],
        "data_node": [data_node],
    }


def answering(documents: list[dict] | None) -> httpx.Client:
    """
    Get a client which answers every search with `documents`

    `None` makes the host refuse, which is how the incomplete run below is staged.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        if documents is None:
            return httpx.Response(500)

        return httpx.Response(
            200,
            json={
                "response": {
                    "numFound": len(documents),
                    "start": 0,
                    "docs": documents,
                }
            },
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def demo_facade() -> SearchAPIFacade:
    """Build a CMIP6 Solr facade pointed at the mock host, with retries off"""
    return SearchAPIFacade(
        parameters=ESGF1_CMIP6_FACADE_PARAMETERS,
        search_api=SearchAPIESGF1Solr(
            HOST,
            Retrying(
                stop=stop_after_attempt(1),
                retry=retry_if_exception(_is_transient),
                reraise=True,
            ),
        ),
        result_parser=SolrSingleRowResultParser(),
    )


def pattern_effect(name: str) -> Requirement:
    """Build the requirement this demo watches"""
    return requirement(
        name=name,
        tree=all_of(leaf(Query(variable="tas", project="CMIP6"), "field")),
        group_by=("model",),
    )


def report(engine, result, heading: str) -> None:
    """Print what a run found and what it changed"""
    print(f"\n--- {heading} ---")
    print(f"run {result.search_run_id}, complete: {result.complete}")

    if not result.complete:
        print("  incomplete, so nothing is compared and nothing is logged")
        print("  (and the next run will compare with the last run that answered)")
        return

    changes = result.changes
    baseline = (
        "nothing (first run of this search)"
        if changes.baseline_run_id is None
        else f"run {changes.baseline_run_id}"
    )
    print(f"compared with: {baseline}")

    if not changes.any_changes:
        print("  no changes")
        return

    for change in get_changes(engine, result.search_run_id):
        detail = "" if change.detail is None else f"  {change.detail}"
        print(
            f"  {change.change_type.value:<9} "
            f"dataset version {change.dataset_version_id}{detail}"
        )


def main() -> None:
    """Run the demo"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help="Database to use. A throwaway one is made if this is not given.",
    )
    args = parser.parse_args()

    path = (
        args.database
        if args.database is not None
        else Path(tempfile.mkdtemp()) / "search-tracking-demo.db"
    )
    print(f"database: {path}")

    engine = configure_sqlite_for_concurrency(create_engine(f"sqlite:///{path}"))
    upgrade_to_head(engine)

    selector = build_list_selector([demo_facade()])

    def track(documents, **kwargs):
        with answering(documents) as client:
            return search_and_track(
                engine,
                pattern_effect("pattern-effect"),
                selector,
                client=client,
                warn_on_pagination=False,
                **kwargs,
            )

    def rerun(documents, **kwargs):
        with answering(documents) as client:
            return rerun_search_esgf(
                engine,
                "pattern-effect",
                selector,
                client=client,
                warn_on_pagination=False,
                **kwargs,
            )

    # 1. The first run. Nothing to compare with, so everything found is "added" --
    #    which reads as "first seen in this series" rather than "newly published".
    first = track([solr_document("tas-a"), solr_document("tas-b")])
    report(engine, first, "run 1: two datasets, neither retracted")

    # 2. Nothing has moved. This is what most days look like, and the thing a watch
    #    has to be able to say quietly.
    report(
        engine,
        rerun([solr_document("tas-a"), solr_document("tas-b")]),
        "run 2: same answer",
    )

    # 3. A retraction, and a node appearing. Note that the version row in the database
    #    has already been overwritten by this run, so the only reason this is
    #    answerable at all is that each run kept its own record.
    report(
        engine,
        rerun(
            [
                solr_document("tas-a", retracted=True),
                solr_document("tas-b", data_node="b.node"),
            ]
        ),
        "run 3: 'tas-a' retracted, 'tas-b' served from another node",
    )

    # 4. A dataset drops out of the answer.
    report(
        engine,
        rerun([solr_document("tas-a", retracted=True)]),
        "run 4: 'tas-b' no longer returned",
    )

    # 5. The host has a bad afternoon. The run is recorded and left incomplete, and
    #    nothing is reported: a half-answer is indistinguishable from data going away,
    #    so calling it a change would be inventing one.
    try:
        report(engine, rerun(None), "run 5: the host refuses")
    except Exception as exc:  # the demo is about what gets recorded, not the error
        print("\n--- run 5: the host refuses ---")
        print(f"the search raised {type(exc).__name__}, as it should")
        print("  the run is still recorded, and left incomplete")

    # 6. Back to normal, and the baseline skips the bad run. Without that, every
    #    dataset the failed run missed would come back as an addition.
    report(
        engine,
        rerun([solr_document("tas-a", retracted=True)]),
        "run 6: healthy again -- compare with run 4, not run 5",
    )

    print("\n--- runs recorded ---")
    for run in get_search_runs(engine, "pattern-effect"):
        print(f"  run {run.id}: complete={run.complete}")

    # 7. The naming policy. The fingerprint leaves the name out, so this is recognised
    #    as the search we are already watching rather than stored as a new one.
    print("\n--- naming ---")
    try:
        with answering([]) as client:
            search_and_track(
                engine,
                pattern_effect("ecs-method"),
                selector,
                client=client,
                warn_on_pagination=False,
            )
    except RequirementNameClashError as exc:
        print(f"same question, new name -> refused by default:\n  {exc}")

    # The other direction: one name cannot mean two searches, whatever is passed, or
    # re-running by name is ambiguous and the comparison compares different questions.
    other_question = requirement(
        name="pattern-effect",
        tree=all_of(leaf(Query(variable="pr", project="CMIP6"), "field")),
        group_by=("model",),
    )
    try:
        with answering([]) as client:
            search_and_track(
                engine,
                other_question,
                selector,
                on_name_clash=OnNameClash.KEEP_OLD_NAME,
                client=client,
                warn_on_pagination=False,
            )
    except RequirementNameInUseError as exc:
        print(f"\nnew question, same name -> always refused:\n  {exc}")

    with answering([solr_document("tas-a", retracted=True)]) as client:
        renamed = search_and_track(
            engine,
            pattern_effect("ecs-method"),
            selector,
            on_name_clash=OnNameClash.OVERWRITE_NAME,
            client=client,
            warn_on_pagination=False,
        )

    print("\nafter asking for the rename:")
    for stored in get_stored_requirements(engine):
        print(f"  requirement {stored.id}: {stored.name!r}")
    print(
        "  the same requirement row, so every run above still belongs to it: "
        f"{renamed.stored_requirement_id == first.stored_requirement_id}"
    )


if __name__ == "__main__":
    main()
