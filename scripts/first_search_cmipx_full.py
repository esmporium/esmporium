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
     validated. No hand-written rename dicts. These now live in
     `esmporium.search.esgf_generations`, which is where the generations
     themselves are headed too.

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

Run it:  uv run python scripts/first_search_cmipx_full.py
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from esmporium.query import (
    QueryCanonical,
    QueryCMIP5,
    QueryCMIP6,
    QueryCMIP7,
    QueryProtocol,
    to_canonical,
)

# The generations (wire formats) and the param classes (one per
# (wire-format, project)) now live in `esmporium.search.esgf_generations`.
# What is left here is everything a generation deliberately does NOT know about:
# which hosts exist, what to do when one does not answer, and in what order to
# try them.
from esmporium.search import (
    DEFAULT_LIMIT,
    ESGF1Solr,
    ESGF15Bridge,
    ESGFNGStac,
    Request,
    SearchAPIGeneration,
    SolrCMIP5Parameters,
    SolrCMIP6Parameters,
    SolrCMIP7Parameters,
    StacCMIP5Parameters,
    StacCMIP6Parameters,
    StacCMIP7Parameters,
)

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

# ORNL's ESGF-1.5 bridge reuses the SAME Solr param name tables (names match);
# only the request encoding differs, which the generation handles.
BRIDGE_CMIP5 = ESGF15Bridge(params=SolrCMIP5Parameters)
BRIDGE_CMIP6 = ESGF15Bridge(params=SolrCMIP6Parameters)

# Per-project rankings, ORDERED BY MEASURED UNIQUE-DATASET COVERAGE (a live
# historical/tas probe: unique master_id, latest & not-retracted). By default
# search() stops at the first node that answers, so this order decides who that
# is; it is also the fallback chain when the top node is down. ORNL's live
# "1.5-bridge" (ESGF15Bridge -- Solr-shaped replies, comma-joined request
# dialect) is included at its measured rank (CMIP6 2nd, CMIP5 3rd).
CMIP5_APIS: list[SearchAPI] = [  # LIU > NCI > ORNL > CEDA > DKRZ; NG has no CMIP5
    SearchAPI("esg-dn1.nsc.liu.se", SOLR_CMIP5, transient_retry(3)),
    SearchAPI("esgf.nci.org.au", SOLR_CMIP5, transient_retry(4)),
    SearchAPI("esgf-node.ornl.gov", BRIDGE_CMIP5, transient_retry(2)),
    SearchAPI("esgf.ceda.ac.uk", SOLR_CMIP5, transient_retry(2)),
    SearchAPI("esgf-data.dkrz.de", SOLR_CMIP5, transient_retry(2)),
    SearchAPI("search.east.esgf.io", STAC_CMIP5, transient_retry(2)),
    SearchAPI("search.west.esgf.io", STAC_CMIP5, transient_retry(2)),
]
CMIP6_APIS: list[SearchAPI] = [  # LIU > ORNL > NCI > CEDA > DKRZ, then NG/STAC
    SearchAPI("esg-dn1.nsc.liu.se", SOLR_CMIP6, transient_retry(3)),
    SearchAPI("esgf-node.ornl.gov", BRIDGE_CMIP6, transient_retry(2)),
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
        numFound (Solr) == numberMatched (STAC). The generation checks it against
        MIN_LIMIT/MAX_LIMIT and raises rather than trusting Solr's 400 or STAC's
        silent truncation. Fetching beyond one page is pagination, deferred to the
        next PR with the merge/dedup + DB recorder.

    Returns
    -------
    :
        Raw JSON per host. A node that never answers (exhausted / non-transient
        error) is omitted; empty-but-valid responses are kept.
    """
    canonical = to_canonical(query)
    results: dict[str, Any] = {}
    attempt = 0
    with httpx.Client(follow_redirects=True) as client:
        while (api := selector(canonical, attempt)) is not None:
            request = api.generation.build_search_request(canonical, limit)
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
            request = api.generation.build_search_request(canonical, limit)
            raw = fire(client, api, request)
            attempt += 1
            if raw is None:
                print(f"[{api.generation.name:8}] {api.host:22} EXHAUSTED (no answer)")
                continue
            count = api.generation.result_count(raw)
            top = _first_id(raw)
            print(f"[{api.generation.name:8}] {api.host:22} count={count} first={top}")

    print("\n--- search() default: stop at the first node that answers ---")
    for host, raw in search(query, limit=limit).items():
        print(f"{host:22} {node_count_summary(raw)}")

    print("\n--- search(stop_at_first_result=False): every node, keyed by host ---")
    for host, raw in search(query, stop_at_first_result=False, limit=limit).items():
        print(f"{host:22} {node_count_summary(raw)}")


def _first_id(raw: dict[str, Any]) -> str | None:
    if "response" in raw:  # Solr-shaped (ESGF1 esg-search or the ESGF-1.5 bridge)
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
