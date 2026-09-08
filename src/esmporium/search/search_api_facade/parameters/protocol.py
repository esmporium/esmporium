"""
Definition of interface for facade parameters

These are different from queries,
because the translation to API facade parameters
is better expressed as more than just a pure mapping.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Protocol

from pydantic import PlainValidator

from esmporium.query import QueryCanonical, QueryProtocol
from esmporium.query.protocol import accept_without_validation

if TYPE_CHECKING:
    from esmporium.search.apis import SearchAPI


class FacadeParametersProtocol(Protocol):
    """
    A facade parameter definition we support
    """

    base_query_style: Annotated[
        type[QueryProtocol], PlainValidator(accept_without_validation)
    ]
    """
    Base query style

    This supplies the query style parameter names.
    Getting from those to the API parameter names
    can involve an extra layer on top of this base style
    (e.g. ESGF-NG's collection prefix).
    """

    def get_facet_values_request_facet_names(
        self, canonical: QueryCanonical, facets: set[str]
    ) -> set[str]:
        """
        Get the facet names to use in a facet values request

        Like everywhere else, mapping is only supported from canonical names
        or from facets this query style names
        which have no canonical equivalent.

        Parameters
        ----------
        canonical
            Canonical query for which to get the facet names

        facets
            The facets names of interest

        Returns
        -------
        :
            API parameter names to use in a facet values request
        """
        ...

    def get_mapping_to_api_facet_names(self, facets: set[str]) -> dict[str, str]:
        """
        Get the mapping from input names to names used by the API

        Like everywhere else, mapping is only supported from canonical names
        or from facets this query style names
        which have no canonical equivalent.

        Parameters
        ----------
        facets
            Facets for which to get the mapping

        Returns
        -------
        :
            Mapping from values in `facets` to the API parameter name.
        """
        ...

    def get_search_request_facet_values(
        self, canonical: QueryCanonical
    ) -> dict[str, tuple[str, ...]]:
        """
        Get the facet values to use in a search request

        Parameters
        ----------
        canonical
            Canonical query for which to get the search request facet values

        Returns
        -------
        :
            Facet values to use in a search request
        """
        ...

    def result_project(self, doc: dict[str, Any], api: SearchAPI) -> str | None:
        """
        Read the project a result document belongs to

        This is the one project-specific piece of result reading that the generic
        reader ([`SearchAPIFacade.read_dataset_rows`][esmporium.search.search_api_facade.SearchAPIFacade.read_dataset_rows])
        cannot do itself: Solr carries an explicit `project` facet, whereas STAC drops it
        (project is the collection) and it must be recovered from `mip_era`. Every other
        facet is read uniformly via [get_mapping_to_api_facet_names][(c).get_mapping_to_api_facet_names].

        Parameters
        ----------
        doc
            One document from the search API's `extract_result_documents`

        api
            The search API the document came from, used to read its fields

        Returns
        -------
        :
            The project value for this document's rows, or `None` if it cannot be read
        """  # noqa: E501
        ...
