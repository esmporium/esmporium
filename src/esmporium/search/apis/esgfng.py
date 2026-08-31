"""
ESGF-NG search API class
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
    UncompilableFacetPatternError,
)
from esmporium.search.apis.request import Request


def stac_summary_values(raw: dict[str, Any], facets: set[str]) -> dict[str, set[str]]:
    """
    Read the available facet values out of a STAC-shaped facet values response

    Not every summary enumerates values.
    STAC also allows a summary to be a regular expression.
    Those facets are left out of the result:
    "we cannot list this one" and "this one has no values"
    have to stay distinguishable.

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
        `raw` summarises nothing at all,
        so this deployment cannot tell us anything about any facet.
    """
    # An empty block is as useless to us as a missing one:
    # either way this deployment has told us nothing it knows.
    if not raw.get("summaries"):
        raise NoFacetValuesReturned(raw, "summaries")

    res: dict[str, set[str]] = {}
    for api_name, summary in raw["summaries"].items():
        if api_name in facets:
            if not isinstance(summary, list):
                # A range or a pattern, i.e. not a list of values.
                # `stac_summary_patterns` is where those are read.
                continue

            values = {
                value
                for value in summary
                # TODO: Why is this isinstance check needed?
                if isinstance(value, str)
            }
            if values:
                res[api_name] = values

    return res


def stac_summary_patterns(
    raw: dict[str, Any], facets: set[str]
) -> dict[str, re.Pattern[str]]:
    """
    Read the supported facet patterns out of a STAC-shaped facet values response

    Not every summary provides supported patterns.
    STAC also allows a summary to enumerate supported values.
    Those facets are left out of the result:
    "we cannot list this one" and "this one has no pattern"
    have to stay distinguishable.

    Parameters
    ----------
    raw
        The response to read.

    facets
        The facets we asked about.

    Returns
    -------
    :
        The pattern which is supported, keyed by the facet name.

    Raises
    ------
    NoFacetValuesReturned
        `raw` summarises nothing at all,
        so this deployment cannot tell us anything about any facet.

    UncompilableFacetPatternError
        A pattern specified for a given facet name
        is not able to be compiled as a regular expression.
    """
    # An empty block is as useless to us as a missing one:
    # either way this deployment has told us nothing it knows.
    if not raw.get("summaries"):
        raise NoFacetValuesReturned(raw, "summaries")

    res: dict[str, re.Pattern[str]] = {}
    for api_name, summary in raw["summaries"].items():
        if api_name in facets:
            if not isinstance(summary, str):
                # Enumerated values or something else.
                # `stac_summary_values` is where those are read.
                continue

            try:
                res[api_name] = re.compile(summary)
            except re.error as exc:
                raise UncompilableFacetPatternError(api_name, summary) from exc

    return res


@dataclass(frozen=True)
class SearchAPIESGFNGSTAC:
    """
    ESGF-NG search API that is based on the STAC standard

    Instances of this class should mirror the (relevant) behaviour
    of the ESGF-NG (search) APIs.
    """

    host: str
    """See [SearchAPI.host][esmporium.search.apis.SearchAPI.host]."""

    retrying: Retrying
    """See [SearchAPI.retrying][esmporium.search.apis.SearchAPI.retrying]."""

    timeout: float = 30.0
    """See [SearchAPI.timeout][esmporium.search.apis.SearchAPI.timeout]."""

    scheme: str = "https"
    """See [SearchAPI.scheme][esmporium.search.apis.SearchAPI.scheme]."""

    min_limit: int = 1
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

        json_body = {
            "filter-lang": "cql2-json",
            "limit": limit,
        }
        and_clauses: list[dict[str, Any]] = [
            {
                "op": "in",
                "args": [
                    # Assume that the caller puts the prefix on
                    {"property": f"{facet}"},
                    list(values),
                ],
            }
            for facet, values in facet_values.items()
        ]
        if and_clauses:
            json_body["filter"] = {"op": "and", "args": and_clauses}

        return Request("POST", "/search", json_body=json_body)

    def get_search_result_n_matches(self, raw: dict[str, Any]) -> int:
        """
        See [SearchAPI.get_search_result_n_matches][esmporium.search.apis.SearchAPI.get_search_result_n_matches].
        """  # noqa: E501
        # The two ESGF-NG deployments disagree on where the total lives:
        # east reports `numberMatched` (the STAC spelling),
        # west reports `numMatched` and `context.matched`.
        # We try everything, starting with the correct (STAC) spelling.
        context = raw.get("context")
        for total in (
            raw.get("numberMatched"),
            raw.get("numMatched"),
            context.get("matched") if isinstance(context, dict) else None,
        ):
            if isinstance(total, int):
                return total

        raise NoSearchResultNumberOfMatchesReturned(
            raw, "numberMatched / numMatched / context.matched"
        )

    def build_get_facet_values_for_project_request(
        self, facets: set[str], project: str
    ) -> Request:
        """
        See [SearchAPI.build_get_facet_values_for_project_request][esmporium.search.apis.SearchAPI.build_get_facet_values_for_project_request].
        """  # noqa: E501
        # Assumes project has been mapped to the intended collection style
        # (the facade passes the collection exactly as the caller wrote it,
        # e.g. `CMIP6`).
        return Request("GET", f"/collections/{project}")

    def parse_facet_values(
        self, raw: dict[str, Any], facets: set[str]
    ) -> dict[str, set[str]]:
        """
        See [SearchAPI.parse_facet_values][esmporium.search.apis.SearchAPI.parse_facet_values].
        """  # noqa: E501
        return stac_summary_values(raw, facets)

    def parse_facet_patterns(
        self, raw: dict[str, Any], facets: set[str]
    ) -> dict[str, re.Pattern[str]]:
        """
        See [SearchAPI.parse_facet_patterns][esmporium.search.apis.SearchAPI.parse_facet_patterns].
        """  # noqa: E501
        return stac_summary_patterns(raw, facets)
