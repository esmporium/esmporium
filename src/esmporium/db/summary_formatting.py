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

from esmporium.datasets import DATASET_FACET_COLUMNS
from esmporium.db.schema import DatasetChangeType
from esmporium.db.summaries import describe_moved
from esmporium.formatting import readable_list

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from esmporium.db.summaries import ChangeRecord, SearchSummary, VersionRecord


_SECTION_HEADINGS: dict[DatasetChangeType, str] = {
    DatasetChangeType.ADDED: "ADDED",
    DatasetChangeType.REMOVED: "REMOVED",
    DatasetChangeType.MODIFIED: "MODIFIED",
}
"""What we call each change type in front of a user"""

DEFAULT_FACETS: tuple[str, ...] = (
    "project",
    "model",
    "experiment",
    "variant_label",
    "variable",
    "reporting_interval",
)
"""
The facets every table shows unless it is told otherwise

A subset of [DATASET_FACET_COLUMNS][esmporium.datasets.DATASET_FACET_COLUMNS], chosen
to cover the facets a search is most often spread across: several projects, several
models, several experiments, several ensemble members, several variables, several
frequencies. `institution` is left out because `model` implies it, `grid_label` and
`processing_id` because they rarely vary within one search.

This is a readability default, not a correctness one: rows are told apart by the `id`
column, which is always shown, so narrowing this can never make two datasets
indistinguishable. Widen it with the `facets` argument to
[format_summary][(m).] when a search does vary in something else:

    summary.render(facets=(*DEFAULT_FACETS, "grid_label"))

Everything is in
[SearchSummary.changes_frame][esmporium.db.SearchSummary.changes_frame] regardless.
"""

_SHORT_HEADERS: dict[str, str] = {
    "variant_label": "variant",
    "reporting_interval": "interval",
    "processing_id": "table",
    "id_project_specific": "native id",
}
"""
Shorter headings for the columns whose canonical names are too wide to tabulate

`reporting_interval` is an 18-character heading over 3-character values. The frames
keep the canonical names; this is only what a heading reads as.
"""


class MisalignedRowError(ValueError):
    """Raised when a table's rows do not match its headings"""

    def __init__(self, *, n_headers: int, row_lengths: Sequence[int]) -> None:
        """
        Initialise

        Parameters
        ----------
        n_headers
            How many headings the table has

        row_lengths
            The lengths of the rows which do not match
        """
        self.n_headers = n_headers
        """How many headings the table has"""

        self.row_lengths = tuple(row_lengths)
        """The lengths of the rows which do not match"""

        msg = (
            f"A table with {n_headers} headings was given "
            f"{len(self.row_lengths)} rows which do not match it, of lengths "
            f"{sorted(set(self.row_lengths))}. "
            "Build the headings and the cells from the same column list, "
            "so that a value cannot end up under the wrong heading."
        )
        super().__init__(msg)


class UnknownFacetError(ValueError):
    """Raised when a summary is asked to show a facet no dataset records"""

    def __init__(self, unknown: Sequence[str]) -> None:
        """
        Initialise

        Parameters
        ----------
        unknown
            The facet names which are not dataset facets
        """
        self.unknown = tuple(unknown)
        """The facet names which are not dataset facets"""

        msg = (
            f"Cannot show {readable_list(self.unknown)}: "
            "a summary can only show the facets a dataset records, which are "
            f"{readable_list(DATASET_FACET_COLUMNS)}. "
            "Facets only a search document knows are not available here; read them "
            "off the raw documents instead."
        )
        super().__init__(msg)


def check_facets(facets: Sequence[str] | None) -> tuple[str, ...]:
    """
    Resolve and check the facets a table should show

    Parameters
    ----------
    facets
        The facets asked for, or `None` for [DEFAULT_FACETS][(m).]

    Returns
    -------
    :
        The facets to show, in [DATASET_FACET_COLUMNS][esmporium.datasets.DATASET_FACET_COLUMNS]
        order so that two tables of the same summary line up

    Raises
    ------
    UnknownFacetError
        One of the names is not a dataset facet
    """  # noqa: E501 - an unwrapped cross-reference
    if facets is None:
        return DEFAULT_FACETS

    asked = tuple(facets)
    unknown = [name for name in asked if name not in DATASET_FACET_COLUMNS]
    if unknown:
        raise UnknownFacetError(unknown)

    return tuple(name for name in DATASET_FACET_COLUMNS if name in asked)


def _row_columns(
    facets: Sequence[str],
) -> tuple[tuple[str, Callable[[VersionRecord], str]], ...]:
    """
    Get the columns which name a version, as `(heading, how to read it)` pairs

    One list feeding both the heading row and every cell, so that a value cannot end
    up under the wrong heading: if the headings and the cells were built from the
    facet list separately, the two could be ordered differently and nothing would
    say so.

    `id` leads, and is the reason narrowing `facets` is safe: two datasets can agree
    on every facet we model and still be different rows (see the uniqueness index on
    [Dataset][esmporium.db.schema.Dataset], which includes `id_project_specific`), so
    the only honest way to tell rows apart is to carry an identity.
    """
    columns: list[tuple[str, Callable[[VersionRecord], str]]] = [
        ("id", lambda version: str(version.dataset_version_id))
    ]
    columns.extend(
        (
            _SHORT_HEADERS.get(facet, facet),
            lambda version, facet=facet: version.facets.get(facet) or "-",  # type: ignore[misc]
        )
        for facet in facets
    )
    columns.append(("version", lambda version: version.version))
    columns.append(("retracted", lambda version: "yes" if version.retracted else "no"))

    return tuple(columns)


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

    Raises
    ------
    MisalignedRowError
        A row does not have one cell per heading
    """
    ragged = [len(row) for row in rows if len(row) != len(headers)]
    if ragged:
        raise MisalignedRowError(n_headers=len(headers), row_lengths=ragged)

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
    facets: Sequence[str],
) -> list[str]:
    """Render one change type's table, or just its count when there are none."""
    heading = _SECTION_HEADINGS[change_type]
    if not changes:
        return [f"{heading} (0)"]

    columns = _row_columns(facets)
    headers = [header for header, _ in columns]
    rows = [[read(change.version) for _, read in columns] for change in changes]
    kept, left_out = _capped(rows, max_rows)

    # `what moved` goes under its row rather than in a column of its own: it is the
    # one free-text field here, and as a column it set the table's width on its own.
    moved_under = {
        index: describe_moved(change.detail)
        for index, change in enumerate(changes[: len(kept)])
        if describe_moved(change.detail)
    }

    lines = [f"{heading} ({len(changes)})"]
    if moved_under:
        table = render_table(headers, kept).splitlines()
        lines.append(table[0])
        for index, row in enumerate(table[1:]):
            lines.append(row)
            if index in moved_under:
                lines.append(f"         {moved_under[index]}")
    else:
        lines.append(render_table(headers, kept))

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


def _current_lines(
    summary: SearchSummary, *, max_rows: int, facets: Sequence[str]
) -> list[str]:
    """Render what the newest run returns now."""
    current = summary.current
    if not current:
        return ["CURRENT STATE", "  the newest run returned nothing"]

    columns = (*_row_columns(facets), ("nodes", lambda v: str(len(v.data_nodes))))
    headers = [header for header, _ in columns]
    rows = [[read(version) for _, read in columns] for version in current]
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


def format_summary(
    summary: SearchSummary,
    *,
    max_rows: int = 20,
    facets: Sequence[str] | None = None,
) -> str:
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

    facets
        Which dataset facets to put in the tables. `None` uses
        [DEFAULT_FACETS][(m).].

        The `id`, `version` and `retracted` columns are always shown, so narrowing
        this never makes two rows indistinguishable; it only makes them less
        self-describing.

    Returns
    -------
    :
        The rendered summary

    Raises
    ------
    UnknownFacetError
        `facets` names something which is not a dataset facet
    """
    shown = check_facets(facets)
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
                    facets=shown,
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
    blocks.append(_current_lines(summary, max_rows=max_rows, facets=shown))

    rendered = "\n\n".join("\n".join(block) for block in blocks)

    return "\n".join(line.rstrip() for line in rendered.splitlines()) + "\n"
