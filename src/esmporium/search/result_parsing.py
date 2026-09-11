"""
The shape result parsers must produce for the `db` layer to store

This is project and search API agnostic. The known result
parsers and translations are defined in
[esmporium.search.search_api_facade.result_parsers][known_result_parsers]
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict


class DatasetFacets(BaseModel):
    """
    The facets required for a single unique dataset row.

    Its fields mirror the facet columns in [`Dataset`][esmporium.db.schema.Dataset]
    """

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

    grid_label: str | None
    """
    See [`Dataset.grid_label`][esmporium.db.schema.Dataset.grid_label].

    Required with no default (unlike the column it maps to): a parser must state the
    value, passing `None` for a project with no grid concept (CMIP5). That makes "this
    data has no grid" an explicit decision at parse time rather than a silent omission,
    so a reader that simply forgets `grid_label` fails here in `search` rather than
    quietly storing `NULL`.
    """

    processing_id: str
    """See [`Dataset.processing_id`][esmporium.db.schema.Dataset.processing_id]."""


@dataclass(frozen=True)
class DataNodeInfo:
    """The data node one copy of a dataset version is hosted on."""

    data_node: str


@dataclass(frozen=True)
class ParsedDocument:
    """One raw search document, reduced to the pieces we store."""

    id_project_specific: str
    """See [`Dataset.id_project_specific`][esmporium.db.schema.Dataset.id_project_specific]."""  # noqa: E501

    datasets: tuple[DatasetFacets, ...]
    """
    The complete dataset rows this document maps to

    Each is a [`DatasetFacets`][(m).DatasetFacets], ready to
    become a [`Dataset`][esmporium.db.schema.Dataset]. A CMIP5
    document yields one row per variable for Solr, and a CMIP6/CMIP7 document
    yields exactly one row for both Solr and STAC.
    """

    version: str
    """See [`DatasetVersion.version`][esmporium.db.schema.DatasetVersion.version]."""

    is_latest: bool
    """See [`DatasetVersion.is_latest`][esmporium.db.schema.DatasetVersion.is_latest]."""  # noqa: E501

    retracted: bool
    """See [`DatasetVersion.retracted`][esmporium.db.schema.DatasetVersion.retracted]."""  # noqa: E501

    nodes: tuple[DataNodeInfo, ...]
    """See [`DataNode`][esmporium.db.schema.DataNode]."""

    esgf_doc_id: str
    """See [`DatasetRawDoc.esgf_doc_id`][esmporium.db.schema.DatasetRawDoc.esgf_doc_id]."""  # noqa: E501

    raw_json: str
    """See [`DatasetRawDoc.raw_json`][esmporium.db.schema.DatasetRawDoc.raw_json]."""

    raw_docs_format_tag: str
    """See [`SearchAPI`][esmporium.search.apis.SearchAPI.raw_docs_format_tag]"""


# Note: this shape will likely need to change once we want to link Datasets
# and searches/query collections in our database.
# (No need to change anything now though,
# let's deal with this change when we need it in PR4)
ResultProcessor = Callable[[str, tuple[ParsedDocument, ...]], None]
"""
A callback that processes the parsed results one host answered with

Called as `processor(search_host, parsed_documents)`. The search layer types its
injection seam against this without importing `db`; the `db` layer supplies a concrete
processor (see [`esmporium.db.build_result_processor`][]) bound to a session.
"""
