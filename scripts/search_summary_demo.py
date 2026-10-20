"""
What a search summary looks like, scene by scene

The scenes are:

1. a quiet morning -- the search ran, nothing changed
2. the interesting morning -- new models, a retraction, data nodes moving
3. the morning you must not misread -- a sub-search failed and another was truncated,
   so the removals may not be real
4. a week in one summary -- several runs folded down to their net effect, with churn
5. a long list -- what `max_rows` does when a search returns more than you can read

Every summary here is built by hand, so this needs no network and no database.
[summarise_search][esmporium.db.summarise_search] is how you get a real one.
"""

from __future__ import annotations

import datetime

from esmporium.db import (
    ChangeRecord,
    RunHealth,
    SearchRunLeafInfo,
    SearchSummary,
    VersionRecord,
)
from esmporium.db.schema import DatasetChangeType

FACET_DEFAULTS = {
    "project": "CMIP7",
    "institution": "CSIRO",
    "variable": "tas",
    "reporting_interval": "mon",
    "grid_label": "gn",
    "processing_id": "Amon",
}
"""The facets every dataset in these scenes shares, i.e. what the search asked for"""


def version(  # noqa: PLR0913 - one argument per thing a scene varies
    model: str,
    experiment: str,
    variant_label: str,
    dataset_version_id: int,
    *,
    version_string: str = "20261018",
    retracted: bool = False,
    data_nodes: tuple[str, ...] = ("esgf.nci.org.au",),
) -> VersionRecord:
    """Get one dataset version, as a summary carries it."""
    return VersionRecord(
        dataset_version_id=dataset_version_id,
        dataset_id=dataset_version_id * 10,
        id_project_specific=(
            f"CMIP7.CSIRO.{model}.{experiment}.{variant_label}.Amon.tas.gn"
        ),
        facets={
            **FACET_DEFAULTS,
            "model": model,
            "experiment": experiment,
            "variant_label": variant_label,
        },
        version=version_string,
        is_latest=True,
        retracted=retracted,
        data_nodes=data_nodes,
        seen_in_run_id=7,
    )


def change(
    change_type: DatasetChangeType,
    of: VersionRecord,
    *,
    detail: dict[str, dict[str, object]] | None = None,
    first_run_id: int = 7,
    last_run_id: int = 7,
) -> ChangeRecord:
    """Get one change, as a summary carries it."""
    return ChangeRecord(
        change_type=change_type,
        version=of,
        detail=detail,
        first_run_id=first_run_id,
        last_run_id=last_run_id,
    )


def leaf(
    role: str,
    *,
    answered: bool = True,
    n_matches: int | None = 209,
    n_documents: int = 209,
    failure: str | None = None,
) -> SearchRunLeafInfo:
    """Get one sub-search's outcome, as a run's health carries it."""
    return SearchRunLeafInfo(
        roles=(role,),
        project="CMIP7",
        answered=answered,
        host="search.east.esgf.io" if answered else None,
        n_matches=n_matches,
        n_documents=n_documents,
        failure=failure,
    )


def healthy_run(run_id: int = 7) -> RunHealth:
    """Get a run where everything answered in full."""
    return RunHealth(
        run_id=run_id,
        started_at=datetime.datetime(2026, 10, 20, 6, 2, tzinfo=datetime.timezone.utc),
        finished_at=datetime.datetime(2026, 10, 20, 6, 4, tzinfo=datetime.timezone.utc),
        complete=True,
        leaves=(leaf("historical.tas"), leaf("control.tas")),
    )


def summary(  # noqa: PLR0913 - one argument per field a scene fills in
    *,
    run: RunHealth | None = None,
    changes: tuple[ChangeRecord, ...] = (),
    churned: int = 0,
    current: tuple[VersionRecord, ...] = (),
    window_runs: tuple[int, ...] = (7,),
    baseline_run_id: int | None = 6,
) -> SearchSummary:
    """Get a whole summary, with the boring parts filled in."""
    return SearchSummary(
        requirement_name="pattern-effect",
        stored_requirement_id=1,
        run=run if run is not None else healthy_run(),
        baseline_run_id=baseline_run_id,
        window_runs=window_runs,
        changes=changes,
        churned=churned,
        current=current,
    )


def show(title: str, built: SearchSummary, *, max_rows: int = 20) -> None:
    """Print one scene, the way you would see it in a terminal."""
    print("=" * 78)
    print(title)
    print("=" * 78)
    print(built.render(max_rows=max_rows))
    print()


# ------------------------------------------------------------------- the datasets
ACCESS_HISTORICAL = version("ACCESS-ESM1-6", "historical", "r1i1p1f1", 101)
ACCESS_CONTROL = version("ACCESS-ESM1-6", "piControl", "r1i1p1f1", 102)
ACCESS_CONTROL_NOW_RETRACTED = version(
    "ACCESS-ESM1-6", "piControl", "r1i1p1f1", 102, retracted=True
)
CESM_HISTORICAL = version("CESM3", "historical", "r1i1p1f1", 103)
CESM_CONTROL = version("CESM3", "piControl", "r1i1p1f1", 104)
MIROC_HISTORICAL = version(
    "MIROC7",
    "historical",
    "r1i1p1f1",
    105,
    data_nodes=("esgf.nci.org.au", "aims3.llnl.gov"),
)
MIROC_CONTROL_RETRACTED = version(
    "MIROC7", "piControl", "r1i1p1f1", 106, retracted=True
)
UKESM_HISTORICAL_OLD = version(
    "UKESM2", "historical", "r1i1p1f1", 107, version_string="20260801"
)

EVERYTHING = (
    ACCESS_HISTORICAL,
    ACCESS_CONTROL,
    CESM_HISTORICAL,
    CESM_CONTROL,
    MIROC_HISTORICAL,
    MIROC_CONTROL_RETRACTED,
)


# ------------------------------------------------------------------ 1. nothing changed
QUIET = summary(current=EVERYTHING)


# ------------------------------------------------------------------ 2. the real news
INTERESTING = summary(
    changes=(
        change(DatasetChangeType.ADDED, CESM_HISTORICAL),
        change(DatasetChangeType.ADDED, CESM_CONTROL),
        change(DatasetChangeType.ADDED, MIROC_CONTROL_RETRACTED),
        change(DatasetChangeType.REMOVED, UKESM_HISTORICAL_OLD),
        change(
            DatasetChangeType.MODIFIED,
            ACCESS_CONTROL_NOW_RETRACTED,
            detail={"retracted": {"old": False, "new": True}},
        ),
        change(
            DatasetChangeType.MODIFIED,
            MIROC_HISTORICAL,
            detail={
                "data_nodes": {
                    "old": ["esgf.nci.org.au"],
                    "new": ["esgf.nci.org.au", "aims3.llnl.gov"],
                }
            },
        ),
    ),
    # The current state has to agree with the changes above it: ACCESS-ESM1-6
    # piControl was just retracted, so that is what the run was told about it.
    current=tuple(
        ACCESS_CONTROL_NOW_RETRACTED if held is ACCESS_CONTROL else held
        for held in EVERYTHING
    ),
)


# ------------------------------------------------------- 3. do not misread this one
BROKEN_RUN = summary(
    run=RunHealth(
        run_id=8,
        started_at=datetime.datetime(2026, 10, 21, 6, 2, tzinfo=datetime.timezone.utc),
        finished_at=datetime.datetime(2026, 10, 21, 6, 9, tzinfo=datetime.timezone.utc),
        complete=False,
        leaves=(
            leaf("historical.tas", n_matches=209, n_documents=50),
            leaf(
                "control.tas",
                answered=False,
                n_matches=None,
                n_documents=0,
                failure="AllSubQueriesFailedError: every endpoint refused",
            ),
        ),
    ),
    changes=(
        change(DatasetChangeType.REMOVED, CESM_CONTROL),
        change(DatasetChangeType.REMOVED, ACCESS_CONTROL),
    ),
    current=(ACCESS_HISTORICAL, CESM_HISTORICAL, MIROC_HISTORICAL),
    window_runs=(8,),
    baseline_run_id=7,
)


# ------------------------------------------------------------- 4. a week in one go
A_WEEK = summary(
    changes=(
        change(DatasetChangeType.ADDED, CESM_HISTORICAL, first_run_id=3, last_run_id=3),
        change(
            DatasetChangeType.ADDED,
            MIROC_CONTROL_RETRACTED,
            first_run_id=4,
            last_run_id=6,
        ),
        change(
            DatasetChangeType.MODIFIED,
            ACCESS_HISTORICAL,
            detail={
                "is_latest": {"old": True, "new": False},
                "data_nodes": {
                    "old": ["esgf.nci.org.au", "aims3.llnl.gov"],
                    "new": ["esgf.nci.org.au"],
                },
            },
            first_run_id=2,
            last_run_id=7,
        ),
    ),
    churned=2,
    current=EVERYTHING,
    window_runs=(1, 2, 3, 4, 5, 6, 7),
    baseline_run_id=None,
)


# ------------------------------------------------------------------ 5. too much data
MANY = summary(
    changes=tuple(
        change(
            DatasetChangeType.ADDED,
            version("CESM3", "historical", f"r{member}i1p1f1", 200 + member),
        )
        for member in range(1, 13)
    ),
    current=tuple(
        version("CESM3", "historical", f"r{member}i1p1f1", 200 + member)
        for member in range(1, 13)
    ),
)


def main() -> None:
    """Run each scene in turn."""
    show("1. a quiet morning: the search ran and nothing changed", QUIET)

    # The one you actually want to read: three arrivals, one of them already
    # retracted, something gone, and two datasets whose metadata moved.
    show("2. the interesting morning", INTERESTING)

    # Every removal below is suspect, and the health block is the only thing that
    # says so. Without it, "piControl has disappeared" reads as news.
    show("3. a run you must not misread", BROKEN_RUN)

    # Seven runs in one summary. Note that nothing appears twice, and the two
    # versions which came and went are counted rather than listed.
    show("4. a week, folded down to its net effect", A_WEEK)

    # max_rows is what keeps a summary readable when a model publishes 12 members.
    show("5. more than you can read (max_rows=4)", MANY, max_rows=4)


if __name__ == "__main__":
    main()
