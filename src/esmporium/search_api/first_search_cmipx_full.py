"""
Concrete, RUNNING examples: QueryCMIP{5,6,7} -> canonical -> live ESGF -> raw JSON.

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
     `Esgf1Solr(params=...)` and `EsgfNgStac(params=...)` carry a SINGLE params
     class -- not a {project: ...} mapping. For STAC the `cmipN:` prefix rides on
     the params class (`StacCMIP5.prefix`), and the collection id is taken
     straight from the query's project. Nothing in the generation is keyed by
     project name, so a generation is not coupled to any particular project.

  3. Host + generation + retry are coupled on `SearchAPI` (was `IndexNode`).
     A host speaks exactly one wire format, and carries its own tenacity retry
     policy. The default plan is just an ordered list of these.

  4. The selector ranks endpoints by the query's PROJECT.
     Every project is expressible on every generation (one params class per
     (wire, project), so nothing assumes a client cannot host a project). The
     default order is the MEASURED unique-dataset ranking per project: CMIP5 and
     CMIP6 -> ESGF1 Solr, widest-coverage node first (LIU currently leads its
     distrib sweep), then the NG/STAC catalogs; CMIP7 -> NG first (that is where
     CMIP7 lives), ESGF1 as fallback. By default search() STOPS at the first node
     that answers -- index nodes are stable and largely mirror each other -- but
     stop_at_first_result=False fans out across the whole ranking and collects
     every node's raw JSON for later merge/dedup.

         SearchAPIGeneration (interface)
              +-- Esgf1Solr(params)          -> Solr GET,  bare names, repeated params
              +-- EsgfNgStac(params)          -> STAC POST, prefix:name, CQL2 tree

Run it:  uv run python -m esmporium.search_api.first_search_cmipx_full
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, Protocol

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
    QueryCMIP6,
    QueryCMIP7,
    QueryFacet,
    QueryProtocol,
    SourceQuery,
    facet_values_from_attributes,
    from_canonical,
    to_canonical,
)

# =============================================================================
# Page-size (`limit`) rules, verified live against both wire formats.
# `limit` is the PAGE SIZE -- the max records in ONE response -- not the total
# match count (that is numFound / numberMatched). Both wires cap a page at
# 10_000. The two APIs chose different FLOORS: Solr accepts 0 (a "count only"
# request -- what every probe here uses), while STAC's floor is 1 (limit=0 ->
# HTTP 422). So a 0 that is fine for Solr must be bumped to 1 on the STAC path --
# that is the ONLY reason for the max(limit, STAC_MIN_LIMIT) coercion below.
# =============================================================================
MAX_LIMIT = 10_000  # >10_000 -> Solr HARD 400 / STAC SILENT truncation; we raise
DEFAULT_LIMIT = 10_000  # take a full page by default
SOLR_MIN_LIMIT = 0  # Solr allows a 0-doc "just the count" request
STAC_MIN_LIMIT = 1  # STAC rejects limit=0 (422); smallest valid page is 1


def _limit_error(limit: int) -> ValueError:
    """Build the error raised when a requested page exceeds MAX_LIMIT."""
    msg = f"limit {limit} exceeds MAX_LIMIT {MAX_LIMIT}; paginate instead"
    return ValueError(msg)


# =============================================================================
# Param classes: one per (wire-format, project). Same annotated idiom as the
# query classes, and, like them, NO shared base class: each is a standalone
# BaseModel that conforms to QueryProtocol structurally and delegates its one
# behaviour (`facet_values`) to the shared free function. Composition, not
# inheritance -- the four boilerplate lines are repeated on purpose to keep the
# classes independent. The wire name is the FIELD name; QueryFacet says which
# canonical facet it is.
# =============================================================================


class SolrCMIP5Parameters(BaseModel):
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


class StacParams(QueryProtocol, Protocol):
    """
    A STAC params class: a query vocabulary that also names its `cmipN:` prefix.

    The prefix lives with the params class because it co-varies exactly with the
    (STAC, project) pair the class already represents; the generation applies it.
    """

    prefix: ClassVar[str]


class StacCMIP5Parameters(BaseModel):
    """
    CMIP5 facet values under their ESGF-NG/STAC property STEMS.

    No `project` field: on STAC the project is the collection id, handled by the
    generation, not sent as a property. Fields are bare stems; the `prefix` below
    is what the generation prepends to each to form the `cmipN:` property name.
    """

    model_config = ConfigDict(extra="forbid")

    prefix: ClassVar[str] = "cmip5"

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


class SolrCMIP6Parameters(BaseModel):
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


class SolrCMIP7Parameters(BaseModel):
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


class StacCMIP6Parameters(BaseModel):
    """CMIP6 facet values under their ESGF-NG/STAC property stems."""

    model_config = ConfigDict(extra="forbid")

    prefix: ClassVar[str] = "cmip6"

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
        """Facets that are set, keyed by this class's own (stem) names."""
        return facet_values_from_attributes(self)


class StacCMIP7Parameters(BaseModel):
    """
    CMIP7 facet values under their ESGF-NG/STAC property stems.

    Note the drift: CMIP7's processing_id is `variable_branding_suffix` on the
    STAC wire (-> cmip7:variable_branding_suffix), not `branding_suffix` as the
    CMIP7 dialect and Solr name have it. This is exactly why generations own
    their OWN vocabularies instead of reusing the dialect's.
    """

    model_config = ConfigDict(extra="forbid")

    prefix: ClassVar[str] = "cmip7"

    source_id: Annotated[FacetValues, QueryFacet("model")] = ()
    institution_id: Annotated[FacetValues, QueryFacet("institution")] = ()
    experiment_id: Annotated[FacetValues, QueryFacet("experiment")] = ()
    variant_label: Annotated[FacetValues, QueryFacet("variant_label")] = ()
    variable_id: Annotated[FacetValues, QueryFacet("variable")] = ()
    frequency: Annotated[FacetValues, QueryFacet("reporting_interval")] = ()
    variable_branding_suffix: Annotated[FacetValues, QueryFacet("processing_id")] = ()
    activity_id: Annotated[FacetValues, QueryFacet("activity")] = ()
    nominal_resolution: Annotated[FacetValues, QueryFacet("resolution")] = ()
    grid_label: Annotated[FacetValues, QueryFacet("grid_label")] = ()
    realm: Annotated[FacetValues, QueryFacet("realm")] = ()

    other_terms: FacetValuesByName = {}
    source_query: SourceQuery = None

    def facet_values(self) -> dict[str, tuple[str, ...]]:
        """Facets that are set, keyed by this class's own (stem) names."""
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
# params class; nothing is keyed by project (STAC's prefix rides on the params).
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
class ESGF1Solr:
    """ESGF1 / Solr (esg-search). Flat GET; multiple values -> repeated params."""

    params: type[QueryProtocol]
    name: str = "ESGF1"

    def build_request(self, canonical: QueryCanonical, limit: int) -> Request:
        """Render `canonical` as a Solr GET request."""
        native = from_canonical(canonical=canonical, to=self.params)

        query: dict[str, Any] = {
            "format": "application/solr+json",
            "limit": limit,  # Solr floor is 0 (count-only); cap enforced in search()
            "distrib": "true",  # federated sweep: this node mirrors the federation
        }
        for wire_name, values in native.facet_values().items():
            query[wire_name] = list(values)  # httpx repeats list params
        return Request("GET", "/esg-search/search", params=query)

    def result_count(self, raw: dict[str, Any]) -> int | None:
        """Read Solr's `numFound` out of a raw response."""
        return raw.get("response", {}).get("numFound")


@dataclass(frozen=True)
class ESGFNGStac:
    """ESGF-NG / STAC 1.0 + CQL2. JSON POST; values -> a CQL2 AND-of-IN tree."""

    params: type[StacParams]
    name: str = "ESGF_NG"

    def build_request(self, canonical: QueryCanonical, limit: int) -> Request:
        """Render `canonical` as a STAC/CQL2 POST request."""
        # project is the collection id, not a property, so translate WITHOUT it.
        # Taken as the user gave it (assumed already in the correct case, e.g.
        # "CMIP5"); we do not second-guess it.

        collection = canonical.project[0]
        without_project = canonical.model_copy(update={"project": ()})
        native = from_canonical(canonical=without_project, to=self.params)

        and_clauses: list[dict[str, Any]] = [
            {"op": "=", "args": [{"property": "collection"}, collection]},
        ]
        for stem, values in native.facet_values().items():
            and_clauses.append(
                {
                    "op": "in",
                    "args": [
                        {"property": f"{self.params.prefix}:{stem}"},
                        list(values),
                    ],
                }
            )

        body = {
            "filter-lang": "cql2-json",
            # STAC's floor is 1 (limit=0 -> 422); Solr's floor is 0 ("count
            # only"), so a 0 that Solr accepts must be bumped here for STAC.
            "limit": max(limit, STAC_MIN_LIMIT),
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


# One generation per (wire-format, project). Each is handed its params class; for
# STAC the cmipN: prefix rides on the params (StacCMIP*.prefix), so a params class
# can never be paired with the wrong prefix.
SOLR_CMIP5 = ESGF1Solr(params=SolrCMIP5Parameters)
STAC_CMIP5 = ESGFNGStac(params=StacCMIP5Parameters)
SOLR_CMIP6 = ESGF1Solr(params=SolrCMIP6Parameters)
STAC_CMIP6 = ESGFNGStac(params=StacCMIP6Parameters)
SOLR_CMIP7 = ESGF1Solr(params=SolrCMIP7Parameters)
STAC_CMIP7 = ESGFNGStac(params=StacCMIP7Parameters)

# Per-project rankings, ORDERED BY MEASURED UNIQUE-DATASET COVERAGE (a live
# historical/tas probe: unique master_id, latest & not-retracted). By default
# search() stops at the first node that answers, so this order decides who that
# is; it is also the fallback chain when the top node is down. ORNL's live
# "1.5-bridge" would rank ~2nd for CMIP6, but it speaks a DIFFERENT request
# dialect (comma-joined multi-value, no `retracted` param) and would silently
# drop facet values under our Solr encoding -- deferred to its own generation.
CMIP5_APIS: list[SearchAPI] = [  # LIU > NCI > CEDA > DKRZ; NG has no CMIP5 (empty)
    SearchAPI("esg-dn1.nsc.liu.se", SOLR_CMIP5, transient_retry(3)),
    SearchAPI("esgf.nci.org.au", SOLR_CMIP5, transient_retry(4)),
    SearchAPI("esgf.ceda.ac.uk", SOLR_CMIP5, transient_retry(2)),
    SearchAPI("esgf-data.dkrz.de", SOLR_CMIP5, transient_retry(2)),
    SearchAPI("search.east.esgf.io", STAC_CMIP5, transient_retry(2)),
    SearchAPI("search.west.esgf.io", STAC_CMIP5, transient_retry(2)),
]
CMIP6_APIS: list[SearchAPI] = [  # LIU > NCI > CEDA > DKRZ Solr, then NG/STAC
    SearchAPI("esg-dn1.nsc.liu.se", SOLR_CMIP6, transient_retry(3)),
    SearchAPI("esgf.nci.org.au", SOLR_CMIP6, transient_retry(3)),
    SearchAPI("esgf.ceda.ac.uk", SOLR_CMIP6, transient_retry(2)),
    SearchAPI("esgf-data.dkrz.de", SOLR_CMIP6, transient_retry(2)),
    SearchAPI("search.east.esgf.io", STAC_CMIP6, transient_retry(2)),
    SearchAPI("search.west.esgf.io", STAC_CMIP6, transient_retry(2)),
]
CMIP7_APIS: list[SearchAPI] = [  # NG first (CMIP7 lives there); ESGF1 fallback
    SearchAPI("search.east.esgf.io", STAC_CMIP7, transient_retry(2)),
    SearchAPI("search.west.esgf.io", STAC_CMIP7, transient_retry(2)),
    SearchAPI("esgf.nci.org.au", SOLR_CMIP7, transient_retry(2)),
    SearchAPI("esgf-data.dkrz.de", SOLR_CMIP7, transient_retry(2)),
]

PROJECT_PLANS: Mapping[str, Sequence[SearchAPI]] = {
    "CMIP5": CMIP5_APIS,
    "CMIP6": CMIP6_APIS,
    "CMIP7": CMIP7_APIS,
}


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
# The endpoint selector: given the CANONICAL query and a 0-based attempt index,
# return the next SearchAPI to try, or None to stop. Injectable, so the choice
# and order of endpoints can vary without touching the search loop. Our default
# ranks endpoints by the query's PROJECT (CMIP5 -> ESGF1 first; CMIP6/CMIP7 ->
# NG first, ESGF1 as fallback); health-based ranking could slot in later.
# =============================================================================
SearchAPISelector = Callable[[QueryCanonical, int], SearchAPI | None]


def list_selector(apis: Sequence[SearchAPI]) -> SearchAPISelector:
    """Build a selector that yields `apis` in order, then stops (ignores project)."""

    def select(canonical: QueryCanonical, attempt: int) -> SearchAPI | None:
        return apis[attempt] if attempt < len(apis) else None

    return select


def project_ranked_selector(
    plans: Mapping[str, Sequence[SearchAPI]],
) -> SearchAPISelector:
    """Build a selector that yields a per-project ranking of endpoints."""

    def select(canonical: QueryCanonical, attempt: int) -> SearchAPI | None:
        apis = plans.get(canonical.project[0])
        if apis is None:
            return None  # a project we have no plan for -> nothing to try
        return apis[attempt] if attempt < len(apis) else None

    return select


DEFAULT_SELECTOR = project_ranked_selector(PROJECT_PLANS)


# =============================================================================
# By DEFAULT stop at the first node that answers: index nodes are stable and
# largely mirror one another, so one good answer is usually enough. Set
# stop_at_first_result=False to walk the WHOLE ranking and collect every node's
# raw JSON keyed by host -- their coverage differs (see the unique-count test),
# so the union is more complete. (Next PR: hand each node's raw JSON to a recorder
# that writes it to the DB, then merge/dedup by master_id across the union.)
# =============================================================================
def search(
    query: QueryProtocol,
    selector: SearchAPISelector = DEFAULT_SELECTOR,
    *,
    stop_at_first_result: bool = True,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """
    Search the ranked endpoints; return raw JSON keyed by host.

    Parameters
    ----------
    query
        The facet query, in any dialect; translated to canonical internally.
    selector
        Chooses which endpoint to try at each attempt (ranked by project).
    stop_at_first_result
        If True (default), return as soon as one node answers -- index nodes are
        stable and largely mirror each other, so one good answer usually suffices.
        If False, walk the WHOLE ranking and collect every node's raw JSON; their
        coverage differs, so the union is more complete (dedup/merge by master_id
        is the next layer).
    limit
        PAGE SIZE (records per response), NOT the total -- the total matched is
        numFound (Solr) == numberMatched (STAC). Capped at MAX_LIMIT; we raise
        above it rather than trust Solr's 400 or STAC's silent truncation. Floors
        differ per generation (Solr 0, STAC 1). Fetching beyond one page is
        pagination, deferred to the next PR with the merge/dedup + DB recorder.

    Returns
    -------
    :
        Raw JSON per host. A node that never answers (exhausted / non-transient
        error) is omitted; empty-but-valid responses are kept.
    """
    if limit > MAX_LIMIT:
        raise _limit_error(limit)

    canonical = to_canonical(query)
    results: dict[str, Any] = {}
    attempt = 0
    with httpx.Client(follow_redirects=True) as client:
        while (api := selector(canonical, attempt)) is not None:
            request = api.generation.build_request(canonical, limit)
            raw = fire(client, api, request)
            if raw is not None:
                results[api.host] = raw
                if stop_at_first_result:
                    break
            attempt += 1
    return results


# =============================================================================
# Demo: walk every endpoint the selector yields, printing each node's raw JSON,
# then show search() collecting all of them.
# =============================================================================
EXAMPLE = QueryCMIP5(
    experiment="historical",
    variable="tas",
    time_frequency="mon",
    ensemble="r1i1p1",
)

# CMIP6 in its own dialect: source_id / experiment_id / variable_id / frequency.
# Deliberately NOT pinned to a variant_label: NG east holds far fewer CMIP6
# records than the ESGF1 Solr federation for this query -- a live reminder that
# coverage differs by node, which is why stop_at_first_result=False (union) exists.
EXAMPLE_CMIP6 = QueryCMIP6(
    experiment_id="historical",
    variable_id="tas",
    frequency="mon",
)

# CMIP7 in its own dialect. Data is very sparse (NG east has ~16 datasets total),
# so we keep the query broad to get a hit; an empty result would be fine too.
EXAMPLE_CMIP7 = QueryCMIP7(
    variable_id="tas",
)


def tour(query: QueryProtocol = EXAMPLE, limit: int = 2) -> None:
    """Walk every endpoint the selector yields, printing each node's result."""
    canonical = to_canonical(query)
    print(f"query      : {query!r}")
    print(
        f"canonical  : project={canonical.project} experiment={canonical.experiment} "
        f"variable={canonical.variable} variant_label={canonical.variant_label} "
        f"reporting_interval={canonical.reporting_interval}\n"
    )

    with httpx.Client(follow_redirects=True) as client:
        attempt = 0
        while (api := DEFAULT_SELECTOR(canonical, attempt)) is not None:
            request = api.generation.build_request(canonical, limit)
            raw = fire(client, api, request)
            attempt += 1
            if raw is None:
                print(f"[{api.generation.name:8}] {api.host:22} EXHAUSTED (no answer)")
                continue
            count = api.generation.result_count(raw)
            top = _first_id(api.generation, raw)
            print(f"[{api.generation.name:8}] {api.host:22} count={count} first={top}")

    print("\n--- search() default: stop at the first node that answers ---")
    for host, raw in search(query, limit=limit).items():
        print(f"{host:22} {node_count_summary(raw)}")

    print("\n--- search(stop_at_first_result=False): every node, keyed by host ---")
    for host, raw in search(query, stop_at_first_result=False, limit=limit).items():
        print(f"{host:22} {node_count_summary(raw)}")


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
    for example in (EXAMPLE, EXAMPLE_CMIP6, EXAMPLE_CMIP7):
        tour(example)
        print("\n" + "=" * 72 + "\n")
