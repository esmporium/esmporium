"""
Reading a search document's dataset facet rows, in our terms

This is the *project* half of result parsing (the *format* half lives on the
[`SearchAPI`][esmporium.search.apis.SearchAPI]). It turns one raw search document
into the `Dataset` facet rows it maps to, named as our columns rather than the API's.

The work is almost entirely data-driven: a facade's parameters already know the
mapping from our facet columns to the API's field names (that is how requests are
built), so we reuse that same mapping to read the answer back. The only piece that is
not a plain rename is the varying axis -- CMIP5 bundles many variables into one
document, so `variable` is read as a list and the document explodes into one row per
variable, while every other project yields a single row.

`project` is handled by the caller (the facade parameters) rather than here, because
the Solr and STAC families disagree on where it comes from: Solr carries a `project`
facet, whereas STAC drops it (project is the collection) and it must be recovered from
`mip_era`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from esmporium.search.apis import SearchAPI
    from esmporium.search.search_api_facade.parameters import FacadeParametersProtocol

#: The non-`variable` `Dataset` facet columns, read as one scalar each per row. These
#: match [`esmporium.db.schema.DATASET_FACET_COLUMNS`][] minus `variable`; the `db`
#: layer's schema (NOT NULL columns) and the golden-parity test keep the two in step.
RESULT_SCALAR_FACET_COLUMNS: tuple[str, ...] = (
    "project",
    "model",
    "institution",
    "experiment",
    "variant_label",
    "reporting_interval",
    "grid_label",
    "processing_id",
)

#: The varying axis: read as a list, so a CMIP5 bundle explodes into one row per var.
RESULT_VARIABLE_COLUMN = "variable"

RESULT_FACET_COLUMNS: tuple[str, ...] = (
    *RESULT_SCALAR_FACET_COLUMNS,
    RESULT_VARIABLE_COLUMN,
)


def read_dataset_rows(
    parameters: FacadeParametersProtocol,
    doc: dict[str, Any],
    api: SearchAPI,
    project: str | None,
) -> tuple[dict[str, str | None], ...]:
    """
    Read the `Dataset` facet rows one document maps to

    Parameters
    ----------
    parameters
        The facade parameters, used only for their column -> API field-name mapping

    doc
        One document from [`SearchAPI.extract_result_documents`][esmporium.search.apis.SearchAPI.extract_result_documents]

    api
        The search API the document came from, used to read its fields

    project
        The project value for these rows, resolved by the caller (see the module
        docstring for why it is not read here)

    Returns
    -------
    :
        One full facet dict per dataset row (one per variable for CMIP5, one
        otherwise). Each dict has the columns in [RESULT_FACET_COLUMNS][(m).];
        `id_project_specific` is added later, by `ParsedDocument.dataset_facets`.
    """  # noqa: E501
    field_of = parameters.get_mapping_to_api_facet_names(set(RESULT_FACET_COLUMNS))

    row_base: dict[str, str | None] = {"project": project}
    for column in RESULT_SCALAR_FACET_COLUMNS:
        if column == "project":
            continue
        api_field = field_of.get(column)
        # A column with no API name for this project (e.g. CMIP5 has no grid_label) is
        # left NULL, which is exactly how the old parser handled it.
        row_base[column] = (
            api.read_facet(doc, api_field) if api_field is not None else None
        )

    variable_field = field_of[RESULT_VARIABLE_COLUMN]
    return tuple(
        {**row_base, RESULT_VARIABLE_COLUMN: variable}
        for variable in api.read_facet_list(doc, variable_field)
    )
