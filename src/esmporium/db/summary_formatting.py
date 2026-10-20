"""
Rendering a [SearchSummary][esmporium.db.SearchSummary] as text

Separate from [esmporium.db.summaries][], which reads and shapes, because the
rendering is the part most likely to be replaced: a caller who wants markdown, HTML
or their own house layout writes their own function of a
[SearchSummary][esmporium.db.SearchSummary] and never touches the reading.

The tables are drawn by hand rather than with
[pandas.DataFrame.to_string][], deliberately. `to_string` carries index and dtype
noise, and its output moves with the terminal width and with the pandas version,
which is exactly what a checked-in regression file must not do. pandas is for the
frames; the standard library is for the text.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from esmporium.db.schema import DatasetChangeType
from esmporium.db.summaries import describe_moved

if TYPE_CHECKING:
    from collections.abc import Sequence

    from esmporium.db.summaries import ChangeRecord, SearchSummary, VersionRecord


_SECTION_HEADINGS: dict[DatasetChangeType, str] = {
    DatasetChangeType.ADDED: "ADDED",
    DatasetChangeType.REMOVED: "REMOVED",
    DatasetChangeType.MODIFIED: "MODIFIED",
}
"""What we call each change type in front of a user"""

_FACET_HEADERS: tuple[str, ...] = ("model", "experiment", "variant_label", "version")
"""
The facets every table leads with

Not every facet a dataset has: a table nobody can scan is no better than no table.
The rest are in
[SearchSummary.changes_frame][esmporium.db.SearchSummary.changes_frame] for anyone
who wants them.
"""


def _facet_cells(version: VersionRecord) -> list[str]:
    """Get the leading cells which name a version, in `_FACET_HEADERS` order."""
    return [
        version.version if name == "version" else (version.facets.get(name) or "-")
        for name in _FACET_HEADERS
    ]


def render_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    indent: str = "  ",
) -> str:
    """
    Render a fixed-width table

    Parameters
    ----------
    headers
        The column headings

    rows
        The rows, each with one cell per heading

    indent
        What to put in front of every line

    Returns
    -------
    :
        The table, with no trailing whitespace on any line. Trailing whitespace is
        stripped because our own pre-commit would strip it from a checked-in
        regression file, and then the test comparing the two would fail for no reason.

    Examples
    --------
    >>> print(render_table(["model", "n"], [["CESM3", "1"], ["ACCESS-ESM1-6", "12"]]))
      model          n
      CESM3          1
      ACCESS-ESM1-6  12
    """
    widths = [
        max(len(str(headers[column])), *(len(str(row[column])) for row in rows))
        if rows
        else len(str(headers[column]))
        for column in range(len(headers))
    ]

    lines = []
    for cells in [headers, *rows]:
        line = indent + "  ".join(
            str(cell).ljust(widths[column]) for column, cell in enumerate(cells)
        )
        lines.append(line.rstrip())

    return "\n".join(lines)


def _capped(
    rows: Sequence[Sequence[str]], max_rows: int
) -> tuple[list[list[str]], int]:
    """Cut a table down to `max_rows` rows, and say how many were left out."""
    kept = [list(row) for row in rows[:max_rows]]

    return kept, len(rows) - len(kept)


def _change_section(
    change_type: DatasetChangeType,
    changes: Sequence[ChangeRecord],
    *,
    max_rows: int,
) -> list[str]:
    """Render one change type's table, or just its count when there are none."""
    heading = _SECTION_HEADINGS[change_type]
    if not changes:
        return [f"{heading} (0)"]

    with_moved = change_type is DatasetChangeType.MODIFIED

    headers = [*_FACET_HEADERS, "retracted"]
    if with_moved:
        headers.append("what moved")

    rows = [
        [
            *_facet_cells(change.version),
            "yes" if change.version.retracted else "no",
            *([_moved_cell(change)] if with_moved else []),
        ]
        for change in changes
    ]
    kept, left_out = _capped(rows, max_rows)

    lines = [f"{heading} ({len(changes)})", render_table(headers, kept)]
    if left_out:
        lines.append(f"  ... (+{left_out} more)")

    # "There is new data" and "there is new data you must not use" are different
    # pieces of news, so an arrival which is already retracted gets said out loud
    # rather than only showing up as a `yes` in a column.
    already_retracted = sum(1 for change in changes if change.added_but_retracted)
    if already_retracted:
        lines.append(
            f"  {already_retracted} of these arrived already retracted, "
            "so they are not data you can use"
        )

    return lines


def _moved_cell(change: ChangeRecord) -> str:
    """Say what moved about a version, for the `what moved` column."""
    return describe_moved(change.detail) or "-"


def _window_lines(summary: SearchSummary) -> list[str]:
    """Say which runs this summary covers, and what it is measured against."""
    runs = summary.window_runs
    if len(runs) == 1:
        covered = f"run {runs[0]}"
    else:
        covered = f"runs {runs[0]}-{runs[-1]} ({len(runs)} runs)"

    started = summary.run.started_at.strftime("%Y-%m-%d %H:%M UTC")
    against = (
        f"run {summary.baseline_run_id}"
        if summary.baseline_run_id is not None
        else "nothing (this is the first run)"
    )

    return [f"{covered}, newest started {started}", f"compared against {against}"]


def _health_lines(summary: SearchSummary) -> list[str]:
    """Say whether the newest run's answer can be believed."""
    health = summary.run
    lines = ["RUN HEALTH"]

    if not health.leaves:
        lines.append("  no sub-searches recorded for this run")

        return lines

    lines.append(
        f"  {health.n_answered}/{health.n_leaves} sub-searches answered"
        + (f"   host: {', '.join(health.hosts)}" if health.hosts else "")
    )

    if health.n_matches is None:
        silent = sum(1 for leaf in health.leaves if leaf.n_matches is None)
        lines.append(
            f"  {health.n_documents} documents collected; "
            f"{silent} of {health.n_leaves} sub-searches reported no total"
        )
    elif health.n_documents < health.n_matches:
        lines.append(
            f"  {health.n_documents}/{health.n_matches} documents collected "
            "-- TRUNCATED, so a removal here may not be real"
        )
    else:
        lines.append(
            f"  {health.n_documents}/{health.n_matches} documents collected "
            "(nothing truncated)"
        )

    for leaf in health.failures:
        why = leaf.failure or "no endpoint answered"
        lines.append(f"  FAILED  {leaf.label}: {why}")

    for leaf in health.truncated:
        lines.append(
            f"  TRUNCATED  {leaf.label}: "
            f"{leaf.n_documents} of {leaf.n_matches} collected"
        )

    if not health.complete:
        lines.extend(
            [
                "  this run is incomplete, so it will not be used as a baseline:",
                "  the next run will compare itself against an earlier one",
            ]
        )

    return lines


def _current_lines(summary: SearchSummary, *, max_rows: int) -> list[str]:
    """Render what the newest run returns now."""
    current = summary.current
    if not current:
        return ["CURRENT STATE", "  the newest run returned nothing"]

    headers = [*_FACET_HEADERS, "retracted", "nodes"]
    rows = [
        [
            *_facet_cells(version),
            "yes" if version.retracted else "no",
            str(len(version.data_nodes)),
        ]
        for version in current
    ]
    kept, left_out = _capped(rows, max_rows)

    n_datasets = len({version.dataset_id for version in current})
    counted = (
        f"{len(current)} datasets"
        if len(current) == n_datasets
        else f"{len(current)} versions of {n_datasets} datasets"
    )
    lines = [
        f"CURRENT STATE ({counted})",
        render_table(headers, kept),
    ]
    if left_out:
        lines.append(f"  ... (+{left_out} more)")

    return lines


def format_summary(summary: SearchSummary, *, max_rows: int = 20) -> str:
    """
    Render a summary as text

    Written for somewhere a person is looking: a terminal, a script's log, a notebook
    cell. Every line is free of trailing whitespace, and the whole thing ends in
    exactly one newline, so it can be written to a file our pre-commit will not then
    want to change.

    Parameters
    ----------
    summary
        What to render

    max_rows
        How many rows of each table to show. The rest are summarised as
        `... (+N more)`, because a watched search can return thousands of datasets and
        the point of a summary is to be read.

    Returns
    -------
    :
        The rendered summary
    """
    blocks: list[list[str]] = [
        [summary.headline],
        _window_lines(summary),
    ]

    if summary.any_changes:
        for change_type in _SECTION_HEADINGS:
            blocks.append(
                _change_section(
                    change_type,
                    [
                        change
                        for change in summary.changes
                        if change.change_type is change_type
                    ],
                    max_rows=max_rows,
                )
            )
    else:
        blocks.append(["NO CHANGES"])

    if summary.churned:
        blocks.append(
            [
                f"{summary.churned} versions moved inside the window and ended up "
                "back where they started"
            ]
        )

    blocks.append(_health_lines(summary))
    blocks.append(_current_lines(summary, max_rows=max_rows))

    rendered = "\n\n".join("\n".join(block) for block in blocks)

    return "\n".join(line.rstrip() for line in rendered.splitlines()) + "\n"
