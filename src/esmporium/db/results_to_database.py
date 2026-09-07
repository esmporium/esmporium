"""
Writing search results into the database

This is where parsed search documents become rows. The parsing -- turning a Solr
record or STAC feature into a
[`ParsedDocument`][esmporium.search.result_parsing.ParsedDocument] -- now happens in
the search/facade layer, so this module is format-agnostic: it only knows how to write
`Dataset`, edition, node, raw-document and link rows from a `ParsedDocument`.
`ingest_parsed_documents` is the single write entry point; `build_result_processor`
wraps it as the callback [`esmporium.search.search`][] invokes as each host answers.
`save_dataset` is the safe single-row add underneath, turning the database's "these two
rows are identical" complaint into a clear error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from esmporium.db.schema import (
    DATASET_IDENTITY_INDEX,
    Dataset,
    DatasetNodeInformation,
    DatasetRawDoc,
    DatasetVersionSpecific,
    RawDocVersionLink,
)
from esmporium.search.result_parsing import ParsedDocument, ResultProcessor

if TYPE_CHECKING:
    from collections.abc import Iterable

    from esmporium.search.result_parsing import NodeInfo


class UnhandledDatasetClashError(Exception):
    """
    Two datasets are identical in our model, so we cannot tell them apart

    This means the data really is different in some facet we do not model, and that
    difference is invisible to [`Dataset`][esmporium.db.schema.Dataset]. It is a signal
    that our model is missing something for this data, not a duplicate to be dropped
    silently. Compare the underlying raw documents with
    [`esmporium.db.dataset_uniqueness.facet_differences`][] to see what differs.
    """

    def __init__(self, dataset: Dataset) -> None:
        self.dataset = dataset
        super().__init__(
            "Two datasets are identical across every column our model records "
            f"(id_project_specific={dataset.id_project_specific!r}, "
            f"variable={dataset.variable!r}), so our dataset model cannot tell them "
            "apart. This clash is not handled: the data differs in a facet we do not "
            "model. Inspect the raw documents with "
            "esmporium.db.dataset_uniqueness.facet_differences to find the difference."
        )


def save_dataset(session: Session, dataset: Dataset) -> Dataset:
    """
    Add a dataset, turning an identity clash into a clear error

    The add is flushed inside a savepoint so the clash surfaces here, at the call
    site, rather than at a later `commit` far from the dataset that caused it. On a
    clash the savepoint is rolled back, so `session` stays usable and any other work
    already staged in it is left intact; the caller still controls the outer
    transaction (nothing is committed here).

    Parameters
    ----------
    session
        The session to add the dataset to

    dataset
        The dataset to add

    Returns
    -------
    :
        The same `dataset`, now flushed into `session`

    Raises
    ------
    UnhandledDatasetClashError
        `dataset` is identical, in every column our model records, to one already
        stored (see [`Dataset`][esmporium.db.schema.Dataset]'s identity index).
    """
    # The savepoint is opened *before* the add so that rolling it back on a clash also
    # expunges the pending dataset; otherwise it would linger and be retried on the next
    # flush, resurfacing as a confusing error against an unrelated dataset.
    savepoint = session.begin_nested()
    session.add(dataset)
    try:
        session.flush()
    except IntegrityError as exc:
        savepoint.rollback()
        if DATASET_IDENTITY_INDEX in str(exc.orig):
            raise UnhandledDatasetClashError(dataset) from exc
        raise
    else:
        savepoint.commit()

    return dataset


def ingest_parsed_documents(
    session: Session,
    search_host: str,
    parsed_documents: Iterable[ParsedDocument],
) -> None:
    """
    Write already-parsed documents from one host into the database

    Parameters
    ----------
    session
        The session to write into. This does NOT commit; the caller controls the
        transaction boundary (the processor from
        [build_result_processor][(m).build_result_processor] commits once per host).

    search_host
        The endpoint the documents came from, recorded on each raw document.

    parsed_documents
        The documents that host answered with, already parsed by the facade's
        `parse_search_results`.
    """
    for parsed in parsed_documents:
        _ingest_document(session, parsed, search_host)


def build_result_processor(session: Session) -> ResultProcessor:
    """
    Build a processor that persists one host's parsed results into `session`

    The returned callback is what [`esmporium.search.search`][] calls as each host
    answers: it ingests that host's documents and commits, so results are durable as
    soon as they arrive. Inject it as
    `search(..., processor=build_result_processor(session))`.

    Parameters
    ----------
    session
        The session the processor writes and commits into.

    Returns
    -------
    :
        A callback of the shape
        [`ResultProcessor`][esmporium.search.result_parsing.ResultProcessor].
    """

    def processor(
        search_host: str, parsed_documents: tuple[ParsedDocument, ...]
    ) -> None:
        ingest_parsed_documents(session, search_host, parsed_documents)
        session.commit()

    return processor


def _ingest_document(
    session: Session, parsed: ParsedDocument, search_host: str
) -> None:
    """Write one parsed document: its datasets, edition, nodes, raw doc and link."""
    for facets in parsed.dataset_facets():
        _get_or_create_dataset(session, facets)

    version = _upsert_version(session, parsed)
    for node in parsed.nodes:
        _upsert_node(session, version.version_id, node)

    raw_doc = _get_or_create_raw_doc(session, parsed, search_host)
    _get_or_create_link(session, raw_doc.id, version.version_id)


def _get_or_create_dataset(session: Session, facets: dict[str, str | None]) -> Dataset:
    """Reuse an identical dataset if we have one, else save a new one.

    Matching on *every* facet (an equal `grid_label` NULL included) keeps re-ingestion
    idempotent without merging two datasets that differ on any single column.
    """
    conditions = [getattr(Dataset, column) == value for column, value in facets.items()]
    existing = session.exec(select(Dataset).where(*conditions)).first()
    if existing is not None:
        return existing
    return save_dataset(session, Dataset(**facets))


def _upsert_version(session: Session, parsed: ParsedDocument) -> DatasetVersionSpecific:
    """Insert the edition, or refresh its snapshot flags if seen before."""
    version_id = f"{parsed.id_project_specific}.v{parsed.version}"
    existing = session.get(DatasetVersionSpecific, version_id)
    if existing is not None:
        existing.is_latest = parsed.is_latest
        existing.retracted = parsed.retracted
        session.add(existing)
        session.flush()
        return existing

    version = DatasetVersionSpecific(
        version_id=version_id,
        id_project_specific=parsed.id_project_specific,
        version=parsed.version,
        is_latest=parsed.is_latest,
        retracted=parsed.retracted,
    )
    session.add(version)
    session.flush()
    return version


def _upsert_node(
    session: Session, version_id: str, node: NodeInfo
) -> DatasetNodeInformation:
    """Insert an (edition, data node) row, or refresh it if seen before."""
    existing = session.exec(
        select(DatasetNodeInformation).where(
            DatasetNodeInformation.version_id == version_id,
            DatasetNodeInformation.data_node == node.data_node,
        )
    ).first()
    if existing is not None:
        existing.index_node = node.index_node
        existing.replica = node.replica
        session.add(existing)
        session.flush()
        return existing

    row = DatasetNodeInformation(
        version_id=version_id,
        data_node=node.data_node,
        index_node=node.index_node,
        replica=node.replica,
    )
    session.add(row)
    session.flush()
    return row


def _get_or_create_raw_doc(
    session: Session, parsed: ParsedDocument, search_host: str
) -> DatasetRawDoc:
    """Store the raw JSON once, keyed by `esgf_doc_id`."""
    existing = session.exec(
        select(DatasetRawDoc).where(DatasetRawDoc.esgf_doc_id == parsed.esgf_doc_id)
    ).first()
    if existing is not None:
        return existing

    raw_doc = DatasetRawDoc(
        esgf_doc_id=parsed.esgf_doc_id,
        search_host=search_host,
        raw_json=parsed.raw_json,
    )
    session.add(raw_doc)
    session.flush()
    return raw_doc


def _get_or_create_link(
    session: Session, raw_id: int | None, version_id: str
) -> RawDocVersionLink:
    """Link a raw document to an edition, once."""
    existing = session.exec(
        select(RawDocVersionLink).where(
            RawDocVersionLink.raw_id == raw_id,
            RawDocVersionLink.version_id == version_id,
        )
    ).first()
    if existing is not None:
        return existing

    link = RawDocVersionLink(raw_id=raw_id, version_id=version_id)
    session.add(link)
    session.flush()
    return link
