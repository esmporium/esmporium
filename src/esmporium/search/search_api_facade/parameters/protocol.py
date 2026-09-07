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

    def read_result_facets(
        self, doc: dict[str, Any], api: SearchAPI
    ) -> tuple[dict[str, str | None], ...]:
        """
        Read the `Dataset` facet rows one search document maps to

        This is the project half of result parsing: it turns one raw document into the
        facet rows it covers, named as our columns. A CMIP5 document yields one row per
        variable in its bundle; every other project yields exactly one row.

        Parameters
        ----------
        doc
            One document from the search API's `extract_result_documents`

        api
            The search API the document came from, used to read its fields

        Returns
        -------
        :
            One full facet dict per dataset row (without `id_project_specific`, which is
            shared and added later)
        """
        ...
