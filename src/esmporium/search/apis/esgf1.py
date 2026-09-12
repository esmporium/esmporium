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
    NoFacetValuesReturnedError,
    NoSearchResultDocumentsError,
    single_facet_value_or_none,
)
from esmporium.search.apis.request import Request
from esmporium.search.result_normalisation import SOLR_FORMAT_TAG


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
    facet_counts = raw.get("facet_counts")
    fields = (
        facet_counts.get("facet_fields") if isinstance(facet_counts, Mapping) else None
    )
    if not fields or not isinstance(fields, Mapping):
        raise NoFacetValuesReturnedError(raw, "facet_counts.facet_fields")

    res: dict[str, set[str]] = {}
    for api_name, flat in fields.items():
        if api_name in facets:
            if not isinstance(flat, list):
                raise NoFacetValuesReturnedError(
                    raw, f"facet_counts.facet_fields.{api_name}"
                )

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
    """
    Extract result documents from a raw Solr response

    Parameters
    ----------
    raw
        Raw response

    Returns
    -------
    :
        Extracted documents

    Raises
    ------
    NoSearchResultDocumentsError
        No search result documents are included in `raw`
    """
    response = raw.get("response")
    if not isinstance(response, dict) or "docs" not in response:
        raise NoSearchResultDocumentsError(raw, "response.docs")

    docs = response["docs"]
    if not isinstance(docs, list):
        raise NoSearchResultDocumentsError(raw, "response.docs")

    res: list[dict[str, Any]] = list(docs)

    return res


def solr_read_facet_list_as_strings(
    doc: dict[str, Any], api_field: str
) -> tuple[str, ...]:
    """
    Read a facet from a Solr document as a tuple of strings

    Parameters
    ----------
    doc
        Document from which to read the facet

    api_field
        Facet (i.e. field in the API response) to read

    Returns
    -------
    :
        The value of `api_field`, cast to a tuple of strings

    Raises
    ------
    NotImplementedError
        The value we found is not one we can safely cast to a tuple of strings
    """
    values = doc.get(api_field)
    if values is None:
        return ()

    if not isinstance(values, list):
        values = [values]

    return tuple(solr_facet_value_as_string(value, api_field) for value in values)


def solr_facet_value_as_string(value: Any, api_field: str) -> str:
    """
    Cast a single value read from a Solr document to a string

    Parameters
    ----------
    value
        Value to cast

    api_field
        Facet (i.e. field in the API response) `value` was read from

        Only used for error messages.

    Returns
    -------
    :
        `value`, cast to a string

    Raises
    ------
    NotImplementedError
        `value` is not one we can safely cast to a string

        Casting anything else would put a value in our database
        that nobody published,
        e.g. a `dict` would come back as `"{'a': 1}"`.
    """
    if isinstance(value, (str, int, float)):
        return str(value)

    msg = (
        f"We do not know how to read {api_field!r} out of a Solr record: "
        f"expected a string, an int, a float or nothing, got {value!r}"
    )
    raise NotImplementedError(msg)


def solr_read_facet_as_string(doc: dict[str, Any], api_field: str) -> str | None:
    """
    Read a facet from a Solr document as a single string

    Parameters
    ----------
    doc
        Document from which to read the facet

    api_field
        Facet (i.e. field in the API response) to read

    Returns
    -------
    :
        The value of `api_field`, cast to a single string

    Raises
    ------
    MultipleFacetValuesError
        In the doc, `api_field` maps to more than one value
    """
    return single_facet_value_or_none(
        solr_read_facet_list_as_strings(doc, api_field), api_field
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

    raw_docs_format_tag: str = SOLR_FORMAT_TAG
    """See [SearchAPI.raw_docs_format_tag][esmporium.search.apis.SearchAPI.raw_docs_format_tag]."""  # noqa: E501

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
