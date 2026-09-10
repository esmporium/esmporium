"""
ESGF1.5 bridge search API class
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tenacity import Retrying

from esmporium.search.apis.esgf1 import (
    solr_extract_result_documents,
    solr_facet_values,
    solr_read_facet_as_string,
    solr_read_facet_list_as_strings,
)
from esmporium.search.apis.protocol import (
    LimitOutOfRangeError,
)
from esmporium.search.apis.request import Request
from esmporium.search.result_normalisation import SOLR_FORMAT_TAG


@dataclass(frozen=True)
class SearchAPIESGF15BridgeSolr:
    """
    ESGF1.5 bridge search API that uses the SOLR format

    Instances of this class should mirror the (relevant) behaviour
    of the ESGF1.5 bridge search APIs.
    """

    host: str
    """See [SearchAPI.host][esmporium.search.apis.SearchAPI.host]."""

    retrying: Retrying
    """See [SearchAPI.retrying][esmporium.search.apis.SearchAPI.retrying]."""

    search_api_tag: str = SOLR_FORMAT_TAG
    """See [SearchAPI.search_api_tag][esmporium.search.apis.SearchAPI.search_api_tag]."""  # noqa: E501

    timeout: float = 30.0
    """See [SearchAPI.timeout][esmporium.search.apis.SearchAPI.timeout]."""

    scheme: str = "https"
    """See [SearchAPI.scheme][esmporium.search.apis.SearchAPI.scheme]."""

    min_limit: int = 0
    """
    Minimum value of limit accepted by this API
    """

    max_limit: int = 10_000
    """
    Maximum value of limit accepted by this API
    """

    def build_search_request(
        self,
        facet_values: Mapping[str, tuple[str, ...]],
        limit: int,
    ) -> Request:
        """
        See [SearchAPI.build_search_request][esmporium.search.apis.SearchAPI.build_search_request].
        """  # noqa: E501
        if limit < self.min_limit or limit > self.max_limit:
            raise LimitOutOfRangeError(
                limit, min_limit=self.min_limit, max_limit=self.max_limit
            )

        params: dict[str, Any] = {
            "format": "application/solr+json",
            "limit": limit,
        }
        for api_name, values in facet_values.items():
            # This API ORs on a comma.
            params[api_name] = ",".join(values)

        return Request("GET", "/esgf-1-5-bridge/", params=params)

    def build_get_facet_values_for_project_request(
        self, facets: set[str], project: str
    ) -> Request:
        """
        See [SearchAPI.build_get_facet_values_for_project_request][esmporium.search.apis.SearchAPI.build_get_facet_values_for_project_request].
        """  # noqa: E501
        params: dict[str, Any] = {
            "format": "application/solr+json",
            # Sorted so the request we build is deterministic.
            "facets": ",".join(sorted(facets)),
            # We want the facet values, not the records,
            # so we ask for the smallest page we are allowed to ask for.
            "limit": self.min_limit,
            "project": project,
        }

        return Request("GET", "/esgf-1-5-bridge/", params=params)

    def parse_facet_values(
        self, raw: dict[str, Any], facets: set[str]
    ) -> dict[str, set[str]]:
        """
        See [SearchAPI.parse_facet_values][esmporium.search.apis.SearchAPI.parse_facet_values].
        """  # noqa: E501
        return solr_facet_values(raw, facets)

    def parse_facet_patterns(
        self, raw: dict[str, Any], facets: set[str]
    ) -> dict[str, re.Pattern[str]]:
        """
        See [SearchAPI.parse_facet_patterns][esmporium.search.apis.SearchAPI.parse_facet_patterns].
        """  # noqa: E501
        # ESGF1.5 (like ESGF1) always enumerates its facet values;
        # it never describes their form.
        return {}

    def extract_result_documents(self, raw: dict[str, Any]) -> list[dict[str, Any]]:
        """
        See [SearchAPI.extract_result_documents][esmporium.search.apis.SearchAPI.extract_result_documents].
        """  # noqa: E501
        return solr_extract_result_documents(raw)

    def read_facet(self, doc: dict[str, Any], api_field: str) -> str | None:
        """
        See [SearchAPI.read_facet][esmporium.search.apis.SearchAPI.read_facet].
        """
        return solr_read_facet_as_string(doc, api_field)

    def read_facet_list(self, doc: dict[str, Any], api_field: str) -> tuple[str, ...]:
        """
        See [SearchAPI.read_facet_list][esmporium.search.apis.SearchAPI.read_facet_list].
        """  # noqa: E501
        return solr_read_facet_list_as_strings(doc, api_field)
