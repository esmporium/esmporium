"""
ESGF1 search API class
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tenacity import Retrying

from esmporium.search.apis.protocol import (
    LimitOutOfRangeError,
    NoFacetValuesReturnedError,
    NoSearchResultNumberOfMatchesReturnedError,
)
from esmporium.search.apis.request import Request
from esmporium.search.result_normalisation import SOLR_FORMAT_TAG
from esmporium.search.result_parsing import DataNodeInfo, ParsedDocShell


def solr_bool(value: bool) -> str:
    """
    Write a boolean the way the Solr-shaped APIs expect to read it

    Parameters
    ----------
    value
        The value to write

    Returns
    -------
    :
        `value`, as these APIs spell it

    Examples
    --------
    >>> solr_bool(True)
    'true'
    >>> solr_bool(False)
    'false'
    """
    return "true" if value else "false"


def get_solr_search_result_n_matches(raw: dict[str, Any]) -> int:
    """
    Get the number of records that matched a search from a Solr-shaped response

    Note: this is not the same as the number of results in `raw`.
    Solr has the idea of 'limit', which means that the number of results returned
    can differ from the total number of records which matched a given query.

    Parameters
    ----------
    raw
        The raw search result to read

    Returns
    -------
    :
        The number of records that matched the search

    Raises
    ------
    NoSearchResultNumberOfMatchesReturnedError
        `raw` does not report the number of records that matched the search
    """
    num_found = raw.get("response", {}).get("numFound")
    if isinstance(num_found, int):
        return num_found
    elif num_found is not None:
        msg = (
            "We expected to get an integer at 'response.numFound', "
            f"but instead got {num_found!r}"
        )
        raise TypeError(msg)

    raise NoSearchResultNumberOfMatchesReturnedError(raw, "response.numFound")


def solr_facet_values(raw: dict[str, Any], facets: set[str]) -> dict[str, set[str]]:
    """
    Read the available facet values out of a Solr-shaped facet values response

    We keep only the facets which were asked about.

    Parameters
    ----------
    raw
        The response to read.

    facets
        The facets we asked about.

    Returns
    -------
    :
        The values which are available, keyed by the facet name.

    Raises
    ------
    NoFacetValuesReturnedError
        `raw` enumerates nothing at all
    """
    fields = raw.get("facet_counts", {}).get("facet_fields", {})
    if not fields:
        raise NoFacetValuesReturnedError(raw, "facet_counts.facet_fields")

    res: dict[str, set[str]] = {}
    for api_name, flat in fields.items():
        if api_name in facets:
            # Parse the API's funny list into the facet values
            res[api_name] = set(flat[0::2])

    return res


def extract_one_element_list(value: Any) -> Any:
    """
    Unwrap a one-element list, e.g. `['CMIP5']` -> `'CMIP5'`, else leave as-is

    Solr returns most facets as single-element lists. This collapses them so a facet
    reads back as the scalar it represents.
    """
    if isinstance(value, list) and len(value) == 1:
        return value[0]
    return value


def solr_extract_result_documents(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the per-dataset records in a Solr-shaped search response."""
    docs: list[dict[str, Any]] = raw.get("response", {}).get("docs", [])
    return list(docs)


def solr_read_facet(doc: dict[str, Any], api_field: str) -> str | None:
    """Read one scalar facet out of a Solr record by its API field name."""
    value = extract_one_element_list(doc.get(api_field))
    return None if value is None else str(value)


def solr_read_facet_list(doc: dict[str, Any], api_field: str) -> tuple[str, ...]:
    """Read a multi-valued facet (e.g. CMIP5's whole `variable` bundle) as a tuple."""
    values = doc.get(api_field)
    if values is None:
        return ()
    if not isinstance(values, list):
        values = [values]
    return tuple(str(value) for value in values)


def solr_read_document_shell(doc: dict[str, Any]) -> ParsedDocShell:
    """Read the format-determined pieces of a Solr record."""
    return ParsedDocShell(
        id_project_specific=extract_one_element_list(doc["master_id"]),
        version=str(extract_one_element_list(doc["version"])),
        is_latest=bool(extract_one_element_list(doc.get("latest", False))),
        retracted=bool(extract_one_element_list(doc.get("retracted", False))),
        nodes=(DataNodeInfo(data_node=extract_one_element_list(doc["data_node"])),),
        esgf_doc_id=extract_one_element_list(doc["id"]),
        raw_json=json.dumps(doc),
    )


@dataclass(frozen=True)
class SearchAPIESGF1Solr:
    """
    ESGF1 search API that uses the SOLR format

    Instances of this class should mirror the (relevant) behaviour
    of the ESGF1 search APIs.
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

    distrib: bool = True
    """
    Whether to perform a distributed search or not

    With this on, the search API answers for every node it knows about,
    which is what makes "search ESGF" a single request rather than one per node.
    Turning this off asks the node only about the data it holds itself,
    which is what you want when you are asking about a specific node.
    """

    min_limit: int = 0
    """
    Minimum value of limit accepted by this API
    """

    max_limit: int = 10_000
    """
    Maximum value of limit accepted by this API
    """

    def build_search_request(
        self, facet_values: Mapping[str, tuple[str, ...]], limit: int
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
            "distrib": solr_bool(self.distrib),
        }
        for api_name, values in facet_values.items():
            # A list becomes a repeated parameter,
            # which is how ESGF1 APIs perform OR queries
            # across multiple values for a given facet.
            params[api_name] = list(values)

        return Request("GET", "/esg-search/search", params=params)

    def get_search_result_n_matches(self, raw: dict[str, Any]) -> int:
        """
        See [SearchAPI.get_search_result_n_matches][esmporium.search.apis.SearchAPI.get_search_result_n_matches].
        """  # noqa: E501
        return get_solr_search_result_n_matches(raw)

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
            "distrib": solr_bool(self.distrib),
            "project": project,
        }

        return Request("GET", "/esg-search/search", params=params)

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
        # ESGF1 always enumerates its facet values; it never describes their form.
        return {}

    def extract_result_documents(self, raw: dict[str, Any]) -> list[dict[str, Any]]:
        """
        See [SearchAPI.extract_result_documents][esmporium.search.apis.SearchAPI.extract_result_documents].
        """  # noqa: E501
        return solr_extract_result_documents(raw)

    def read_document_shell(self, doc: dict[str, Any]) -> ParsedDocShell:
        """
        See [SearchAPI.read_document_shell][esmporium.search.apis.SearchAPI.read_document_shell].
        """  # noqa: E501
        return solr_read_document_shell(doc)

    def read_facet(self, doc: dict[str, Any], api_field: str) -> str | None:
        """
        See [SearchAPI.read_facet][esmporium.search.apis.SearchAPI.read_facet].
        """
        # This doesn't match its type hint.
        # We need to be careful here.
        # I would fail loudly if a user uses `read_facet`
        # but we find more than one value
        # (at the moment I don't think this would happen).
        # Or we just get rid of read_facet
        # and only have `read_facet_list` and push all length checking onto callers.
        return solr_read_facet(doc, api_field)

    def read_facet_list(self, doc: dict[str, Any], api_field: str) -> tuple[str, ...]:
        """
        See [SearchAPI.read_facet_list][esmporium.search.apis.SearchAPI.read_facet_list].
        """  # noqa: E501
        return solr_read_facet_list(doc, api_field)
