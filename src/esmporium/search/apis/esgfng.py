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
    NoFacetValuesReturnedError,
    NoSearchResultNumberOfMatchesReturnedError,
    UncompilableFacetPatternError,
)
from esmporium.search.apis.request import Request


# In future, `aggregations` could be used to return value counts per facet value.
# Currently, only esgf-ng-east handles aggregation, not west.
# Sticking to summary for now as this is handled by east and west.
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
    NoFacetValuesReturnedError
        `raw` summarises nothing at all,
        so this deployment cannot tell us anything about any facet.
    """
    # An empty block is as useless to us as a missing one:
    # either way this deployment has told us nothing it knows.
    if not raw.get("summaries"):
        raise NoFacetValuesReturnedError(raw, "summaries")

    res: dict[str, set[str]] = {}
    for api_name, summary in raw["summaries"].items():
        # This match is deliberately exact (case-sensitive). We only ever build
        # lowercase-prefixed names (`cmip7:variable_id`), and both ESGF-NG
        # deployments key their summaries the same way today, so exact matching
        # works. Do not "fix" this by case-folding the keys: these collections
        # really do treat case as significant -- east's CMIP6Plus, for one,
        # carries both `cmip6plus:Conventions` and `cmip6plus:conventions` as
        # separate keys, so lowering every key would collapse the two and
        # silently clobber one. The cost of exactness is that a node changing a
        # key's case would drop that facet silently (it reads as "no enumerable
        # values"); `test_summary_facet_keys_have_not_drifted_in_case` is what
        # turns that drift into a loud failure.
        if api_name in facets:
            if not isinstance(summary, list):
                # A range or a pattern, i.e. not a list of values.
                # `stac_summary_patterns` is where those are read.
                continue

            values = {
                value
                for value in summary
                # The `isinstance` check ensures that we don't pick up dicts.
                # The dicts probably shouldn't be there.
                # Where they are there (e.g. CMIP6 member_id),
                # that looks like a bug in the CMIP6 CVs
                # because CMIP6 uses variant_label, not member_id.
                # Hence leave this for now,
                # but we should raise an error in CMIP6 CVs at some point
                # and we should be aware that this means
                # that member_id is silently dropped from CMIP6 pattern parsing
                # at the moment, rather than loudly dropped.
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
    NoFacetValuesReturnedError
        `raw` summarises nothing at all,
        so this deployment cannot tell us anything about any facet.

    UncompilableFacetPatternError
        A pattern specified for a given facet name
        is not able to be compiled as a regular expression.
    """
    # An empty block is as useless to us as a missing one:
    # either way this deployment has told us nothing it knows.
    if not raw.get("summaries"):
        raise NoFacetValuesReturnedError(raw, "summaries")

    res: dict[str, re.Pattern[str]] = {}
    for api_name, summary in raw["summaries"].items():
        # Exact, case-sensitive match, for the reasons in `stac_summary_values`.
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

        json_body: dict[str, Any] = {
            "filter-lang": "cql2-json",
            "limit": limit,
        }
        and_clauses: list[dict[str, Any]] = [
            {
                "op": "in",
                "args": [
                    # Assume that the caller puts the prefix on
                    {"property": facet},
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
        candidates = (
            ("numberMatched", raw.get("numberMatched")),
            ("numMatched", raw.get("numMatched")),
            (
                "context.matched",
                context.get("matched") if isinstance(context, dict) else None,
            ),
        )
        for loc, total in candidates:
            if isinstance(total, int):
                return total

            elif total is not None:
                msg = (
                    f"We expected to get an integer at {loc}, but instead got {total!r}"
                )
                raise TypeError(msg)

        raise NoSearchResultNumberOfMatchesReturnedError(
            raw, tuple(loc for loc, _ in candidates)
        )

    def build_get_facet_values_for_project_request(
        self, facets: set[str], project: str
    ) -> Request:
        """
        See [SearchAPI.build_get_facet_values_for_project_request][esmporium.search.apis.SearchAPI.build_get_facet_values_for_project_request].
        """  # noqa: E501
        # Assumes project has been mapped to the intended collection style.
        #
        # The collection name is case-sensitive on east: it must be the exact
        # ESGF spelling (e.g. `CMIP6Plus`, not `CMIP6PLUS` or `cmip6plus`) or
        # east returns 404. West is case-insensitive. The response echoes the
        # name back in its `id` field, and the two deployments disagree on its
        # case (east keeps `CMIP7`, west lowercases it to `cmip7`), but we never
        # read `id`, so that difference is nothing we have to handle.
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
