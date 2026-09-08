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
[`esmporium.search.search_api_facade.SearchAPIFacade.read_dataset_rows`][]), not this
type's: here a document simply carries the list of dataset rows it maps to.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict


class DatasetFacets(BaseModel):
    """
    One complete, savable dataset row: the identity plus every facet we model

    This is the typed shape
    [`SearchAPIFacade.read_dataset_rows`][esmporium.search.search_api_facade.SearchAPIFacade.read_dataset_rows]
    produces and the `db` layer promotes into a
    [`Dataset`][esmporium.db.schema.Dataset] row (`Dataset(**facets.model_dump())`).
    Its fields mirror `Dataset`'s facet columns plus `id_project_specific`.

    Keeping this a real model, rather than a bare dict, moves the coupling with
    `Dataset` from "hope the dict keys line up" to something checked in two places: a
    field-parity test pins these fields to `Dataset`'s columns, and `db` validating each
    row into a `Dataset` at ingest is the loud runtime backstop. Because the fields are
    required (bar `grid_label`, which CMIP5 has no value for), a facet the reader forgot
    to produce fails here at construction, in `search`, instead of as a far-away NOT
    NULL error at commit time. `extra="forbid"` means an unexpected facet is rejected too.
    """  # noqa: E501

    model_config = ConfigDict(extra="forbid")

    id_project_specific: str
    """See [`Dataset.id_project_specific`][esmporium.db.schema.Dataset.id_project_specific]."""  # noqa: E501

    project: str
    """See [`Dataset.project`][esmporium.db.schema.Dataset.project]."""

    model: str
    """See [`Dataset.model`][esmporium.db.schema.Dataset.model]."""

    institution: str
    """See [`Dataset.institution`][esmporium.db.schema.Dataset.institution]."""

    experiment: str
    """See [`Dataset.experiment`][esmporium.db.schema.Dataset.experiment]."""

    variant_label: str
    """See [`Dataset.variant_label`][esmporium.db.schema.Dataset.variant_label]."""

    variable: str
    """See [`Dataset.variable`][esmporium.db.schema.Dataset.variable]."""

    reporting_interval: str
    """See [`Dataset.reporting_interval`][esmporium.db.schema.Dataset.reporting_interval]."""  # noqa: E501

    grid_label: str | None = None
    """See [`Dataset.grid_label`][esmporium.db.schema.Dataset.grid_label]. CMIP5 has none."""  # noqa: E501

    processing_id: str
    """See [`Dataset.processing_id`][esmporium.db.schema.Dataset.processing_id]."""


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
    """
    The bundle's native id (Solr `master_id`, or the version-free STAC id)

    Kept at the document level as the bundle's identity. Every row in `datasets`
    also carries this same value (a row is a complete, savable
    [`DatasetFacets`][(m).DatasetFacets]); both come from the one format shell, so they
    cannot disagree.
    """

    datasets: tuple[DatasetFacets, ...]
    """
    The complete dataset rows this document maps to

    Each is a [`DatasetFacets`][(m).DatasetFacets] -- every facet column plus
    `id_project_specific`, ready to become a [`Dataset`][esmporium.db.schema.Dataset]. A
    CMIP5 document yields one row per variable in its bundle; a CMIP6/CMIP7 document
    yields exactly one.
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


ResultProcessor = Callable[[str, tuple[ParsedDocument, ...]], None]
"""
A callback that processes the parsed results one host answered with

Called as `processor(search_host, parsed_documents)`. The search layer types its
injection seam against this without importing `db`; the `db` layer supplies a concrete
processor (see [`esmporium.db.build_result_processor`][]) bound to a session.
"""
