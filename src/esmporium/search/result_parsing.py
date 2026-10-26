"""
The shape result parsers must produce for the `db` layer to store

This is project and search API agnostic. The known result
parsers and translations are defined in
[known_result_parsers][esmporium.search.search_api_facade.result_parsers.known_result_parsers].
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict

from esmporium.datasets.data_access import AccessService, ContainerFormat

if TYPE_CHECKING:
    from esmporium.search.search_api_facade import SearchAPIFacade


class DatasetFacets(BaseModel):
    """
    The facets required for a single unique dataset row.

    Its fields mirror the facet columns in [Dataset][esmporium.db.schema.Dataset]
    """

    model_config = ConfigDict(extra="forbid")

    id_project_specific: str
    """See [Dataset.id_project_specific][esmporium.db.schema.Dataset.id_project_specific]."""  # noqa: E501

    project: str
    """See [Dataset.project][esmporium.db.schema.Dataset.project]."""

    model: str
    """See [Dataset.model][esmporium.db.schema.Dataset.model]."""

    institution: str
    """See [Dataset.institution][esmporium.db.schema.Dataset.institution]."""

    experiment: str
    """See [Dataset.experiment][esmporium.db.schema.Dataset.experiment]."""

    variant_label: str
    """See [Dataset.variant_label][esmporium.db.schema.Dataset.variant_label]."""

    variable: str
    """See [Dataset.variable][esmporium.db.schema.Dataset.variable]."""

    reporting_interval: str
    """See [Dataset.reporting_interval][esmporium.db.schema.Dataset.reporting_interval]."""  # noqa: E501

    grid_label: str | None
    """
    See [Dataset.grid_label][esmporium.db.schema.Dataset.grid_label].

    Required with no default (unlike the column it maps to): a parser must state the
    value, passing `None` for a project with no grid concept (CMIP5). That makes "this
    data has no grid" an explicit decision at parse time rather than a silent omission,
    so a reader that simply forgets `grid_label` fails here in `search` rather than
    quietly storing `NULL`.
    """

    processing_id: str
    """See [Dataset.processing_id][esmporium.db.schema.Dataset.processing_id]."""


@dataclass(frozen=True)
class DataNodeInfo:
    """The data node one copy of a dataset version is hosted on."""

    data_node: str


@dataclass(frozen=True)
class FileInfo:
    """
    One file a search document described

    The search-layer mirror of [File][esmporium.db.schema.File]. Node-independent:
    every field here describes the bytes, not where they are, so two nodes serving
    the same file describe it identically.
    """

    filename: str
    """See [File.filename][esmporium.db.schema.File.filename]."""

    size: int | None
    """See [File.size][esmporium.db.schema.File.size]."""

    checksum: str | None
    """See [File.checksum][esmporium.db.schema.File.checksum]."""

    checksum_type: str | None
    """See [File.checksum_type][esmporium.db.schema.File.checksum_type]."""

    tracking_id: str | None
    """See [File.tracking_id][esmporium.db.schema.File.tracking_id]."""

    local_path: str | None
    """See [File.local_path][esmporium.db.schema.File.local_path]."""


@dataclass(frozen=True)
class DataAccessInfo:
    """
    One route to bytes a search document described

    The search-layer mirror of [DataAccess][esmporium.db.schema.DataAccess].
    """

    filename: str | None
    """
    Which of the document's files this route serves, or `None` for all of them

    A name rather than an object or an id, because the parse layer has no database
    ids and a route refers to its file by name in both APIs anyway. `None` means the
    same thing [DataAccess.file_id][esmporium.db.schema.DataAccess.file_id] being
    `NULL` means: this route serves the whole dataset version.
    """

    data_node: str
    """See [DataNode.data_node][esmporium.db.schema.DataNode.data_node]."""

    service: AccessService
    """See [DataAccess.service][esmporium.db.schema.DataAccess.service]."""

    container_format: ContainerFormat
    """
    See [DataAccess.container_format][esmporium.db.schema.DataAccess.container_format].
    """

    url: str
    """See [DataAccess.url][esmporium.db.schema.DataAccess.url]."""

    raw_json: str
    """See [DataAccess.raw_json][esmporium.db.schema.DataAccess.raw_json]."""


@dataclass(frozen=True)
class DataAccessPayload:
    """
    Every file and route one search document described, for one dataset version

    The two travel together because a route refers to a file by name, so splitting
    them would let a caller hold half of a pair.

    Both empty is the honest answer for a document that says nothing about files,
    which is every Solr *dataset* document: ESGF-1 publishes no per-file URLs at
    dataset level, so those are filled in by a separate `type=File` request (see
    [esmporium.db.record_solr_file_access][]). ESGF-NG STAC carries its files in the
    item's `assets`, so its parser fills these in with no extra request.
    """

    files: tuple[FileInfo, ...] = ()
    """The files the document described."""

    accesses: tuple[DataAccessInfo, ...] = ()
    """
    The routes the document described

    Each names one of `files`, or the whole version with `filename=None`.
    """


@dataclass(frozen=True)
class ParsedDocument:
    """One raw search document, reduced to the pieces we store."""

    id_project_specific: str
    """See [Dataset.id_project_specific][esmporium.db.schema.Dataset.id_project_specific]."""  # noqa: E501

    datasets: tuple[DatasetFacets, ...]
    """
    The complete dataset rows this document maps to

    Each is a [DatasetFacets][(m).DatasetFacets], ready to
    become a [Dataset][esmporium.db.schema.Dataset]. A CMIP5
    document yields one row per variable for Solr, and a CMIP6/CMIP7 document
    yields exactly one row for both Solr and STAC.
    """

    version: str
    """See [DatasetVersion.version][esmporium.db.schema.DatasetVersion.version]."""

    is_latest: bool
    """See [DatasetVersion.is_latest][esmporium.db.schema.DatasetVersion.is_latest]."""

    retracted: bool
    """See [DatasetVersion.retracted][esmporium.db.schema.DatasetVersion.retracted]."""

    nodes: tuple[DataNodeInfo, ...]
    """See [DataNode][esmporium.db.schema.DataNode]."""

    esgf_doc_id: str
    """See [DatasetRawDoc.esgf_doc_id][esmporium.db.schema.DatasetRawDoc.esgf_doc_id]."""  # noqa: E501

    raw_json: str
    """See [DatasetRawDoc.raw_json][esmporium.db.schema.DatasetRawDoc.raw_json]."""

    raw_docs_format_tag: str
    """See [SearchAPI][esmporium.search.apis.SearchAPI.raw_docs_format_tag]"""

    files: tuple[FileInfo, ...] = ()
    """
    The files this document described, if it described any

    Empty for every Solr dataset document, because Solr publishes no per-file
    information at dataset level; those arrive through
    [esmporium.db.record_solr_file_access][] instead. ESGF-NG STAC carries them in
    the item's `assets`, so its parser fills these in for free.

    **These describe the single dataset version this document is about.** A parser
    whose document maps to more than one dataset row (a CMIP5 Solr bundle, one row
    per variable) has nothing that says which row a file belongs to, so it must leave
    these empty; ingesting a multi-row document that does not raises
    [AmbiguousDataAccessError][esmporium.db.AmbiguousDataAccessError].
    """

    accesses: tuple[DataAccessInfo, ...] = ()
    """
    The routes to bytes this document described, if it described any

    Each names one of `files`, or the whole version with `filename=None`. Same
    attribution rule as `files`.
    """


# Note: this shape will likely need to change once we want to link Datasets
# and searches/query collections in our database.
# (No need to change anything now though,
# let's deal with this change when we need it in PR4)
class ResultProcessor(Protocol):
    """
    A callback that processes the parsed results one host answered with

    The database layer supplies a concrete processor
    (see [esmporium.db.build_result_processor][]).
    """

    def __call__(
        self, facade: SearchAPIFacade, parsed_documents: tuple[ParsedDocument, ...]
    ) -> None:
        """
        Process the results that come from a given facade
        """
