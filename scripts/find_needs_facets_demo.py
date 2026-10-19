"""
What `needs_facets` is worth: the same lookups, with and without the documents

Sits beside `requirements_solve_demo.py`, which shows what the solver *is*. This shows
what one part of it *costs*, because the answer is not obvious: a
[CatalogueEntry][esmporium.requirements.CatalogueEntry] is built from a `Dataset` row
and from that dataset's stored search documents, and the row is already in hand while
the documents are a second query and a JSON parse each.

A requirement which names nothing a document holds -- the common shape, grouping on
`model` and `variant_label` and preferring on `grid_label` -- needs none of that work,
and [facets_read][esmporium.requirements.solve.facets_read] is how `solve` says so.

Two savings, measured separately because they are independent:

1. **Per lookup**: whether the stored documents are opened at all.
2. **Per solve**: how many lookups there are. A leaf's candidates do not depend on the
   group, so one question is asked once rather than once per group.

Needs no network. By default it builds its own throwaway SQLite database, so the
numbers are reproducible from a clean checkout; `--database PATH` points it at a real
store instead, which is the honest version of the same question -- real ESGF documents
have field counts and version fan-out that generated ones only imitate.

Usage
-----
    python scripts/find_needs_facets_demo.py
    python scripts/find_needs_facets_demo.py --datasets 50000
    python scripts/find_needs_facets_demo.py --database ~/esmporium.db
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

from sqlmodel import Session, create_engine

from esmporium.datasets import DATASET_FACET_COLUMNS
from esmporium.db import (
    METADATA,
    DatabaseCatalogue,
    Dataset,
    DatasetRawDoc,
    DatasetVersion,
    RawDocVersionLink,
    configure_sqlite_for_concurrency,
)
from esmporium.query import Query
from esmporium.requirements import (
    UnloadedFacetError,
    all_of,
    facets_read,
    leaf,
    requirement,
    solve,
)

SOLR = "solr"
"""The format tag of the documents this writes, i.e. the common case"""

DOCUMENT_FIELDS = 57
"""
How many keys a generated document carries

The number in the note this work came from, which measured real Solr documents, so the
generated ones are the same size as the thing they stand in for.
"""

MODELS = tuple(f"Model{index:02d}" for index in range(40))
"""Forty models, i.e. forty groups: the size of a real multi-model analysis"""

VARIABLES = ("tas", "rlut")
"""Two variables an analysis needs together, i.e. two leaves"""

GREGORY = requirement(
    name="gregory-ish",
    tree=all_of(*(leaf(Query(variable=variable), variable) for variable in VARIABLES)),
    group_by=("model", "variant_label"),
    where=Query(project="CMIP6", reporting_interval="mon"),
    prefer={"grid_label": ("gn", "gr")},
)
"""
The requirement being solved, named only in facets which are columns

That is the point rather than a convenience: every facet it mentions is a column of
`Dataset`, so nothing it does needs a document opened. A requirement which grouped on
`cordex-cmip6:driving_source_id` would need every document read, and should: the
saving is in not reading what nobody asked for, not in reading less than was asked for.
"""


def one_document(model: str, variable: str, index: int) -> dict[str, object]:
    """Get one stored search document, shaped and sized like a real Solr one."""
    # Single-element lists, which is how Solr returns a facet and so what the
    # flattener has to unwrap. Bare strings would be less work than the real thing
    # and would flatter the numbers below. `id` is the exception, as it is upstream.
    document: dict[str, object] = {
        "id": f"CMIP6.CMIP.Institute.{model}.historical.r1i1p1f1.Amon.{variable}.gn",
        "project": ["CMIP6"],
        "source_id": [model],
        "variable_id": [variable],
        "frequency": ["mon"],
        "grid_label": ["gn"],
        "realm": ["atmos"],
        "activity_id": ["CMIP"],
        "nominal_resolution": ["100 km"],
        "mip_era": ["CMIP6"],
    }
    # Padded out to the real field count. These stand for the fields a requirement
    # never names and which are nonetheless fetched, parsed and flattened for it
    # today, which is the whole cost being measured.
    for field in range(DOCUMENT_FIELDS - len(document)):
        document[f"field_{field:02d}"] = [f"value-{index}-{field}"]

    return document


def build_database(path: Path, datasets: int) -> None:
    """Write `datasets` datasets, each with one version and one document."""
    engine = configure_sqlite_for_concurrency(create_engine(f"sqlite:///{path}"))
    METADATA.create_all(engine)

    facets = {column: f"{column}-value" for column in DATASET_FACET_COLUMNS}
    with Session(engine) as session:
        for index in range(datasets):
            model = MODELS[index % len(MODELS)]
            variable = VARIABLES[index % len(VARIABLES)]
            row = Dataset(
                **{
                    **facets,
                    "id_project_specific": f"CMIP6.{model}.{variable}.{index}",
                    "project": "CMIP6",
                    "model": model,
                    "variable": variable,
                    "reporting_interval": "mon",
                    "grid_label": "gn",
                    "variant_label": "r1i1p1f1",
                }
            )
            session.add(row)
            session.flush()

            version = DatasetVersion(
                dataset_id=row.id,
                version="20200101",
                is_latest=True,
                retracted=False,
            )
            session.add(version)
            session.flush()

            document = DatasetRawDoc(
                esgf_doc_id=f"doc-{index}",
                raw_json=json.dumps(one_document(model, variable, index)),
                raw_docs_format_tag=SOLR,
            )
            session.add(document)
            session.flush()
            session.add(
                RawDocVersionLink(raw_doc_id=document.id, dataset_version_id=version.id)
            )

        session.commit()

    engine.dispose()


def best_of(call, repeats: int = 3) -> tuple[float, int]:
    """Get the fastest of a few runs, and what the call found, to drown out noise."""
    best = float("inf")
    found = 0
    for _ in range(repeats):
        started = time.perf_counter()
        result = call()
        best = min(best, time.perf_counter() - started)
        found = len(result)

    return best, found


def banner(title: str) -> None:
    """Print one scene's heading."""
    print("=" * 78)
    print(title)
    print("=" * 78)


def show_one_lookup(catalogue: DatabaseCatalogue, query: Query, needs: frozenset[str]):
    """Time one lookup both ways, and return what the full one cost."""
    banner("1. One lookup, with and without the stored documents")
    print(f"the requirement names: {', '.join(sorted(needs))}")
    print("every one of those is a column, so no document has to be opened\n")

    read, found = best_of(lambda: catalogue.find(query))
    skipped, _ = best_of(lambda: catalogue.find(query, needs_facets=needs))

    print(f"matched {found:,} datasets")
    print(f"  documents read     {read:8.3f}s")
    print(f"  documents skipped  {skipped:8.3f}s     {read / skipped:.1f}x cheaper")
    print()

    return read


def show_one_solve(catalogue: DatabaseCatalogue, full_lookup: float) -> None:
    """Count the lookups a solve makes, and what the two shapes cost."""
    banner("2. One solve: how many lookups, and how costly each")

    started = time.perf_counter()
    result = solve(GREGORY, catalogue)
    measured = time.perf_counter() - started

    groups = len(result.satisfied) + len(result.unsatisfied) + len(result.ambiguous)
    leaves = len(VARIABLES)
    before = leaves * (groups + 1)

    print(f"{leaves} leaves, {groups} groups discovered")
    print(f"  lookups before   {before:4d}   each reading every document")
    print(f"  lookups now      {leaves:4d}   none of them reading one")
    print()
    print(f"  solve, as it is now     {measured:9.3f}s   (measured)")
    # Multiplied rather than measured: the old shape over this many rows takes
    # minutes, and it was the same lookup repeated, so the product is what it cost.
    print(
        f"  solve, as it was       ~{before * full_lookup:9.3f}s   "
        f"({before} x the full lookup above)"
    )
    print(
        f"  together                          {before * full_lookup / measured:6.0f}x"
    )
    print()


def show_what_entries_answer(
    catalogue: DatabaseCatalogue, query: Query, needs: frozenset[str]
) -> None:
    """Show what a lookup which skipped the documents will and will not answer."""
    banner("3. What the entries will and will not answer")

    found = catalogue.find(query, needs_facets=needs)
    if not found:
        print("nothing matched, so there is nothing to ask")
        return

    entry = found[0]
    print(f"entry #{entry.id} was built for: {', '.join(sorted(needs))}")
    print(f"  .facet('model')  -> {entry.facet('model')!r}")
    try:
        entry.facet("realm")
    except UnloadedFacetError as error:
        # The message is the point: `None` here would be indistinguishable from a
        # dataset which records no realm, which is a real thing that happens.
        print("  .facet('realm')  -> UnloadedFacetError:")
        print(f"      {error}")
    print()


def main() -> None:
    """Build (or open) a database, then time the lookups two ways."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--datasets",
        type=int,
        default=20_000,
        help="how many datasets to generate (ignored with --database)",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help="an existing database to measure instead of generating one",
    )
    arguments = parser.parse_args()

    with tempfile.TemporaryDirectory() as directory:
        if arguments.database is None:
            path = Path(directory) / "demo.sqlite"
            banner(f"Building {arguments.datasets:,} datasets, one document each")
            started = time.perf_counter()
            build_database(path, arguments.datasets)
            print(f"done in {time.perf_counter() - started:.1f}s -> {path}")
            print()
        else:
            path = arguments.database

        engine = configure_sqlite_for_concurrency(create_engine(f"sqlite:///{path}"))
        query = Query(project="CMIP6", variable="tas", reporting_interval="mon")
        catalogue = DatabaseCatalogue(engine)
        needs = facets_read(GREGORY)

        full_lookup = show_one_lookup(catalogue, query, needs)
        show_one_solve(catalogue, full_lookup)
        show_what_entries_answer(catalogue, query, needs)

        engine.dispose()


if __name__ == "__main__":
    main()
