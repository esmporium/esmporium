"""
Definition of the interface for parsing search results into the pieces we store
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

from esmporium.search.result_parsing import DatasetFacets

if TYPE_CHECKING:
    from collections.abc import Collection

    from esmporium.search.apis import SearchAPI
    from esmporium.search.result_parsing import ParsedDocument
    from esmporium.search.search_api_facade.parameters import (
        FacadeParametersProtocol,
    )

NMatchesReader = Callable[[dict[str, Any]], int]
"""
Reads how many records matched a search out of a raw response

Where that is written can be an endpoint's own choice rather than the format's, which
is why this is something a result parser is given rather than something it knows.
"""


def get_single_value_columns_from_doc(
    doc: dict[str, Any],
    search_api: SearchAPI,
    facade_parameters: FacadeParametersProtocol,
    exclude: Collection[str],
) -> dict[str, str | None]:
    """
    Read the single-valued facet columns of one document

    This is the shared workhorse of the result parsers:
    every [`DatasetFacets`][esmporium.search.result_parsing.DatasetFacets]
    column except those in `exclude` is read as a scalar via
    [search_api.read_facet][esmporium.search.apis.SearchAPI.read_facet].
    Because `read_facet` raises on a multi-valued field,
    a document that unexpectedly carries several values for a column
    meant to be single-valued fails loudly here.

    Parameters
    ----------
    doc
        One document

        Usually extracted with
        [search_api.extract_result_documents][esmporium.search.apis.SearchAPI.extract_result_documents].

    search_api
        The search API the document came from, used to read `doc`'s fields

    facade_parameters
        The facade parameters that name which API field carries each dataset column

    exclude
        Columns to leave out

        For example, columns the parser fills in itself, or a multi-valued axis it reads separately.

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


class ResultParserProtocol(Protocol):
    """
    A parser of one project's search results from one search API
    """

    def get_n_matches(self, raw: dict[str, Any]) -> int:
        """
        Get the number of records that matched a search from a raw response

        Note: this is not necessarily the same as the number of results in `raw`.
        Some search APIs will only return a limited number of results.
        This method returns the total number of records which matched the search,
        which can be much higher than the number of results returned in `raw`.

        This lives here rather than on the search API because where the total is
        written can be an endpoint's own choice: the two ESGF-NG deployments speak the
        same format and still disagree about it.
        We might move this back to the search API
        if the two start speaking the same language again
        (it really should be defined by the STAC format, not implementation).

        Parameters
        ----------
        raw
            The response to read, i.e. the answer to a request built with
            [SearchAPI.build_search_request][esmporium.search.apis.SearchAPI.build_search_request]

        Returns
        -------
        :
            The number of records that matched the search

        Raises
        ------
        NoSearchResultNumberOfMatchesReturnedError
            `raw` does not report the number of records that matched the search
        """
        ...

    def parse_search_results(
        self,
        raw: dict[str, Any],
        *,
        api: SearchAPI,
        facade_parameters: FacadeParametersProtocol,
    ) -> tuple[ParsedDocument, ...]:
        """
        Parse a raw search response into the documents it carries

        Parameters
        ----------
        raw
            The response to read

            Usually the answer to a request built with
            [SearchAPI.build_search_request][esmporium.search.apis.SearchAPI.build_search_request].

        api
            The search API the response came from,
            used to split it into documents and to read fields out of them

        facade_parameters
            The facade parameters that name which API field carries each column.

        Returns
        -------
        :
            Parsed documents
        """
        ...

    def get_dataset_rows(
        self,
        doc: dict[str, Any],
        *,
        api: SearchAPI,
        facade_parameters: FacadeParametersProtocol,
    ) -> tuple[DatasetFacets, ...]:
        """
        Read the complete dataset rows one search document maps to

        Parameters
        ----------
        doc
            One document

            Usually from
            [api.extract_result_documents][esmporium.search.apis.SearchAPI.extract_result_documents].

        api
            The search API the document came from, used to read its fields

        facade_parameters
            The facade parameters that name which API field carries each column

        Returns
        -------
        :
            Extracted dataset facets
        """
        ...
