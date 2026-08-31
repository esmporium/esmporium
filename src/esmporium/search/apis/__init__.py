"""
Direct search API interaction support

This is low-level, intended to mirror the ESGF search APIs directly.
It is extremely easy to make invalid queries using these pieces.
If you want to make queries, we recommend using the components in
[esmporium.search.search_api_facade][] instead
because of their more robust query creation, result parsing and error handling.
We provide these interfaces in case you want or need to use them directly,
without the constraints provided by our higher-level interfaces.
"""

from esmporium.search.apis.esgf1 import SearchAPIESGF1Solr
from esmporium.search.apis.esgf15_bridge import SearchAPIESGF15BridgeSolr
from esmporium.search.apis.esgfng import SearchAPIESGFNGSTAC
from esmporium.search.apis.protocol import SearchAPI
from esmporium.search.apis.request import Request

__all__ = [
    "Request",
    "SearchAPI",
    "SearchAPIESGF1Solr",
    "SearchAPIESGF15BridgeSolr",
    "SearchAPIESGFNGSTAC",
]
