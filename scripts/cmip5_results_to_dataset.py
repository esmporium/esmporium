"""
Live CMIP5 search -> what a result looks like, and how it becomes a `Dataset` row

Searches ESGF for CMIP5 `tas` / monthly / `historical` and prints:

1. the Dataset-level record  -> `variable` is a LIST of every variable in the
   dataset, so it cannot tell us which row is `tas`;
2. the File-level record     -> `variable` is a single value, `['tas']`. This is
   the "layer down" CMIP5 needs, and it is just a search parameter (`type=File`);
3. the `Dataset` row we would store, with an `id` built from the columns (not from
   ESGF's `master_id`), using a placeholder in the `grid_label` slot;
4. every field that is NOT a `Dataset` column, grouped by where it belongs:
   version-specific, node-specific, CMIP5-specific, or "no clear home yet".

This is a hand-run visualisation, not library code. It talks to ESGF directly with
`httpx` on purpose: no canonical/native translation, so what you see is what the
node sent.

Run it:  uv run python scripts/cmip5_results_to_dataset.py
"""

from __future__ import annotations

from typing import Any

import httpx

HOST = "esgf.nci.org.au"
SEARCH_URL = f"https://{HOST}/esg-search/search"

# The search that defines this exploration.
BASE_QUERY = {
    "project": "CMIP5",
    "experiment": "historical",
    "time_frequency": "mon",
    "variable": "tas",
    "ensemble": "r1i1p1",  # pinned only to keep the result set small and readable
    "format": "application/solr+json",
    "distrib": "false",
    "limit": 1,
}

# CMIP5's Solr field name -> our `Dataset` column name. `grid_label` is absent on
# purpose: CMIP5 has no grid concept, so nothing on the record fills it. `model`
# is absent too: we take it from the DRS id instead (see `drs_model`), because the
# `model` facet is the display name (e.g. `BCC-CSM1.1(m)`) and we want the lower
# case DRS token (e.g. `bcc-csm1-1-m`), which has no dots to confuse our ids.
FIELD_TO_COLUMN = {
    "project": "project",
    "institute": "institution",
    "experiment": "experiment",
    "ensemble": "variant_label",
    "variable": "variable",
    "time_frequency": "reporting_interval",
    "cmor_table": "processing_id",
}

# The order the columns go into the id we build for ourselves. This mirrors
# `DATASET_FACET_COLUMNS` in the schema, and keeps `grid_label` in place so the id
# is well-formed even for CMIP5, where the column itself is NULL.
ID_COLUMN_ORDER = (
    "project",
    "model",
    "institution",
    "experiment",
    "variant_label",
    "variable",
    "reporting_interval",
    "grid_label",
    "processing_id",
)

# CMIP5 has no grid, so the id's grid slot gets a stand-in. Kept out of the
# `grid_label` column (which stays None); it exists only to keep ids well-formed.
GRID_LABEL_PLACEHOLDER = "0"

# CMIP5's DRS id is dot-separated as:
#   cmip5.<product>.<institute>.<model>.<experiment>.<frequency>.<realm>.<table>...
# so the lower-case model token is at position 3. We read it from there rather
# than trusting the `model` display facet.
DRS_MODEL_INDEX = 3

# The longest a rendered field value may be before we truncate it (step 4 display).
MAX_VALUE_CHARS = 70

# --- How we sort the fields that are NOT a Dataset column (step 4). ------------
# These groupings are our reading of the fields, from looking at live records;
# ESGF does not label them this way. Anything not listed lands in "no clear home".

# Belongs to a published *version* of a dataset (changes when a new version comes
# out): the version string, whether it is the latest/retracted, and the exact
# content of that version (size, file count, checksums, tracking id).
VERSION_FIELDS = {
    "version",
    "latest",
    "retracted",
    "number_of_files",
    "size",
    "timestamp",
    "_timestamp",
    "checksum",
    "checksum_type",
    "tracking_id",
}

# Belongs to a *copy on a data node* (one version can live on several nodes):
# which node, whether this copy is a replica, and how to reach it.
NODE_FIELDS = {
    "data_node",
    "index_node",
    "replica",
    "access",
    "url",
    "number_of_aggregations",
}

# Specific to the CMIP5 project vocabulary: has no place in a generic Dataset.
CMIP5_FIELDS = {
    "product",
    "experiment_family",
    "dataset_id_template_",
    "directory_format_template_",
}

# ESGF identifiers. We build our own id from the columns, but we keep these to
# trace a row back to ESGF and to stitch file records to their parent dataset.
IDENTIFIER_FIELDS = {"id", "master_id", "instance_id", "dataset_id"}

# Pure search/index noise: a relevance score, Solr's internal doc version, and
# the record type. Not worth storing.
IGNORE_FIELDS = {"score", "_version_", "type"}


def one(value: Any) -> Any:
    """Unwrap a one-element list, e.g. `['CMIP5']` -> `'CMIP5'`, else leave as-is."""
    if isinstance(value, list) and len(value) == 1:
        return value[0]
    return value


def drs_model(doc: dict[str, Any]) -> str:
    """Read the lower-case DRS model token out of a record's `instance_id`."""
    instance_id = one(doc.get("instance_id", ""))
    return instance_id.split(".")[DRS_MODEL_INDEX]


def search(client: httpx.Client, *, record_type: str) -> dict[str, Any]:
    """
    Run the CMIP5 search at a given granularity and return the first record

    Parameters
    ----------
    client
        The HTTP client to search with

    record_type
        `"Dataset"` for dataset-level records, `"File"` for file-level records

    Returns
    -------
    :
        The response's `numFound` and its first record (or `None` if empty)
    """
    response = client.get(SEARCH_URL, params={**BASE_QUERY, "type": record_type})
    response.raise_for_status()
    body = response.json()["response"]
    docs = body["docs"]
    return {"num_found": body["numFound"], "doc": docs[0] if docs else None}


def fetch_parent_dataset(
    client: httpx.Client, file_doc: dict[str, Any]
) -> dict[str, Any] | None:
    """
    Fetch the dataset a file belongs to, so the two levels can be compared fairly

    A plain "first dataset" and "first file" search return unrelated datasets, so
    their ids and versions differ just because they are different data. Here we
    follow the file's `dataset_id` back to its own parent, so any remaining
    difference is a real Dataset-vs-File difference.

    Parameters
    ----------
    client
        The HTTP client to search with

    file_doc
        A file-level record, whose `dataset_id` names its parent

    Returns
    -------
    :
        The parent dataset-level record, or `None` if it could not be found
    """
    # `dataset_id` is `<instance_id>|<data_node>`; the instance_id (with version)
    # uniquely names the parent dataset.
    parent_instance_id = one(file_doc["dataset_id"]).split("|")[0]
    response = client.get(
        SEARCH_URL,
        params={
            "instance_id": parent_instance_id,
            "type": "Dataset",
            "format": "application/solr+json",
            "distrib": "false",
            "limit": 1,
        },
    )
    response.raise_for_status()
    docs = response.json()["response"]["docs"]
    return docs[0] if docs else None


def show_dataset_level(doc: dict[str, Any]) -> None:
    """Show why the dataset-level record cannot give us a single variable."""
    variables = doc.get("variable", [])
    print("  master_id :", one(doc.get("master_id")))
    print(f"  variable  : a list of {len(variables)} values -> {variables[:3]} ...")
    print("  -> bundles every variable; cannot isolate `tas` from here")


def build_row(file_doc: dict[str, Any]) -> dict[str, Any]:
    """
    Turn one file-level record into the `Dataset` row we would store

    Parameters
    ----------
    file_doc
        A file-level CMIP5 record (its `variable` is a single value)

    Returns
    -------
    :
        The `Dataset` columns, plus the `id` we build from them
    """
    # Pull each column straight off the record, no translation layer...
    row: dict[str, Any] = {
        column: one(file_doc[field])
        for field, column in FIELD_TO_COLUMN.items()
        if field in file_doc
    }
    # ...except `model`, which we take as the lower-case DRS token, and
    # `grid_label`, which CMIP5 has no value for.
    row["model"] = drs_model(file_doc)
    row["grid_label"] = None

    # Build our own id from the columns, filling the grid slot with the stand-in.
    # We deliberately do NOT reuse ESGF's `master_id` (it has no variable in it).
    id_parts = [
        str(row.get(column) or GRID_LABEL_PLACEHOLDER) for column in ID_COLUMN_ORDER
    ]
    row["id"] = ".".join(id_parts)
    return row


def show_row(file_doc: dict[str, Any]) -> None:
    """Show the file-level record and the `Dataset` row it produces."""
    print("  variable  :", file_doc.get("variable"), "  <- a single variable")
    print("  dataset_id:", one(file_doc.get("dataset_id")), " (the parent dataset)")
    print("\n  the Dataset row we would store:")
    row = build_row(file_doc)
    print(f"    {'id':20} = {row['id']}   <- built from the columns")
    for column in ID_COLUMN_ORDER:
        value = row.get(column)
        note = "  (NULL for CMIP5; id used a placeholder)" if value is None else ""
        print(f"    {column:20} = {value}{note}")


def classify_field(field: str) -> str:
    """Say which group a non-Dataset field belongs to (its print heading)."""
    if field in VERSION_FIELDS:
        return "version-specific  (-> a future version table)"
    if field in NODE_FIELDS:
        return "node-specific     (-> a future data-node table)"
    if field in CMIP5_FIELDS:
        return "CMIP5-specific    (project vocabulary, no generic home)"
    if field in IDENTIFIER_FIELDS:
        return "ESGF identifiers  (kept to trace/stitch, not a column)"
    if field in IGNORE_FIELDS:
        return "ignored           (search/index noise)"
    return "no clear home yet (describes the data itself; decide later)"


def fmt_value(value: Any) -> str:
    """Render a field value compactly: unwrap and truncate long values."""
    unwrapped = one(value)
    if isinstance(unwrapped, list):
        text = f"[{len(unwrapped)}] {unwrapped[:2]}"
    else:
        text = str(unwrapped)
    return text if len(text) <= MAX_VALUE_CHARS else text[:MAX_VALUE_CHARS] + "..."


def field_value_display(
    field: str, dataset_doc: dict[str, Any], file_doc: dict[str, Any]
) -> str:
    """
    Show a field's value, noting when the Dataset and File levels disagree

    Parameters
    ----------
    field
        The field to show

    dataset_doc
        The dataset-level record

    file_doc
        The file-level record (the one a stored row comes from)

    Returns
    -------
    :
        The value to print, from the file level where present, else the dataset
        level; when both carry it and they differ, both are shown.
    """
    in_file = field in file_doc
    in_dataset = field in dataset_doc
    file_value = fmt_value(file_doc.get(field))
    dataset_value = fmt_value(dataset_doc.get(field))

    if in_file and in_dataset and file_value != dataset_value:
        return f"{file_value}   (dataset-level: {dataset_value})"
    if in_file:
        return file_value
    return f"{dataset_value}  [dataset-level only]"


def show_non_dataset_fields(
    dataset_doc: dict[str, Any], file_doc: dict[str, Any]
) -> None:
    """
    Group every field that is NOT a Dataset column by where it belongs, with values

    Parameters
    ----------
    dataset_doc
        The dataset-level record

    file_doc
        The file-level record

    Both are pooled so the picture is complete: some fields appear on only one of
    them, and some (version, replica, size) carry different values at each level.
    """
    mapped = set(FIELD_TO_COLUMN) | {"model"}
    all_fields = (set(dataset_doc) | set(file_doc)) - mapped

    groups: dict[str, list[str]] = {}
    for field in sorted(all_fields):
        heading = classify_field(field)
        value = field_value_display(field, dataset_doc, file_doc)
        groups.setdefault(heading, []).append(f"    {field:26} = {value}")

    for heading in (
        "version-specific  (-> a future version table)",
        "node-specific     (-> a future data-node table)",
        "CMIP5-specific    (project vocabulary, no generic home)",
        "ESGF identifiers  (kept to trace/stitch, not a column)",
        "no clear home yet (describes the data itself; decide later)",
        "ignored           (search/index noise)",
    ):
        rows = groups.get(heading)
        if rows:
            print(f"  {heading}:")
            print("\n".join(rows))
            print()


def main() -> None:
    """Search CMIP5 at both levels and show how a result becomes a Dataset row."""
    print("Searching ESGF (CMIP5, historical, monthly, tas)\n")
    with httpx.Client(follow_redirects=True, timeout=60.0) as client:
        # Start from one tas file, then follow it back to its own parent dataset,
        # so every comparison below is for the same dataset.
        file = search(client, record_type="File")
        if file["doc"] is None:
            print("no file-level records came back; try re-running")
            return
        file_doc = file["doc"]
        dataset_doc = fetch_parent_dataset(client, file_doc)

    print("STEP 1 - the file's parent dataset-level record")
    if dataset_doc is not None:
        show_dataset_level(dataset_doc)

    print(
        f"\nSTEP 2 - File-level record: the 'layer down' (numFound={file['num_found']})"
    )
    show_row(file_doc)

    print("\nSTEP 3 - fields that are NOT a Dataset column, grouped by where they go")
    print(
        "         (file-level value shown; dataset-level noted only where it differs)\n"
    )
    show_non_dataset_fields(dataset_doc or {}, file_doc)


if __name__ == "__main__":
    main()
