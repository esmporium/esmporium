"""
ESGF1 search API class
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tenacity import Retrying

from esmporium.search.apis.protocol import (
    LimitOutOfRangeError,
    NoFacetValuesReturned,
    NoSearchResultNumberOfMatchesReturned,
)
from esmporium.search.apis.request import Request


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
    NoSearchResultNumberOfMatchesReturned
        `raw` does not report the number of records that matched the search
    """
    num_found = raw.get("response", {}).get("numFound")
    if isinstance(num_found, int):
        return num_found

    raise NoSearchResultNumberOfMatchesReturned(raw, "response.numFound")


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
    NoFacetValuesReturned
        `raw` enumerates nothing at all
    """
    fields = raw.get("facet_counts", {}).get("facet_fields", {})
    if not fields:
        raise NoFacetValuesReturned(raw, "facet_counts.facet_fields")

    res: dict[str, set[str]] = {}
    for api_name, flat in fields.items():
        if api_name in facets:
            # Parse the API's funny list into the facet values
            res[api_name] = set(flat[0::2])

    return res


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
        See [SearchAPI.get_search_result_count][esmporium.search.apis.SearchAPI.get_search_result_count].
        """  # noqa: E501
        return get_solr_search_result_n_matches(raw)

    def build_get_facet_values_request(self, facets: set[str], project: str) -> Request:
        """
        See [SearchAPI.build_get_facet_values_request][esmporium.search.apis.SearchAPI.build_get_facet_values_request].
        """  # noqa: E501
        params: dict[str, Any] = {
            "format": "application/solr+json",
            "facets": ",".join(facets),
            # We want the vocabulary, not the records,
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
