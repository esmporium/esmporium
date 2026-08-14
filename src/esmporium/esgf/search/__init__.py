"""
Searching ESGF index nodes and returning their raw results.

A user's facet query (lowered to a
[`CanonicalQuery`][esmporium.esgf.canonical.CanonicalQuery]) is translated, per
search-API generation, into that API's own request and sent to index nodes. Two
generations are supported: ESGF1 (Solr) and ESGF-NG (STAC / CQL2). Each generation
owns its full translation of the query into API parameters — the query itself knows
nothing about API parameter names or encodings.
"""

from esmporium.esgf.search.client import CallResult, search_once
from esmporium.esgf.search.generation import (
    GENERATION_CONFIGS,
    GenerationConfig,
    SearchAPIGeneration,
    get_generation_config,
)
from esmporium.esgf.search.hosts import KNOWN_NODES, IndexNode
from esmporium.esgf.search.recorder import (
    FakeRecorder,
    NullRecorder,
    Recorder,
    SearchApiCallStat,
)
from esmporium.esgf.search.request import (
    DEFAULT_LIMIT,
    SearchRequest,
    UnrepresentableFacetError,
    build_request,
)
from esmporium.esgf.search.search import NoProjectToSearchError, search
from esmporium.esgf.search.selector import EndPointSelector, make_default_selector

__all__ = [
    "DEFAULT_LIMIT",
    "GENERATION_CONFIGS",
    "KNOWN_NODES",
    "CallResult",
    "EndPointSelector",
    "FakeRecorder",
    "GenerationConfig",
    "IndexNode",
    "NoProjectToSearchError",
    "NullRecorder",
    "Recorder",
    "SearchAPIGeneration",
    "SearchApiCallStat",
    "SearchRequest",
    "UnrepresentableFacetError",
    "build_request",
    "get_generation_config",
    "make_default_selector",
    "search",
    "search_once",
]
