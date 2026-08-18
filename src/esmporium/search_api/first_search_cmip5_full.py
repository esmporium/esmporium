"""
One concrete, RUNNING example: QueryCMIP5 -> canonical -> live ESGF -> raw JSON.

Full-code companion to `first_search_cmip5_walkthrough.py` (the narrated design
doc). Still a single hard-coded example: we want to watch the whole thing work
once before lifting it into the real, tested modules.

Design decisions baked in here:

  1. NAME translation reuses the `query/` machinery.
     Each (wire-format, project) name table is an ANNOTATED class -- the same
     `Annotated[FacetValues, QueryFacet("<canonical>")]` idiom as the query
     classes -- so `from_canonical` populates it for free and every mapping is
     validated. No hand-written rename dicts.

  2. A generation is a pure translator, handed its vocabulary.
     `Esgf1Solr(params=...)` and `EsgfNgStac(params=..., prefix="cmip5")` carry a
     SINGLE params class (and, for STAC, a single prefix string) -- not a
     {project: ...} mapping. Nothing in the generation is keyed by project name,
     so a generation is not coupled to any particular project.

  3. Host + generation + retry are coupled on `SearchAPI` (was `IndexNode`).
     A host speaks exactly one wire format, and carries its own tenacity retry
     policy. The default plan is just an ordered list of these.

  4. Any project on any generation is EXPRESSIBLE.
     We define SolrCMIP7 (CMIP7 -> ESGF1) as well, to prove we do not bake in an
     assumption that a client cannot host a project. Our DEFAULT plan still
     assumes the usual homes (CMIP5 lives on ESGF1; NG has no CMIP5 yet, which is
     fine) -- a user who knows otherwise injects their own `SearchAPI` list.

         SearchAPIGeneration (interface)
              +-- Esgf1Solr(params)          -> Solr GET,  bare names, repeated params
              +-- EsgfNgStac(params, prefix)  -> STAC POST, prefix:name, CQL2 tree

Run it:  uv run python -m esmporium.search_api.first_search_cmip5_full
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict
from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from esmporium.query import (
    FacetValues,
    FacetValuesByName,
    QueryCanonical,
    QueryCMIP5,
    QueryFacet,
    QueryProtocol,
    SourceQuery,
    facet_values_from_attributes,
    from_canonical,
    to_canonical,
)


# =============================================================================
# Param classes: one per (wire-format, project). Same annotated idiom as the
# query classes, and, like them, NO shared base class: each is a standalone
# BaseModel that conforms to QueryProtocol structurally and delegates its one
# behaviour (`facet_values`) to the shared free function. Composition, not
# inheritance -- the four boilerplate lines are repeated on purpose to keep the
# classes independent. The wire name is the FIELD name; QueryFacet says which
# canonical facet it is.
# =============================================================================
class SolrCMIP5(BaseModel):
    """CMIP5 facet values under their ESGF1/Solr param names."""

    model_config = ConfigDict(extra="forbid")

    project: Annotated[FacetValues, QueryFacet("project")] = ()
    model: Annotated[FacetValues, QueryFacet("model")] = ()
    institute: Annotated[FacetValues, QueryFacet("institution")] = ()
    experiment: Annotated[FacetValues, QueryFacet("experiment")] = ()
    variable: Annotated[FacetValues, QueryFacet("variable")] = ()
    ensemble: Annotated[FacetValues, QueryFacet("variant_label")] = ()
    time_frequency: Annotated[FacetValues, QueryFacet("reporting_interval")] = ()
    cmor_table: Annotated[FacetValues, QueryFacet("processing_id")] = ()
    realm: Annotated[FacetValues, QueryFacet("realm")] = ()
    product: Annotated[FacetValues, QueryFacet(None)] = ()

    other_terms: FacetValuesByName = {}
    source_query: SourceQuery = None

    def facet_values(self) -> dict[str, tuple[str, ...]]:
        """Facets that are set, keyed by this class's own (wire) names."""
        return facet_values_from_attributes(self)


class StacCMIP5(BaseModel):
    """
    CMIP5 facet values under their ESGF-NG/STAC property STEMS.

    No `project` field: on STAC the project is the collection id, handled by the
    generation, not sent as a property. The `cmipN:` prefix is added by the
    generation too, so these are bare stems.
    """

    model_config = ConfigDict(extra="forbid")

    model: Annotated[FacetValues, QueryFacet("model")] = ()
    institute: Annotated[FacetValues, QueryFacet("institution")] = ()
    experiment: Annotated[FacetValues, QueryFacet("experiment")] = ()
    variable: Annotated[FacetValues, QueryFacet("variable")] = ()
    ensemble: Annotated[FacetValues, QueryFacet("variant_label")] = ()
    time_frequency: Annotated[FacetValues, QueryFacet("reporting_interval")] = ()
    cmor_table: Annotated[FacetValues, QueryFacet("processing_id")] = ()
    realm: Annotated[FacetValues, QueryFacet("realm")] = ()
    product: Annotated[FacetValues, QueryFacet(None)] = ()

    other_terms: FacetValuesByName = {}
    source_query: SourceQuery = None

    def facet_values(self) -> dict[str, tuple[str, ...]]:
        """Facets that are set, keyed by this class's own (stem) names."""
        return facet_values_from_attributes(self)


class SolrCMIP6(BaseModel):
    """
    CMIP6 facet values under their ESGF1/Solr param names.

    ESGF1 genuinely still hosts CMIP6 (live-confirmed), so this is a real,
    usable mapping -- not just a proof of expressibility. Not in the default
    CMIP5 plan; injected when searching CMIP6 on an ESGF1 node.
    """

    model_config = ConfigDict(extra="forbid")

    project: Annotated[FacetValues, QueryFacet("project")] = ()
    source_id: Annotated[FacetValues, QueryFacet("model")] = ()
    institution_id: Annotated[FacetValues, QueryFacet("institution")] = ()
    experiment_id: Annotated[FacetValues, QueryFacet("experiment")] = ()
    variant_label: Annotated[FacetValues, QueryFacet("variant_label")] = ()
    variable_id: Annotated[FacetValues, QueryFacet("variable")] = ()
    frequency: Annotated[FacetValues, QueryFacet("reporting_interval")] = ()
    table_id: Annotated[FacetValues, QueryFacet("processing_id")] = ()
    activity_id: Annotated[FacetValues, QueryFacet("activity")] = ()
    nominal_resolution: Annotated[FacetValues, QueryFacet("resolution")] = ()
    grid_label: Annotated[FacetValues, QueryFacet("grid_label")] = ()
    realm: Annotated[FacetValues, QueryFacet("realm")] = ()

    other_terms: FacetValuesByName = {}
    source_query: SourceQuery = None

    def facet_values(self) -> dict[str, tuple[str, ...]]:
        """Facets that are set, keyed by this class's own (wire) names."""
        return facet_values_from_attributes(self)


class SolrCMIP7(BaseModel):
    """
    CMIP7 facet values under ESGF1/Solr param names (best-guess).

    Defined to prove that any project is expressible on any generation -- we do
    NOT assume ESGF1 cannot host CMIP7. Not part of the default plan.
    """

    model_config = ConfigDict(extra="forbid")

    project: Annotated[FacetValues, QueryFacet("project")] = ()
    source_id: Annotated[FacetValues, QueryFacet("model")] = ()
    institution_id: Annotated[FacetValues, QueryFacet("institution")] = ()
    experiment_id: Annotated[FacetValues, QueryFacet("experiment")] = ()
    variable_id: Annotated[FacetValues, QueryFacet("variable")] = ()
    variant_label: Annotated[FacetValues, QueryFacet("variant_label")] = ()
    frequency: Annotated[FacetValues, QueryFacet("reporting_interval")] = ()
    branding_suffix: Annotated[FacetValues, QueryFacet("processing_id")] = ()
    activity_id: Annotated[FacetValues, QueryFacet("activity")] = ()
    grid_label: Annotated[FacetValues, QueryFacet("grid_label")] = ()
    nominal_resolution: Annotated[FacetValues, QueryFacet("resolution")] = ()
    realm: Annotated[FacetValues, QueryFacet("realm")] = ()

    other_terms: FacetValuesByName = {}
    source_query: SourceQuery = None

    def facet_values(self) -> dict[str, tuple[str, ...]]:
        """Facets that are set, keyed by this class's own (wire) names."""
        return facet_values_from_attributes(self)


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
# The generation interface + its two implementations. Each is handed a single
# params class (and, for STAC, a single prefix); nothing is keyed by project.
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


@dataclass(frozen=True)
class Esgf1Solr:
    """ESGF1 / Solr (esg-search). Flat GET; multiple values -> repeated params."""

    params: type[QueryProtocol]
    name: str = "ESGF1"

    def build_request(self, canonical: QueryCanonical, limit: int) -> Request:
        """Render `canonical` as a Solr GET request."""
        native = from_canonical(canonical=canonical, to=self.params)

        query: dict[str, Any] = {"format": "application/solr+json", "limit": limit}
        for wire_name, values in native.facet_values().items():
            query[wire_name] = list(values)  # httpx repeats list params
        return Request("GET", "/esg-search/search", params=query)

    def result_count(self, raw: dict[str, Any]) -> int | None:
        """Read Solr's `numFound` out of a raw response."""
        return raw.get("response", {}).get("numFound")


@dataclass(frozen=True)
class EsgfNgStac:
    """ESGF-NG / STAC 1.0 + CQL2. JSON POST; values -> a CQL2 AND-of-IN tree."""

    params: type[QueryProtocol]
    prefix: str
    name: str = "ESGF_NG"

    def build_request(self, canonical: QueryCanonical, limit: int) -> Request:
        """Render `canonical` as a STAC/CQL2 POST request."""
        # project is the collection id, not a property, so translate WITHOUT it.
        project = canonical.project[0]
        without_project = canonical.model_copy(update={"project": ()})
        native = from_canonical(canonical=without_project, to=self.params)

        # collection id is UPPERCASE.
        and_clauses: list[dict[str, Any]] = [
            {"op": "=", "args": [{"property": "collection"}, project.upper()]},
        ]
        for stem, values in native.facet_values().items():
            and_clauses.append(
                {
                    "op": "in",
                    "args": [{"property": f"{self.prefix}:{stem}"}, list(values)],
                }
            )

        body = {
            "filter-lang": "cql2-json",
            "limit": max(limit, 1),  # STAC rejects limit=0; Solr allows it
            "filter": {"op": "and", "args": and_clauses},
        }
        return Request("POST", "/search", json_body=body)

    def result_count(self, raw: dict[str, Any]) -> int | None:
        """Read the match count, falling back to the feature count for west."""
        matched = raw.get("numberMatched")
        if matched is not None:
            return matched
        return len(raw.get("features", []))


# =============================================================================
# Retry policy, via tenacity. A 5xx from ESGF1 means a load-balanced backend is
# flapping, so it (and transport errors) are transient and retried; a 4xx is a
# real "no" and is not retried.
# =============================================================================
_TRANSIENT_STATUS_FLOOR = 500


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= _TRANSIENT_STATUS_FLOOR
    return isinstance(exc, httpx.TransportError)


def transient_retry(attempts: int) -> Retrying:
    """Build a tenacity policy that retries transient failures with backoff."""
    return Retrying(
        stop=stop_after_attempt(attempts),
        wait=wait_exponential(multiplier=0.5, max=8),
        retry=retry_if_exception(_is_transient),
        reraise=True,
    )


# =============================================================================
# A SearchAPI = a host + the generation it speaks + its own retry policy.
# =============================================================================
@dataclass(frozen=True)
class SearchAPI:
    """One endpoint we can hit."""

    host: str
    generation: SearchAPIGeneration
    retrying: Retrying

    def url(self, request: Request) -> str:
        """Build the full URL for `request` against this host."""
        return f"https://{self.host}{request.path}"


# Generations for the CMIP5 example: each handed its params (and prefix).
SOLR_CMIP5 = Esgf1Solr(params=SolrCMIP5)
# Should we just put prefix on the classes like StacCMIP5?
# They feel tightly coupled to me so it would be clearer if they were defined together,
# but maybe there is a reason not to add this coupling.
STAC_CMIP5 = EsgfNgStac(params=StacCMIP5, prefix="cmip5")

# The retry/preference plan, top to bottom. ORNL is dead for ESGF1 (serves a web
# app now), so DKRZ is the live fallback.
# NCI is tried a few times, then DKRZ; NG east/west are empty for CMIP5 (fine).
DEFAULT_SEARCH_APIS: list[SearchAPI] = [
    SearchAPI("esgf.nci.org.au", SOLR_CMIP5, transient_retry(4)),
    SearchAPI("esgf-data.dkrz.de", SOLR_CMIP5, transient_retry(2)),
    SearchAPI("search.east.esgf.io", STAC_CMIP5, transient_retry(2)),
    SearchAPI("search.west.esgf.io", STAC_CMIP5, transient_retry(2)),
]

# We do NOT assume a client cannot host a project. To search, say, CMIP7 on an
# ESGF1 node (not our default assumption), a user injects their own list:
#
#   cmip7_on_esgf1 = SearchAPI(
#       "some-esgf1-host", Esgf1Solr(params=SolrCMIP7), transient_retry(2)
#   )
#   search(some_cmip7_query, apis=[cmip7_on_esgf1])


# =============================================================================
# Firing one API, driven by its tenacity policy. No generation branching here.
# =============================================================================
def fire(
    client: httpx.Client, api: SearchAPI, request: Request
) -> dict[str, Any] | None:
    """
    Send `request` to `api` under its retry policy.

    Returns the raw JSON on success, or None if the policy gives up (or the
    endpoint gives a non-transient "no", e.g. a 4xx or unparseable body).
    """

    def _once() -> dict[str, Any]:
        resp = client.request(
            request.method,
            api.url(request),
            params=request.params,
            json=request.json_body,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    try:
        return api.retrying(_once)
    except (httpx.HTTPError, ValueError):
        return None


# =============================================================================
# Production shape: walk the plan, first API that answers wins.
# =============================================================================
_ALL_APIS_EXHAUSTED = "every search API in the plan was exhausted"


def search(
    query: QueryCMIP5,
    apis: list[SearchAPI] = DEFAULT_SEARCH_APIS,
    limit: int = 10,
) -> dict[str, Any]:
    """
    Search for a CMIP5 query and return raw JSON, keyed by project.

    First API that returns a (non-error) response wins.
    """
    canonical = to_canonical(query)
    with httpx.Client(follow_redirects=True) as client:
        for api in apis:
            request = api.generation.build_request(canonical, limit)
            raw = fire(client, api, request)
            if raw is not None:
                return {canonical.project[0]: raw}
    raise RuntimeError(_ALL_APIS_EXHAUSTED)


# =============================================================================
# Demo: hit EVERY API so we can watch each generation's raw JSON, then show the
# production first-success result. (The tour is illustration; `search` is real.)
# =============================================================================
EXAMPLE = QueryCMIP5(
    experiment="historical",
    variable="tas",
    time_frequency="mon",
    ensemble="r1i1p1",
)


def tour(query: QueryCMIP5 = EXAMPLE, limit: int = 2) -> None:
    """Hit every API and print each generation's raw result, then run `search`."""
    canonical = to_canonical(query)
    print(f"query      : {query!r}")
    print(
        f"canonical  : project={canonical.project} experiment={canonical.experiment} "
        f"variable={canonical.variable} variant_label={canonical.variant_label} "
        f"reporting_interval={canonical.reporting_interval}\n"
    )

    with httpx.Client(follow_redirects=True) as client:
        # Let's add our client selector function now
        # and add searching different projects too
        # to see how things go once we break that flow out.
        for api in DEFAULT_SEARCH_APIS:
            request = api.generation.build_request(canonical, limit)
            raw = fire(client, api, request)
            if raw is None:
                print(f"[{api.generation.name:8}] {api.host:22} EXHAUSTED (no answer)")
                continue
            count = api.generation.result_count(raw)
            top = _first_id(api.generation, raw)
            print(f"[{api.generation.name:8}] {api.host:22} count={count} first={top}")

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
