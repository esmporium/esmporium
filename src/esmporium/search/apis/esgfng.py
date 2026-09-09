"""
ESGF-NG search API class
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
    UncompilableFacetPatternError,
    single_facet_value_or_none,
)
from esmporium.search.apis.request import Request
from esmporium.search.result_normalisation import STAC_FORMAT_TAG
from esmporium.search.result_parsing import DataNodeInfo, ParsedDocShell


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


def stac_extract_result_documents(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the per-dataset features in a STAC-shaped search response."""
    # Let's raise if there is no "features" key
    # rather than silently returning no results.
    # We expect there to be a features key.
    # If there isn't, something has gone really wrong.
    features: list[dict[str, Any]] = raw.get("features", [])
    return list(features)


def stac_read_facet_list(feature: dict[str, Any], api_field: str) -> tuple[str, ...]:
    """Read a facet from a STAC feature's `properties` as a tuple of its values.

    A STAC feature is expected to carry one value per facet, but a `properties` entry
    that is itself a list is read as the values it holds (so
    [stac_read_facet][(m).] can flag it), rather than being stringified whole.
    """
    # As above, let's raise if there is no "properties" key
    # rather than silently returning nothing.
    # We expect there to be a properties key.
    # If there isn't, something has gone really wrong.
    value = feature.get("properties", {}).get(api_field)
    if value is None:
        return ()
    if not isinstance(value, list):
        value = [value]
    return tuple(str(item) for item in value)


def stac_read_facet(feature: dict[str, Any], api_field: str) -> str | None:
    """Read one scalar facet from a STAC feature's `properties` by API field name.

    Built on [stac_read_facet_list][(m).] so a field that unexpectedly carries several
    values raises [MultipleFacetValuesError][esmporium.search.apis.MultipleFacetValuesError]
    rather than being stringified into a single value.
    """  # noqa: E501
    return single_facet_value_or_none(
        stac_read_facet_list(feature, api_field), api_field
    )


def _is_version_token(inv: str) -> bool:
    return inv.startswith("v") and inv[1:].isdigit()


def _strip_version(native_id: str) -> str:
    """Drop a trailing `.vYYYYMMDD` token so different editions share a bundle id."""
    parts = native_id.split(".")
    # A version token is the last `.`-segment written as `v` followed by digits, e.g.
    # `.v20200623`. `isdigit()` on the tail after the `v` requires at least one digit,
    # so a bare `v` is not mistaken for a version.
    if parts and _is_version_token(parts[-1]):
        return ".".join(parts[:-1])
    return native_id


def stac_nodes(feature: dict[str, Any]) -> tuple[DataNodeInfo, ...]:
    """Return the distinct data nodes a STAC feature's assets are hosted on."""
    hosts: list[str] = []
    for asset in feature.get("assets", {}).values():
        # `alternate:name` (STAC alternate-assets extension) is the canonical data-node
        # identity, matching Solr's `data_node` (e.g. `ceda.ac.uk`). Deliberately do NOT
        # read `href` here: `href` is the file-download URL (e.g. `dap.ceda.ac.uk`), a
        # different concept from the data node, reserved for a future file-access step.
        host = asset.get("alternate:name")
        if host and host not in hosts:
            hosts.append(host)
    return tuple(DataNodeInfo(host) for host in hosts)


def stac_read_document_shell(feature: dict[str, Any]) -> ParsedDocShell:
    """Read the format-determined pieces of a STAC feature."""
    props: dict[str, Any] = feature["properties"]
    feature_id = feature["id"]
    return ParsedDocShell(
        # Funny: STAC fixed their IDs so we can't use them
        # without applying strip_version here.
        # The shape of this ID could be different in a different project
        # i.e. there are still hidden assumptions about the project in here.
        #
        # Let's fix this by changing doc_parsing to response_parsing,
        # then we can use the response parsers to handle all of this stuff
        # in a way that allows us to make the coupling between project and API clear.
        # Let's move any response handling
        # which is project specific to response_parsing,
        # leaving only things
        # which are purely defined by the response format on the search API classes.
        #
        # (The paragraph above should be ok as a start for a claude prompt,
        # we can also this together tomorrow morning
        # if you want to avoid going round in more circles).
        id_project_specific=_strip_version(feature_id),
        version=props["version"],  # if there isn't a version, fail loudly
        is_latest=props["latest"],  # if there isn't latest, fail loudly
        retracted=props["retracted"],  # if there isn't retracted, fail loudly
        nodes=stac_nodes(feature),
        esgf_doc_id=feature_id,
        raw_json=json.dumps(feature),
    )


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

    search_api_tag: str = STAC_FORMAT_TAG
    """See [SearchAPI.search_api_tag][esmporium.search.apis.SearchAPI.search_api_tag]."""  # noqa: E501

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
        # Given the different APIs disagree,
        # this is the kind of thing
        # I would consider pushing onto the new response parser
        # (to make clear that it's coupled to the endpoint,
        # not just a pure formatting thing).
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

    def extract_result_documents(self, raw: dict[str, Any]) -> list[dict[str, Any]]:
        """
        See [SearchAPI.extract_result_documents][esmporium.search.apis.SearchAPI.extract_result_documents].
        """  # noqa: E501
        # Can stay here: this just assumes there's a 'features' key,
        # which I think is how the STAC spec works.
        # We might want to inline `stac_extract_result_documents` given how small it is.
        return stac_extract_result_documents(raw)

    def read_document_shell(self, doc: dict[str, Any]) -> ParsedDocShell:
        """
        See [SearchAPI.read_document_shell][esmporium.search.apis.SearchAPI.read_document_shell].
        """  # noqa: E501
        # This gets pushed onto result parser
        # as the details of how this works can vary by project.
        return stac_read_document_shell(doc)

    def read_facet(self, doc: dict[str, Any], api_field: str) -> str | None:
        """
        See [SearchAPI.read_facet][esmporium.search.apis.SearchAPI.read_facet].
        """
        # This can stay here:
        # I believe the way this is setup just follows the STAC spec.
        # We can inline stac_read_facet given how tiny it is.
        return stac_read_facet(doc, api_field)

    def read_facet_list(self, doc: dict[str, Any], api_field: str) -> tuple[str, ...]:
        """
        See [SearchAPI.read_facet_list][esmporium.search.apis.SearchAPI.read_facet_list].
        """  # noqa: E501
        # This can stay here:
        # I believe the way this is setup just follows the STAC spec.
        # We can inline stac_read_facet_list given how tiny it is.
        return stac_read_facet_list(doc, api_field)
