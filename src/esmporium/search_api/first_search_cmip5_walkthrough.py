# ruff: noqa  # narrated design doc / pseudocode, not a real module — skip linting
# """
# A SINGLE concrete walkthrough: QueryCMIP5 -> canonical -> live ESGF -> raw JSON.

# This file is PSEUDOCODE / a thinking aid, not the final module.
# The goal is to see the whole workflow *once*, top to bottom, for one hard-coded
# example, before we pull it apart into the real, abstracted modules.

# Nothing here is imported by the package. Zeb's original sketch lives next door in
# `first_search.py`; this is the "make it concrete for CMIP5" companion.

# =============================================================================
# THE THREE AXES (this is the whole mental model)
# =============================================================================

# The query/ package already gave us TWO independent axes:

#     (1) DIALECT              (2) PROJECT
#     the vocabulary you       the thing you are
#     *type your query in*     *searching for*
#     e.g. QueryCMIP5 calls    e.g. project=("CMIP5",)
#     it `ensemble`,           -- just another facet
#     QueryCMIP6 calls the
#     same thing `variant_label`

#     Any dialect can search for any project. Both meet in the middle at
#     QueryCanonical (the `variant_label` / `model` / ... vocabulary).

#         QueryCMIP5 ----to_canonical----> QueryCanonical <----from_canonical---- QueryCMIP6
#         (dialect in)                     (neutral middle)                       (dialect out)

# Going LIVE adds a THIRD axis the query/ package never had to care about:

#     (3) GENERATION -- the *wire format* of the endpoint we actually hit
#         - ESGF1        : Solr  (esg-search)         -> flat GET query params
#         - ESGF_NG_EAST : STAC 1.0 + CQL2            -> JSON POST body
#         - ESGF_NG_WEST : STAC 1.0 + CQL2            -> JSON POST body

# Crucially, the wire vocabulary is NOT the dialect vocabulary. CMIP7's dialect
# says `branding_suffix`; the ESGF-NG wire says `cmip7:variable_branding_suffix`.
# So we CANNOT reuse query/'s canonical->dialect translation for the wire step.

# =============================================================================
# THE 2D TRANSLATION PROBLEM (Zeb's open question in first_search.py)
# =============================================================================

# The canonical -> wire-request translation depends on TWO things at once:

#                         project = CMIP5   CMIP6                CMIP7
#                       +-----------------+--------------------+----------------------+
#         gen = ESGF1   | model, ensemble | source_id, ...     | source_id, ...       |  Solr names
#                       +-----------------+--------------------+----------------------+
#         gen = ESGF_NG | cmip5:... (none | cmip6:variant_...  | cmip7:variable_      |  STAC/CQL2 names
#                       |  ingested yet)  |                    |   branding_suffix    |
#                       +-----------------+--------------------+----------------------+

# RECOMMENDATION (my answer to Zeb's "how do I handle this 2D space"):
#     Make the GENERATION own the full translation, and give each Generation its
#     OWN per-project name dictionary + its OWN value-encoding rules.

#     generation.build_request(canonical) does:
#         1. look at canonical.project to pick the right sub-dictionary
#         2. rename canonical facets -> that generation's wire names for that project
#         3. encode/combine values the way that generation's wire format wants
#            (Solr: repeated params; STAC: a CQL2 boolean tree)

#     Why this and not the alternatives:
#       - NOT methods-on-the-client (`to_cmip5_style_parameters`, ...): that bakes
#         the project list into the type; adding a project edits every client.
#       - NOT a grid of free functions (`convert_query_to_esgf1_cmip5_...` x N x M):
#         that IS the 2D grid, just spelled out by hand -- combinatorial and easy
#         to get half-updated.
#       - The Generation-owns-a-dict approach turns the 2D grid into DATA (one
#         dict per generation, keyed by project) instead of code, and keeps the
#         "how do values combine" logic in exactly one place per wire format.

#     The dictionaries are the generation's OWN -- deliberately not imported from
#     the query/ dialect specs -- because the wire names drift from the dialect
#     names (the branding_suffix example) and we want that drift to be visible and
#     local, not a surprise coupling.
# """

# from __future__ import annotations

# from typing import Any

# from esmporium.query import QueryCanonical, QueryCMIP5, to_canonical

# # =============================================================================
# # STEP 0.  A concrete CMIP5 query, in CMIP5's own dialect.
# # =============================================================================
# # Deliberately hard-coded. This is the "one example" we want to watch flow
# # through the whole system.
# EXAMPLE = QueryCMIP5(
#     experiment="historical",
#     variable="tas",
#     time_frequency="mon",
#     ensemble="r1i1p1",
#     # model="ACCESS1-0",   # uncomment to narrow further
# )


# # =============================================================================
# # STEP 1.  dialect -> canonical.   (this part is DONE, we just call it)
# # =============================================================================
# def to_canonical_query(query: QueryCMIP5) -> QueryCanonical:
#     # This is literally just the existing function. Shown as a named step so the
#     # walkthrough reads as one pipeline.
#     #   ensemble       -> variant_label
#     #   time_frequency -> reporting_interval
#     #   cmor_table     -> processing_id
#     #   product        -> stays put (CMIP5-specific, no canonical equivalent)
#     return to_canonical(query)


# # =============================================================================
# # STEP 2.  Describe the endpoints we can hit, and the ORDER to try them.
# # =============================================================================
# # An IndexNode is just "a host + which wire format it speaks".
# # (In the real module this is a small frozen dataclass; here, a tuple is enough
# #  to see the shape.)   IndexNode = (host, generation)
# #
# # NOTE: hostnames below are my best guess and are flagged for you to confirm --
# #       see the questions in the chat. Treat them as placeholders.

# ESGF1 = "ESGF1"  # Solr
# ESGF_NG_EAST = "ESGF_NG_EAST"  # STAC/CQL2
# ESGF_NG_WEST = "ESGF_NG_WEST"  # STAC/CQL2

# # The retry/preference plan you described, read top-to-bottom:
# #   - NCI ESGF1 index node, tried a few times (it flaps 501/200, retry catches it)
# #   - then ORNL ESGF1 index node
# #   - then ESGF-NG east   (won't have CMIP5 -> empty, which is FINE)
# #   - then ESGF-NG west   (same)
# #
# # We model "try NCI a few times" as the node appearing with a per-node retry
# # budget, NOT by listing it 3x. The selector below advances on empty and retries
# # on error, so the budget lives on the node.
# NODE_PLAN: list[dict[str, Any]] = [
#     {"host": "esgf.nci.org.au", "generation": ESGF1, "retries": 3},
#     {"host": "esgf-node.ornl.gov", "generation": ESGF1, "retries": 2},
#     {"host": "search.east.esgf.io", "generation": ESGF_NG_EAST, "retries": 2},
#     {"host": "search.west.esgf.io", "generation": ESGF_NG_WEST, "retries": 2},
# ]


# # =============================================================================
# # STEP 3.  canonical -> a wire request, chosen by (generation, project).
# # =============================================================================
# # This is the crux (the 2D grid above). ONE function here for the walkthrough;
# # in the real module each branch becomes `Generation.build_request(canonical)`
# # with the generation carrying its own dictionaries.
# def build_request(canonical: QueryCanonical, generation: str) -> dict[str, Any]:
#     project = canonical.project[0]  # single-project example; real code loops

#     if generation == ESGF1:
#         # --- Solr wire format -------------------------------------------------
#         # For CMIP5, the Solr param names happen to line up with CMIP5's dialect
#         # names -- but that is a COINCIDENCE of this generation+project cell, so
#         # we still write the mapping out explicitly (it is the generation's own
#         # dictionary, not query/'s).
#         esgf1_cmip5_names = {
#             "model": "model",
#             "experiment": "experiment",
#             "variable": "variable",
#             "variant_label": "ensemble",
#             "reporting_interval": "time_frequency",
#             "processing_id": "cmor_table",
#             "realm": "realm",
#         }
#         params: dict[str, Any] = {
#             "project": project,
#             "format": "application/solr+json",
#             "limit": 10,
#             "retracted": "false",  # TODO: confirm the include-retracted mechanism
#         }
#         for canonical_name, wire_name in esgf1_cmip5_names.items():
#             values = getattr(canonical, canonical_name)
#             if values:
#                 # Solr combines multiple values by REPEATING the param.
#                 params[wire_name] = list(values)
#         # (query-specific facets like CMIP5 `product` would be added from
#         #  canonical.query_specific_facets here.)
#         return {"method": "GET", "path": "/esg-search/search", "params": params}

#     if generation in (ESGF_NG_EAST, ESGF_NG_WEST):
#         # --- STAC 1.0 + CQL2 wire format -------------------------------------
#         # Names are `cmipN:<wire_name>`. Collection id is UPPERCASE.
#         # For CMIP5 nothing is ingested, so this returns empty -- fine, we just
#         # want to prove the path builds and fires.
#         ng_cmip5_names = {
#             "model": "cmip5:model",
#             "experiment": "cmip5:experiment",
#             "variable": "cmip5:variable",
#             "variant_label": "cmip5:ensemble",
#             "reporting_interval": "cmip5:time_frequency",
#             "processing_id": "cmip5:cmor_table",
#             "realm": "cmip5:realm",
#         }
#         # STAC combines values as a CQL2 boolean tree (AND of per-facet ORs),
#         # not repeated params. Sketch of the structure:
#         and_clauses: list[dict[str, Any]] = [
#             {"op": "=", "args": [{"property": "collection"}, project.upper()]},
#             {"op": "=", "args": [{"property": "retracted"}, False]},
#         ]
#         for canonical_name, wire_name in ng_cmip5_names.items():
#             values = getattr(canonical, canonical_name)
#             if values:
#                 # facet matches ANY of its values -> `IN`
#                 and_clauses.append(
#                     {"op": "in", "args": [{"property": wire_name}, list(values)]}
#                 )
#         body = {
#             "filter-lang": "cql2-json",
#             "filter": {"op": "and", "args": and_clauses},
#         }
#         return {"method": "POST", "path": "/search", "body": body}

#     raise ValueError(f"unknown generation {generation!r}")


# # =============================================================================
# # STEP 4.  Fire one node, with its own retry budget.
# # =============================================================================
# def try_one_node(
#     node: dict[str, Any], request: dict[str, Any]
# ) -> dict[str, Any] | None:
#     # PSEUDOCODE -- real version uses httpx (sync, sequential) and is a direct dep
#     # we still need to ADD to pyproject.toml.
#     #
#     # url = f"https://{node['host']}{request['path']}"
#     # for attempt in range(node["retries"] + 1):
#     #     try:
#     #         if request["method"] == "GET":
#     #             resp = httpx.get(url, params=request["params"], timeout=...)
#     #         else:
#     #             resp = httpx.post(url, json=request["body"], timeout=...)
#     #         if resp.status_code >= 500:      # ESGF1 flaps 501/200 -> transient
#     #             continue                      # retry SAME node
#     #         resp.raise_for_status()
#     #         return resp.json()                # raw JSON, success
#     #     except (httpx.TransportError, httpx.HTTPStatusError):
#     #         continue                          # retry SAME node
#     # return None                               # node exhausted -> caller advances
#     raise NotImplementedError("walkthrough only")


# # =============================================================================
# # STEP 5.  The whole pipeline, hard-coded, returning raw JSON.
# # =============================================================================
# def search_cmip5_walkthrough() -> dict[str, Any]:
#     canonical = to_canonical_query(EXAMPLE)

#     # Selector policy (from the plan): walk NODE_PLAN in order. Each node gets its
#     # own retry budget inside try_one_node. A node that ERRORS out is exhausted
#     # and we ADVANCE. A node that returns an EMPTY result is a valid answer for
#     # this example -- but because we *expect* CMIP5 to only really live on ESGF1,
#     # the real selector's "advance on empty" rule is what would carry us past an
#     # empty ESGF-NG. For this first cut we just take the first NON-None response.
#     for node in NODE_PLAN:
#         request = build_request(canonical, node["generation"])
#         raw = try_one_node(node, request)
#         if raw is not None:
#             # throwaway result shape for now: {project: raw_json}
#             return {canonical.project[0]: raw}

#     raise RuntimeError("every node exhausted")


# # Open design threads intentionally left for discussion (see chat):
# #   - endpoint hostnames (east/west) + NCI host: CONFIRM.
# #   - "advance on empty vs first non-None": for the multi-project real workflow
# #     these differ; for this single CMIP5 example they mostly don't.
# #   - where the injectable end_point_selector(sub_query, attempt) seam goes once
# #     we lift NODE_PLAN out of this function.
