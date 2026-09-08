"""
The parsed shape of a search result, and the processor that persists it

A search response is a bundle of per-dataset documents (Solr dataset records or
STAC features). Each document is parsed into a [`ParsedDocument`][(m).]: the bundle
it belongs to, the one-or-more dataset rows it covers, the edition, and where it is
hosted. This is the shape the search/facade layer produces and the `db` layer
consumes, so it lives here, below both -- the facade knows how to build it (it knows
the response format and the project), and `db` knows how to write it, without either
sniffing the other's concerns.

Why this lives in `search` and not `db`: `db` may import `search`, but `search` must
never import `db` (that would be a cycle). The facade now produces `ParsedDocument`s,
so the type has to sit at or below the search layer.

The "one document, many rows" shape is deliberately project-agnostic. A CMIP5 Solr
record bundles many variables and so yields many rows; a CMIP6/CMIP7 document yields
exactly one. Which is which is the reader's concern (see
[`esmporium.search.result_readers`][]), not this type's: here a document simply
carries the list of dataset rows it maps to.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class DataNodeInfo:
    """The data node one copy of a dataset version is hosted on.

    Only the data node (where the data lives) is kept. We deliberately do not carry the
    index node (which search index answered) or the replica flag: neither is stored --
    [`DatasetNodeInformation`][esmporium.db.schema.DatasetNodeInformation] records only
    the data node -- so reading them here would be carrying fields nothing consumes.
    """

    data_node: str


@dataclass(frozen=True)
class ParsedDocShell:
    """
    The pieces of a document that its response *format* alone determines

    These are read by the [`SearchAPI`][esmporium.search.apis.SearchAPI] (Solr vs
    STAC), which knows the envelope shape but nothing about which project's facet
    names to read. The facade combines this with the project-specific facet rows to
    build a full [`ParsedDocument`][(m).].
    """

    id_project_specific: str
    version: str
    is_latest: bool
    retracted: bool
    nodes: tuple[DataNodeInfo, ...]
    esgf_doc_id: str
    raw_json: str


@dataclass(frozen=True)
class ParsedDocument:
    """One raw search document, reduced to the pieces we store."""

    id_project_specific: str
    """The bundle's native id (Solr `master_id`, or the version-free STAC id)."""

    datasets: tuple[dict[str, str | None], ...]
    """
    The dataset rows this document maps to, one full facet dict each

    Each dict is the non-id [`Dataset`][esmporium.db.schema.Dataset] facet columns
    (`project`, `model`, ..., `variable`). A CMIP5 document yields one row per variable
    in its bundle; a CMIP6/CMIP7 document yields exactly one. The `id_project_specific`
    shared by every row is kept once, on this object, and folded back in by
    [`dataset_facets`][(c).dataset_facets].
    """

    version: str
    is_latest: bool
    retracted: bool
    nodes: tuple[DataNodeInfo, ...]
    esgf_doc_id: str
    raw_json: str

    search_api_tag: str
    """
    Names the format of `raw_json`, taken from the producing
    [`SearchAPI`][esmporium.search.apis.SearchAPI.search_api_tag]

    Stored on the raw-doc row so the right flattener can normalise it at load time
    (see [`esmporium.search.normalise_stored_document`][]).
    """

    def dataset_facets(self) -> list[dict[str, str | None]]:
        """Return the full [`Dataset`][esmporium.db.schema.Dataset] kwargs per row."""
        return [
            {**row, "id_project_specific": self.id_project_specific}
            for row in self.datasets
        ]


ResultProcessor = Callable[[str, tuple[ParsedDocument, ...]], None]
"""
A callback that processes the parsed results one host answered with

Called as `processor(search_host, parsed_documents)`. The search layer types its
injection seam against this without importing `db`; the `db` layer supplies a concrete
processor (see [`esmporium.db.build_result_processor`][]) bound to a session.
"""
