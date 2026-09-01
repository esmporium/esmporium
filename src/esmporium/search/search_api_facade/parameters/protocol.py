"""
Definition of interface for facade parameters

These are different from queries,
because the translation to API facade parameters
is better expressed as more than just a pure mapping.
"""

from __future__ import annotations

from typing import Annotated, Protocol

from pydantic import PlainValidator

from esmporium.query import QueryCanonical, QueryProtocol
from esmporium.query.protocol import accept_without_validation


class FacadeParametersProtocol(Protocol):
    """
    A facade parameter definition we support
    """

    base_query_style: Annotated[
        type[QueryProtocol], PlainValidator(accept_without_validation)
    ]
    """
    Base query style

    Mapping to facets as used by the actual API
    can involve an extra layer on top of this base style
    (e.g. ESGF-NG's prefix).
    """

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

    def get_mapping_to_api_facet_names(self, facets: set[str]) -> dict[str, str]:
        """
        Get the mapping from input names to names used by the API

        Like everywhere else, mapping is only supported from canonical names
        or if the facet name is already a project-specific name.

        Parameters
        ----------
        facets
            Facets for which to get the mapping

        Returns
        -------
        :
            Mapping from values in `facets` to the name that the API uses.
        """
        ...
