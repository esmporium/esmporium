"""
Definition of interface for facade parameters

These are different from queries,
because the translation to API facade parameters
is better expressed as more than just a pure mapping.
"""

from __future__ import annotations

from typing import Protocol

from esmporium.query.protocol import QueryProtocol


class FacadeParametersProtocol(Protocol):
    """
    A facade parameter definition we support
    """

    base_query_style: type[QueryProtocol]
    """
    Base query style

    Mapping to facets as used by the actual API
    can involve an extra layer on top of this base style
    (e.g. ESGF-NG's prefix).
    """

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
