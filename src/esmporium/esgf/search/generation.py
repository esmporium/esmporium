"""
Search-API generations and the per-generation data for building their requests.

A [`SearchAPIGeneration`][esmporium.esgf.search.generation.SearchAPIGeneration] is
one of the three ESGF search-API 'profiles' our search layer can build requests
for: the legacy **ESGF1** (Solr / esg-search) API, and the two **ESGF-NG** (STAC /
CQL2) deployments, **east** and **west**.

This module holds only *data*: the enum, and a
[`GenerationConfig`][esmporium.esgf.search.generation.GenerationConfig] per
generation carrying everything the request builder (see
[`request`][esmporium.esgf.search]) needs — the search path, whether the API
speaks CQL2, the canonical-facet -> API-parameter *name* dictionaries, and (for
STAC) the project -> collection-id map. There is no request-building logic here.

The name dictionaries are the Generation's *own*, deliberately not reused from the
facet-query project profiles: the API parameter names are the search API's to
define, not the project's, and they do differ. The clearest example is CMIP7's
`processing_id`, which the STAC API exposes as `cmip7:variable_branding_suffix`
(not the `branding_suffix` a project-native mapping would assume) — verified live.
"""

from enum import Enum

from pydantic import BaseModel


class SearchAPIGeneration(str, Enum):
    """
    Which ESGF search-API generation ('profile') a node speaks.

    Not to be confused with the index node (host) itself: many hosts serve the
    same generation. The generation selects how a query is turned into a request.
    """

    ESGF1 = "esgf1"
    """Legacy ESGF1 Solr / esg-search API (comma-joined GET params)."""
    ESGF_NG_EAST = "esgf_ng_east"
    """ESGF-NG STAC / CQL2 API, east deployment."""
    ESGF_NG_WEST = "esgf_ng_west"
    """ESGF-NG STAC / CQL2 API, west deployment."""


# Canonical facet -> ESGF1 Solr parameter name, per project. The Solr parameter
# names are the project-native facet names. CMIP5 has no activity/resolution/grid.
# Documented convention (the esg-search endpoints are currently 501, so these are
# not live-verified, unlike the STAC names below).
_SOLR_FACET_NAMES: dict[str, dict[str, str]] = {
    "CMIP5": {
        "model": "model",
        "institution": "institute",
        "experiment": "experiment",
        "variable": "variable",
        "variant_label": "ensemble",
        "reporting_interval": "time_frequency",
        "processing_id": "cmor_table",
        "realm": "realm",
    },
    "CMIP6": {
        "model": "source_id",
        "institution": "institution_id",
        "experiment": "experiment_id",
        "variable": "variable_id",
        "variant_label": "variant_label",
        "reporting_interval": "frequency",
        "processing_id": "table_id",
        "activity": "activity_id",
        "resolution": "nominal_resolution",
        "grid_label": "grid_label",
        "realm": "realm",
    },
}

# Canonical facet -> ESGF-NG STAC property name, per project. Verified live on both
# east and west (2026-08-14): properties are `cmip6:`/`cmip7:`-prefixed, and the
# prefixed spelling is the one that filters on *both* deployments. Note the CMIP7
# `processing_id` -> `cmip7:variable_branding_suffix` special case.
_STAC_FACET_NAMES: dict[str, dict[str, str]] = {
    "CMIP6": {
        "model": "cmip6:source_id",
        "institution": "cmip6:institution_id",
        "experiment": "cmip6:experiment_id",
        "variable": "cmip6:variable_id",
        "variant_label": "cmip6:variant_label",
        "reporting_interval": "cmip6:frequency",
        "processing_id": "cmip6:table_id",
        "activity": "cmip6:activity_id",
        "resolution": "cmip6:nominal_resolution",
        "grid_label": "cmip6:grid_label",
        "realm": "cmip6:realm",
    },
    "CMIP7": {
        "model": "cmip7:source_id",
        "institution": "cmip7:institution_id",
        "experiment": "cmip7:experiment_id",
        "variable": "cmip7:variable_id",
        "variant_label": "cmip7:variant_label",
        "reporting_interval": "cmip7:frequency",
        "processing_id": "cmip7:variable_branding_suffix",
        "activity": "cmip7:activity_id",
        "resolution": "cmip7:nominal_resolution",
        "grid_label": "cmip7:grid_label",
        "realm": "cmip7:realm",
    },
}

# Project -> STAC collection id for the search filter. Verified live: the ids are
# UPPERCASE (lowercase returns 0 matches). Projects absent here fall back to
# `project.upper()` at request time and simply return empty (unknown collection).
_STAC_COLLECTION_IDS: dict[str, str] = {
    "CMIP6": "CMIP6",
    "CMIP7": "CMIP7",
}


class GenerationConfig(BaseModel):
    """
    The data one search-API generation needs to have its requests built.

    Immutable data only; the request builder consumes an instance of this rather
    than branching on the generation itself.
    """

    model_config = {"frozen": True}

    generation: SearchAPIGeneration
    """The generation this config describes."""

    search_path: str
    """Path appended to ``https://{host}`` to reach the search endpoint."""

    builds_cql2: bool
    """Whether this API takes a STAC CQL2 body (True) or Solr GET params (False)."""

    facet_names: dict[str, dict[str, str]]
    """Project -> {canonical facet -> this API's parameter/property name}."""

    collection_ids: dict[str, str] = {}
    """Project -> STAC collection id (STAC only; empty for Solr)."""

    def search_url(self, host: str) -> str:
        """
        Build the full search URL for a host serving this generation.

        Parameters
        ----------
        host
            The node host, without scheme or path (e.g. ``discovery.west.esgf.io``).

        Returns
        -------
        :
            The absolute search URL, e.g. ``https://discovery.west.esgf.io/search``.
        """
        return f"https://{host}{self.search_path}"


GENERATION_CONFIGS: dict[SearchAPIGeneration, GenerationConfig] = {
    SearchAPIGeneration.ESGF1: GenerationConfig(
        generation=SearchAPIGeneration.ESGF1,
        search_path="/esg-search/search",
        builds_cql2=False,
        facet_names=_SOLR_FACET_NAMES,
    ),
    SearchAPIGeneration.ESGF_NG_EAST: GenerationConfig(
        generation=SearchAPIGeneration.ESGF_NG_EAST,
        search_path="/search",
        builds_cql2=True,
        facet_names=_STAC_FACET_NAMES,
        collection_ids=_STAC_COLLECTION_IDS,
    ),
    SearchAPIGeneration.ESGF_NG_WEST: GenerationConfig(
        generation=SearchAPIGeneration.ESGF_NG_WEST,
        search_path="/search",
        builds_cql2=True,
        facet_names=_STAC_FACET_NAMES,
        collection_ids=_STAC_COLLECTION_IDS,
    ),
}
"""
The per-generation config registry.

East and west share the same STAC data (names, collection ids); they differ only
by host, which is carried by the [`IndexNode`][esmporium.esgf.search.hosts.IndexNode],
not here.
"""


def get_generation_config(generation: SearchAPIGeneration) -> GenerationConfig:
    """
    Look up the config for a search-API generation.

    Parameters
    ----------
    generation
        The generation to look up.

    Returns
    -------
    :
        Its [`GenerationConfig`][esmporium.esgf.search.generation.GenerationConfig].
    """
    return GENERATION_CONFIGS[generation]
