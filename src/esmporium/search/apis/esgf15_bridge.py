"""
ESGF1.5 bridge search API class
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tenacity import Retrying

from esmporium.search.apis.esgf1 import (
    DATASET_DOCUMENTS,
    solr_extract_result_documents,
    solr_facet_values,
    solr_file_search_params,
    solr_n_matches,
    solr_next_page_request,
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

    raw_docs_format_tag: str = SOLR_FORMAT_TAG
    """See [SearchAPI.raw_docs_format_tag][esmporium.search.apis.SearchAPI.raw_docs_format_tag]."""  # noqa: E501

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
            # See `SearchAPIESGF1Solr.build_search_request` for why this is stated
            # rather than left to the server default.
            "type": DATASET_DOCUMENTS,
            "limit": limit,
        }
        for api_name, values in facet_values.items():
            # This API ORs on a comma.
            params[api_name] = ",".join(values)

        return Request("GET", "/esgf-1-5-bridge/", params=params)

    def build_file_search_request(
        self,
        dataset_ids: Sequence[str],
        limit: int,
        facet_values: Mapping[str, Sequence[str]] | None = None,
    ) -> Request:
        """
        Build the `type=File` request that gets per-file information for datasets

        Solr publishes `access`, `size` and `number_of_files` on a dataset document
        but **no per-file URLs**, so getting them is a second request. This is the
        whole asymmetry with ESGF-NG, where a STAC item's `assets` are its files and
        the dataset search already carries them.

        Parameters
        ----------
        dataset_ids
            The node-specific dataset ids to ask about, i.e.
            `<instance_id>|<data_node>`

            These are exactly the `id` the dataset documents published, and exactly
            what a File document names as its `dataset_id`, so the answers join back
            onto the datasets with no lookup.

        limit
            Page size

        facet_values
            Any further facets to narrow by, keyed as this API names them

            This is how `variable` is passed, and passing it matters: a CMIP5
            `dataset_id` is *table*-grained, so without narrowing, asking about one
            CMIP5 dataset returns every variable in its table. The facet is spelled
            `variable` for CMIP5 and `variable_id` for CMIP6/CMIP7, so the caller
            must read the name from the facade rather than hard-coding either.

        Returns
        -------
        :
            The request to send
        """
        params = solr_file_search_params(limit)
        # This API ORs on a comma, and -- unlike rejecting it -- silently keeps only
        # one value of a repeated parameter. Joining the wrong way here would ask
        # about one of these datasets and look like an answer about all of them.
        params["dataset_id"] = ",".join(dataset_ids)
        for api_name, values in (facet_values or {}).items():
            params[api_name] = ",".join(values)

        return Request("GET", "/esgf-1-5-bridge/", params=params)

    def next_page_request(
        self, request: Request, raw: dict[str, Any]
    ) -> Request | None:
        """
        See [SearchAPI.next_page_request][esmporium.search.apis.SearchAPI.next_page_request].
        """  # noqa: E501
        # The offset semantics are identical to ESGF1 Solr; the differing path and
        # comma-OR facets are already carried on `request`, so the shared helper does.
        return solr_next_page_request(request, raw)

    def get_n_matches(self, raw: dict[str, Any]) -> int:
        """
        See [SearchAPI.get_n_matches][esmporium.search.apis.SearchAPI.get_n_matches].
        """
        return solr_n_matches(raw)

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
