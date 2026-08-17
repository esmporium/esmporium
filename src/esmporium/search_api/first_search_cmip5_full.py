"""
One concrete, RUNNING example: QueryCMIP5 -> canonical -> live ESGF -> raw JSON.

This is the full-code companion to `first_search_cmip5_walkthrough.py` (which is
the narrated design doc). Everything here actually runs. It is still a single
hard-coded example -- we want to watch the whole thing work once before we lift
it into the real, tested modules.

Two design decisions from the walkthrough are now baked in:

  1. HOST AND GENERATION ARE COUPLED.
     A host speaks exactly one wire format, so `IndexNode` carries both. The
     retry plan is just an ordered list of these nodes.

  2. NO `if generation == ...` LADDER.
     Each generation is a class behind one interface (`SearchAPIGeneration`).
     The node holds its generation; the search loop calls methods with zero
     branching. Adding a generation = adding a class.

         SearchAPIGeneration (interface)
              ├── Esgf1Solr    -> Solr GET,  per-project name dict, repeated params
              └── EsgfNgStac   -> STAC POST, cmipN: name dict, CQL2 AND-of-IN tree

Run it:  uv run python -m esmporium.search_api.first_search_cmip5_full

(NOTE: `httpx` is used directly here and still needs adding as a real dependency
in pyproject.toml -- right now it is only present transitively.)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from esmporium.query import QueryCanonical, QueryCMIP5, to_canonical


# =============================================================================
# The request a generation produces. `method` carries the GET/POST difference,
# so the fire loop never has to branch on it.
# =============================================================================
@dataclass(frozen=True)
class Request:
    """A ready-to-send HTTP request, minus the host."""

    method: str
    path: str
    params: dict[str, Any] | None = None
    json_body: dict[str, Any] | None = None


# =============================================================================
# The generation interface + its two implementations.
# Each generation OWNS its per-project name dictionary and its value-combining
# rules -- deliberately not reusing query/'s dialect specs, because the wire
# names drift from the dialect names (e.g. CMIP7 branding_suffix ->
# cmip7:variable_branding_suffix).
# =============================================================================
class SearchAPIGeneration(Protocol):
    """The wire format of a family of endpoints."""

    name: str

    def build_request(self, canonical: QueryCanonical, limit: int) -> Request:
        """Turn a canonical query into a request in this generation's format."""
        ...

    def result_count(self, raw: dict[str, Any]) -> int | None:
        """Read the match count out of a raw response (for logging/emptiness)."""
        ...


# Canonical facet -> Solr param name, per project. CMIP5 is filled in; other
# projects would be added as sibling entries (same shape).
_SOLR_NAMES: dict[str, dict[str, str]] = {
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
}


@dataclass(frozen=True)
class Esgf1Solr:
    """ESGF1 / Solr (esg-search). Flat GET; multiple values -> repeated params."""

    name: str = "ESGF1"

    def build_request(self, canonical: QueryCanonical, limit: int) -> Request:
        """Render `canonical` as a Solr GET request."""
        project = canonical.project[0]
        names = _SOLR_NAMES[project]

        params: dict[str, Any] = {
            "project": project,
            "format": "application/solr+json",
            "limit": limit,
        }
        for canonical_name, wire_name in names.items():
            values = getattr(canonical, canonical_name)
            if values:
                params[wire_name] = list(values)  # httpx repeats list params
        # CMIP5-specific facets (e.g. `product`) would be merged from
        # canonical.query_specific_facets here.
        return Request("GET", "/esg-search/search", params=params)

    def result_count(self, raw: dict[str, Any]) -> int | None:
        """Read Solr's `numFound` out of a raw response."""
        return raw.get("response", {}).get("numFound")


# Canonical facet -> STAC property name, per project.
_STAC_NAMES: dict[str, dict[str, str]] = {
    "CMIP5": {
        "model": "cmip5:model",
        "institution": "cmip5:institute",
        "experiment": "cmip5:experiment",
        "variable": "cmip5:variable",
        "variant_label": "cmip5:ensemble",
        "reporting_interval": "cmip5:time_frequency",
        "processing_id": "cmip5:cmor_table",
        "realm": "cmip5:realm",
    },
}


@dataclass(frozen=True)
class EsgfNgStac:
    """ESGF-NG / STAC 1.0 + CQL2. JSON POST; values -> a CQL2 AND-of-IN tree."""

    name: str = "ESGF_NG"

    def build_request(self, canonical: QueryCanonical, limit: int) -> Request:
        """Render `canonical` as a STAC/CQL2 POST request."""
        project = canonical.project[0]
        names = _STAC_NAMES[project]

        # collection id is UPPERCASE; retracted is a bare boolean property.
        and_clauses: list[dict[str, Any]] = [
            {"op": "=", "args": [{"property": "collection"}, project.upper()]},
        ]
        for canonical_name, wire_name in names.items():
            values = getattr(canonical, canonical_name)
            if values:
                and_clauses.append(
                    {"op": "in", "args": [{"property": wire_name}, list(values)]}
                )

        body = {
            "filter-lang": "cql2-json",
            "limit": max(limit, 1),  # STAC rejects limit=0; Solr allows it
            "filter": {"op": "and", "args": and_clauses},
        }
        return Request("POST", "/search", json_body=body)

    def result_count(self, raw: dict[str, Any]) -> int | None:
        """Read the match count, falling back to the feature count for west."""
        # east returns numberMatched; west omits it, so fall back to feature count
        matched = raw.get("numberMatched")
        if matched is not None:
            return matched
        return len(raw.get("features", []))


# Stateless, so shared singletons are fine.
ESGF1 = Esgf1Solr()
ESGF_NG = EsgfNgStac()


# =============================================================================
# A node = a host + the generation it speaks + its own retry budget.
# =============================================================================
@dataclass(frozen=True)
class IndexNode:
    """One endpoint we can hit."""

    host: str
    generation: SearchAPIGeneration
    retries: int = 2

    def url(self, request: Request) -> str:
        """Build the full URL for `request` against this node's host."""
        return f"https://{self.host}{request.path}"


# The retry/preference plan you described, top to bottom.
# ORNL is dead for ESGF1 (serves a web app now), so DKRZ is the live fallback.
NODE_PLAN: list[IndexNode] = [
    IndexNode("esgf.nci.org.au", ESGF1, retries=3),  # NCI, tried a few times
    IndexNode("esgf-data.dkrz.de", ESGF1, retries=2),  # ESGF1 fallback (was ORNL)
    IndexNode("search.east.esgf.io", ESGF_NG, retries=2),  # empty for CMIP5, that's ok
    IndexNode("search.west.esgf.io", ESGF_NG, retries=2),  # empty for CMIP5, that's ok
]


# A 5xx from ESGF1 means a load-balanced backend is flapping, not a real answer.
_TRANSIENT_STATUS_FLOOR = 500


# =============================================================================
# Firing one node, with its own retry budget. No generation branching here.
# =============================================================================
def fire(
    client: httpx.Client, node: IndexNode, request: Request
) -> dict[str, Any] | None:
    """
    Send `request` to `node`, retrying on transient failure.

    Returns the raw JSON on success, or None if the node's budget is exhausted.
    ESGF1 flaps 501/200 across load-balanced backends, so >=500 is transient.
    """
    url = node.url(request)
    for _attempt in range(node.retries + 1):
        try:
            resp = client.request(
                request.method,
                url,
                params=request.params,
                json=request.json_body,
                timeout=30,
            )
            if resp.status_code >= _TRANSIENT_STATUS_FLOOR:
                continue  # transient -> retry the SAME node
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError):
            continue  # transport error or bad JSON -> retry the SAME node
    return None


_ALL_NODES_EXHAUSTED = "every node in the plan was exhausted"


# =============================================================================
# Production shape: walk the plan, first node that answers wins.
# =============================================================================
def search(
    query: QueryCMIP5,
    nodes: list[IndexNode] = NODE_PLAN,
    limit: int = 10,
) -> dict[str, Any]:
    """
    Search for a CMIP5 query and return raw JSON, keyed by project.

    First node that returns a (non-error) response wins.
    """
    canonical = to_canonical(query)
    with httpx.Client(follow_redirects=True) as client:
        for node in nodes:
            request = node.generation.build_request(canonical, limit)
            raw = fire(client, node, request)
            if raw is not None:
                return {canonical.project[0]: raw}
    raise RuntimeError(_ALL_NODES_EXHAUSTED)


# =============================================================================
# Demo: hit EVERY node so we can watch each generation's raw JSON, then show the
# production first-success result. (The tour is illustration; `search` above is
# the real behaviour.)
# =============================================================================
EXAMPLE = QueryCMIP5(
    experiment="historical",
    variable="tas",
    time_frequency="mon",
    ensemble="r1i1p1",
)


def tour(query: QueryCMIP5 = EXAMPLE, limit: int = 2) -> None:
    """Hit every node and print each generation's raw result, then run `search`."""
    canonical = to_canonical(query)
    print(f"query      : {query!r}")
    print(
        f"canonical  : project={canonical.project} experiment={canonical.experiment} "
        f"variable={canonical.variable} variant_label={canonical.variant_label} "
        f"reporting_interval={canonical.reporting_interval}\n"
    )

    with httpx.Client(follow_redirects=True) as client:
        for node in NODE_PLAN:
            request = node.generation.build_request(canonical, limit)
            raw = fire(client, node, request)
            if raw is None:
                print(
                    f"[{node.generation.name:8}] {node.host:22} EXHAUSTED (no answer)"
                )
                continue
            count = node.generation.result_count(raw)
            top = _first_id(node.generation, raw)
            print(
                f"[{node.generation.name:8}] {node.host:22} count={count} first={top}"
            )

    print("\n--- production search() (first success wins) ---")
    result = search(query, limit=limit)
    ((project, raw),) = result.items()
    print(
        f"returned raw JSON for project {project!r}: "
        f"{node_count_summary(raw)}  keys={list(raw)[:6]}"
    )


def _first_id(generation: SearchAPIGeneration, raw: dict[str, Any]) -> str | None:
    if generation.name == "ESGF1":
        docs = raw.get("response", {}).get("docs", [])
        return docs[0].get("id") if docs else None
    feats = raw.get("features", [])
    return feats[0].get("id") if feats else None


def node_count_summary(raw: dict[str, Any]) -> str:
    """Summarise a raw response's match count without knowing its generation."""
    if "response" in raw:
        return f"numFound={raw['response'].get('numFound')}"
    return f"numberMatched={raw.get('numberMatched')}"


if __name__ == "__main__":
    tour()
