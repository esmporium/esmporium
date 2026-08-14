"""
Index nodes: a host paired with the search-API generation it serves.

An [`IndexNode`][esmporium.esgf.search.hosts.IndexNode] is the unit the search
layer fans out over — a host plus the
[`SearchAPIGeneration`][esmporium.esgf.search.generation.SearchAPIGeneration] that
host serves, which is all we need to build the right request and reach it.
[`KNOWN_NODES`][esmporium.esgf.search.hosts.KNOWN_NODES] is a starting set of
public nodes; a user searches their own by constructing an `IndexNode` (host +
generation) and passing it to the search entry point.
"""

from pydantic import BaseModel

from esmporium.esgf.search.generation import SearchAPIGeneration


class IndexNode(BaseModel):
    """
    A single ESGF index node to search: a host and the generation it speaks.

    Immutable: a node is a plain value identifying *where* to search and *how* to
    talk to it. The user must supply the generation for their own hosts, because it
    is what selects the query-to-request translation.
    """

    model_config = {"frozen": True}

    host: str
    """The node host, without scheme or path (e.g. ``api.stac.esgf.ceda.ac.uk``)."""

    generation: SearchAPIGeneration
    """Which search-API generation this host serves."""


KNOWN_NODES: tuple[IndexNode, ...] = (
    IndexNode(
        host="api.stac.esgf.ceda.ac.uk",
        generation=SearchAPIGeneration.ESGF_NG_EAST,
    ),
    IndexNode(
        host="discovery.west.esgf.io",
        generation=SearchAPIGeneration.ESGF_NG_WEST,
    ),
    IndexNode(
        host="esgf.ceda.ac.uk",
        generation=SearchAPIGeneration.ESGF1,
    ),
    IndexNode(
        host="esgf-node.ornl.gov",
        generation=SearchAPIGeneration.ESGF1,
    ),
)
"""
A starting set of public ESGF index nodes and the generation each serves.

The two ESGF-NG (STAC) nodes are live. The ESGF1 (Solr) nodes are included for when
their esg-search endpoints return: as of 2026-08 they respond ``501 Not
Implemented`` across the federation, which the search layer handles as any other
failing node (retry, then advance to the next). Not ranked; not exhaustive — users
extend this by passing their own nodes.
"""
