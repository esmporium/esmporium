"""
Definition of the interface for parsing search result documents into dataset rows

A doc parser is the result-reading counterpart of the facade parameters
([esmporium.search.search_api_facade.parameters][]): where the parameters know *which*
API field carries each facet for a project, the doc parser knows the *shape* of a
document for a project and format -- above all, how many dataset rows one document maps
to. A CMIP5 Solr document bundles many variables and explodes into one row per variable;
a CMIP6/CMIP7 document maps to a single row and any extra variable value is a loud
error, not a silent extra row.

Keeping this as its own collaborator (rather than special-casing one facet inside the
facade) means each project-and-format's document shape is stated once, in the open,
instead of every parser behaving as though every document might bundle variables.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from esmporium.search.result_parsing import DatasetFacets

if TYPE_CHECKING:
    from collections.abc import Collection

    from esmporium.search.apis import SearchAPI
    from esmporium.search.search_api_facade.parameters import (
        FacadeParametersProtocol,
    )


def get_single_value_columns_from_doc(
    doc: dict[str, Any],
    search_api: SearchAPI,
    facade_parameters: FacadeParametersProtocol,
    exclude: Collection[str],
) -> dict[str, str | None]:
    """
    Read the single-valued facet columns of one document

    This is the shared workhorse of the doc parsers: every
    [`DatasetFacets`][esmporium.search.result_parsing.DatasetFacets] column except those
    in `exclude` is read as a scalar via
    [search_api.read_facet][esmporium.search.apis.SearchAPI.read_facet]. Because
    `read_facet` now raises on a multi-valued field, a document that unexpectedly carries
    several values for a column meant to be single-valued fails loudly here.

    Parameters
    ----------
    doc
        One document from
        [search_api.extract_result_documents][esmporium.search.apis.SearchAPI.extract_result_documents]

    search_api
        The search API the document came from, used to read its fields

    facade_parameters
        The facade parameters that name which API field carries each column

    exclude
        Columns to leave out (e.g. those the caller fills in itself, or a multi-valued
        axis a parser reads separately)

    Returns
    -------
    :
        The value of each read column, keyed by
        [`DatasetFacets`][esmporium.search.result_parsing.DatasetFacets] column name.

        A column the project has no API name for (e.g. CMIP5 has no `grid_label`) is left
        out entirely, so the model's default applies rather than an explicit `None`.
    """  # noqa: E501
    columns_to_get = set(DatasetFacets.model_fields) - set(exclude)
    field_of = facade_parameters.get_mapping_to_api_facet_names(columns_to_get)

    res: dict[str, str | None] = {}
    for column in columns_to_get:
        api_field = field_of.get(column)
        # A column with no API name for this project is left unset, so DatasetFacets'
        # default applies -- fine for the one optional facet, and a loud error at
        # construction for any required one.
        if api_field is not None:
            res[column] = search_api.read_facet(doc, api_field)

    return res


class DocParserProtocol(Protocol):
    """
    A parser of search result documents into dataset-row column dictionaries
    """

    def get_dataset_rows_from_doc(
        self,
        doc: dict[str, Any],
        *,
        api: SearchAPI,
        facade_parameters: FacadeParametersProtocol,
        exclude_columns: Collection[str],
    ) -> tuple[dict[str, str | None], ...]:
        """
        Read the dataset rows one document maps to, as column dictionaries

        The facade fills in the columns it owns (`id_project_specific` and `project`) and
        passes them as `exclude_columns` so this parser does not read them; every other
        [`DatasetFacets`][esmporium.search.result_parsing.DatasetFacets] column is this
        parser's concern.

        Parameters
        ----------
        doc
            One document from
            [api.extract_result_documents][esmporium.search.apis.SearchAPI.extract_result_documents]

        api
            The search API the document came from, used to read its fields

        facade_parameters
            The facade parameters that name which API field carries each column

        exclude_columns
            Columns the caller fills in itself, so this parser must not read them

        Returns
        -------
        :
            One column dictionary per dataset row (one per variable for a CMIP5 bundle,
            one otherwise), each keyed by
            [`DatasetFacets`][esmporium.search.result_parsing.DatasetFacets] column name
        """  # noqa: E501
        ...
