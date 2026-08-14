"""
Turn a canonical query into a concrete request for one search-API generation.

[`build_request`][esmporium.esgf.search.request.build_request] takes a
[`CanonicalQuery`][esmporium.esgf.canonical.CanonicalQuery], the project being
searched, and that node's
[`GenerationConfig`][esmporium.esgf.search.generation.GenerationConfig], and returns
a [`SearchRequest`][esmporium.esgf.search.request.SearchRequest] — either a Solr GET
(ESGF1) or a STAC CQL2 POST (ESGF-NG).

The Generation owns the whole translation: it maps canonical facet names to the
API's parameter/property names (from its config) and encodes the values in the
API's own way. Two behaviours differ between the generations and are the crux of
this module:

- **value encoding.** Values within a facet are OR-ed: Solr comma-joins them
  (``variable_id=tas,pr``); STAC uses a CQL2 ``in`` list. Different facets are
  AND-ed: Solr by separate params, STAC by a CQL2 ``and``.
- **retracted.** We always want retracted datasets included (to detect later when a
  dataset becomes retracted). ESGF-NG/STAC includes them *by default*, so we add no
  retracted filter — verified live, and adding one actually breaks west. ESGF1/Solr
  excludes them by default, so we send an explicit include control (unverified: the
  esg-search endpoints are 501 across the federation).
"""

from typing import Any

from pydantic import BaseModel

from esmporium.esgf.canonical import CANONICAL_FACETS, CanonicalQuery
from esmporium.esgf.search.generation import GenerationConfig

# One page of results is enough for this step: the raw JSON is a throwaway
# intermediate and full pagination is a later concern. STAC rejects limit=0, so
# this must stay >= 1.
DEFAULT_LIMIT = 100

# The Solr param that asks esg-search to include retracted datasets. UNVERIFIED:
# the esg-search endpoints return 501 federation-wide, so this documented guess
# cannot be checked live yet (tracked as an open item). It exists so that a
# retracted control is present on every ESGF1 request, per the design.
_SOLR_RETRACTED_INCLUDE = ("retracted", "*")


class UnrepresentableFacetError(ValueError):
    """
    A queried canonical facet has no parameter name for this project/generation.

    For example, ``grid_label`` queried against CMIP5, which has no grid concept.
    The facet genuinely cannot be expressed, so the (project) match is empty; the
    search orchestrator catches this and records no results for that project rather
    than dropping the facet and returning a wrong superset.
    """

    def __init__(self, facet: str, project: str, generation: str) -> None:
        self.facet = facet
        self.project = project
        self.generation = generation
        super().__init__(
            f"facet {facet!r} has no {generation} parameter name for project {project}"
        )


class SearchRequest(BaseModel):
    """
    A ready-to-send request for one (node, project): how and what to send.

    Solr requests carry ``params`` (a GET query string); STAC requests carry
    ``json_body`` (a POST body). The client pairs this with the node's URL.
    """

    model_config = {"frozen": True}

    method: str
    """HTTP method, ``"GET"`` (Solr) or ``"POST"`` (STAC)."""

    params: dict[str, str] = {}
    """Query-string params for a Solr GET; empty for STAC."""

    json_body: dict[str, Any] | None = None
    """JSON POST body for a STAC search; ``None`` for Solr."""


def _set_canonical_facets(
    canonical: CanonicalQuery,
) -> list[tuple[str, tuple[str, ...]]]:
    """Return ``(canonical_facet, values)`` for each canonical facet that is set.

    Sorted by facet name so the generated request is deterministic.
    """
    pairs: list[tuple[str, tuple[str, ...]]] = []
    for facet in sorted(CANONICAL_FACETS):
        values: tuple[str, ...] = getattr(canonical, facet)
        if values:
            pairs.append((facet, values))
    return pairs


def build_request(
    canonical: CanonicalQuery, project: str, config: GenerationConfig
) -> SearchRequest:
    """
    Build the request to search one project on a node of a given generation.

    Parameters
    ----------
    canonical
        The query, already lowered to the canonical vocabulary.

    project
        The project to search (e.g. ``"CMIP6"``); selects the name map and, for
        STAC, the collection.

    config
        The target node's generation config (name maps, encoding, collection ids).

    Returns
    -------
    :
        A [`SearchRequest`][esmporium.esgf.search.request.SearchRequest] to send.

    Raises
    ------
    UnrepresentableFacetError
        If a set canonical facet has no parameter name for this project/generation.
    """
    if config.builds_cql2:
        return _build_stac_request(canonical, project, config)
    return _build_solr_request(canonical, project, config)


def _facet_name(facet: str, project: str, config: GenerationConfig) -> str:
    """Look up this generation's param name for a canonical facet, or fail loud."""
    name = config.facet_names.get(project, {}).get(facet)
    if name is None:
        raise UnrepresentableFacetError(facet, project, config.generation.value)
    return name


def _build_solr_request(
    canonical: CanonicalQuery, project: str, config: GenerationConfig
) -> SearchRequest:
    """Build an ESGF1 Solr GET: comma-joined params + retracted include control."""
    params: dict[str, str] = {}
    for facet, values in _set_canonical_facets(canonical):
        params[_facet_name(facet, project, config)] = ",".join(values)

    # Passthrough facets keep their native names, best-effort (a wrong one simply
    # returns no results on Solr).
    for name, values in canonical.extra_facets.items():
        if values:
            params[name] = ",".join(values)

    params["project"] = project
    params["type"] = "Dataset"
    params["format"] = "application/solr+json"
    params["limit"] = str(DEFAULT_LIMIT)
    retracted_key, retracted_value = _SOLR_RETRACTED_INCLUDE
    params[retracted_key] = retracted_value
    return SearchRequest(method="GET", params=params)


def _build_stac_request(
    canonical: CanonicalQuery, project: str, config: GenerationConfig
) -> SearchRequest:
    """Build an ESGF-NG STAC POST.

    A CQL2 filter (``in`` per facet, ``and`` across facets) plus the collection; no
    retracted filter, since STAC includes retracted datasets by default.
    """
    args: list[dict[str, Any]] = []
    for facet, values in _set_canonical_facets(canonical):
        prop = _facet_name(facet, project, config)
        args.append({"op": "in", "args": [{"property": prop}, list(values)]})

    # Passthrough facets, best-effort: prefix with this project's namespace if the
    # caller has not already (a wrong property returns no results, not an error).
    for name, values in canonical.extra_facets.items():
        if values:
            prop = name if ":" in name else f"{project.lower()}:{name}"
            args.append({"op": "in", "args": [{"property": prop}, list(values)]})

    collection = config.collection_ids.get(project, project.upper())
    body: dict[str, Any] = {"collections": [collection], "limit": DEFAULT_LIMIT}
    if args:
        body["filter-lang"] = "cql2-json"
        body["filter"] = args[0] if len(args) == 1 else {"op": "and", "args": args}
    # No retracted filter: STAC includes retracted by default (verified live).
    return SearchRequest(method="POST", json_body=body)
