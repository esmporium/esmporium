"""
Writing search results into the database
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from esmporium.db.schema import (
    DATASET_FACET_COLUMNS,
    DATASET_IDENTITY_INDEX,
    DataNode,
    Dataset,
    DatasetRawDoc,
    DatasetVersion,
    DatasetVersionDataNodeLink,
    RawDocVersionLink,
)
from esmporium.search.result_parsing import (
    DatasetFacets,
    ParsedDocument,
    ResultProcessor,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


class UnhandledDatasetClashError(Exception):
    """
    Two datasets are identical over all `Dataset` columns except id_project_specific.

    The data is different in some facet(s) we do not model, and that difference is
    invisible to [`Dataset`][esmporium.db.schema.Dataset].

    For example, CMIP5 contains "product", a facet in the id_project_specific but
    one which we do not model. Two rows in [`Dataset`][esmporium.db.schema.Dataset]
    may be the same for all columns but differ by product in id_project_specific.
    In this case, the user must choose which row to keep.
    """

    def __init__(self, dataset: Dataset) -> None:
        self.dataset = dataset
        identity = {
            "id_project_specific": dataset.id_project_specific,
            **{column: getattr(dataset, column) for column in DATASET_FACET_COLUMNS},
        }
        super().__init__(
            "Two datasets are identical across every column our model records "
            f"({identity!r}), so our dataset model cannot tell them apart. "
            "This clash is not handled: the data differs in a facet we do not "
            "model. Please raise an issue at "
            "https://github.com/esmporium/esmporium/issues to discuss your use "
            "case, quoting the identity above. To find the differing facet "
            "exactly, flatten the clashing datasets' raw documents with "
            "esmporium.search.normalise_stored_document and compare them with "
            "esmporium.db.dataset_uniqueness.facet_differences, and quote that "
            "difference in the issue too."
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
    parsed_documents: Iterable[ParsedDocument],
) -> None:
    """
    Write already-parsed documents into the database

    Parameters
    ----------
    session
        The session to write into. This does NOT commit; the caller controls the
        transaction boundary (the processor from
        [build_result_processor][(m).build_result_processor] commits once per host).

    parsed_documents
        The documents a host answered with, already parsed by the facade's
        `parse_search_results`.
    """
    for parsed in parsed_documents:
        _ingest_document(session, parsed)


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

    # TODO: remove search_host?
    def processor(
        search_host: str, parsed_documents: tuple[ParsedDocument, ...]
    ) -> None:
        # `search_host` is part of the ResultProcessor callback contract but is no
        # longer persisted, so it is deliberately unused here.
        ingest_parsed_documents(session, parsed_documents)
        session.commit()

    return processor


class _SurrogateKeyRow(Protocol):
    """
    A row whose surrogate `id` is assigned by the database when the row is flushed.
    """

    id: int | None


def _flushed_id(row: _SurrogateKeyRow) -> int:
    """
    Get the database-assigned id of a row we have already flushed

    Tables declare `id: int | None`, following sqlalchemy's convention.
    However, in many cases, we need to make sure that we have an `int`, not `None`.
    This helps us do that narrowing consistently.
    """
    if row.id is None:
        msg = (
            f"{type(row).__name__} has no id, so it was never flushed into the "
            "session. Every row-creating helper here must flush before its id is used."
        )
        raise RuntimeError(msg)

    return row.id


def _ingest_document(session: Session, parsed: ParsedDocument) -> None:
    """Write one parsed document: its datasets, editions, nodes, raw doc and links."""
    datasets = [_get_or_create_dataset(session, facets) for facets in parsed.datasets]
    dataset_versions = [
        _upsert_version(session, _flushed_id(dataset), parsed) for dataset in datasets
    ]

    nodes = [_get_or_create_node(session, node.data_node) for node in parsed.nodes]
    for version in dataset_versions:
        for node in nodes:
            _get_or_create_version_node_link(
                session, _flushed_id(version), _flushed_id(node)
            )

    raw_doc = _get_or_create_raw_doc(session, parsed)
    for version in dataset_versions:
        _get_or_create_link(session, _flushed_id(raw_doc), _flushed_id(version))


def _get_or_create_dataset(session: Session, facets: DatasetFacets) -> Dataset:
    """Reuse an identical dataset if we have one, else save a new one.

    `facets` is the search layer's typed row; `model_dump()` turns it into `Dataset`
    kwargs. Building the `Dataset` here is also the loud boundary check: a field on
    `DatasetFacets` that `Dataset` does not accept fails here, not silently dropped.

    Matching on *every* facet (an equal `grid_label` NULL included) keeps re-ingestion
    idempotent without merging two datasets that differ on any single column.
    """
    facet_values = facets.model_dump()
    conditions = [
        getattr(Dataset, column) == value for column, value in facet_values.items()
    ]
    # We match on every column, so the identity index (see `Dataset.__table_args__`)
    # guarantees at most one row can satisfy this. `one_or_none` encodes exactly that
    # expectation: it returns the row or `None`, and raises `MultipleResultsFound` if a
    # second ever exists -- a corrupt or mis-migrated database then fails loudly
    existing = session.exec(select(Dataset).where(*conditions)).one_or_none()
    if existing is not None:
        return existing
    return save_dataset(session, Dataset(**facet_values))


def _upsert_version(
    session: Session, dataset_id: int, parsed: ParsedDocument
) -> DatasetVersion:
    """Insert this dataset's edition, or refresh its snapshot flags if seen before."""
    existing = session.exec(
        select(DatasetVersion).where(
            DatasetVersion.dataset_id == dataset_id,
            DatasetVersion.version == parsed.version,
        )
    ).one_or_none()
    if existing is not None:
        existing.is_latest = parsed.is_latest
        existing.retracted = parsed.retracted
        session.add(existing)
        session.flush()
        return existing

    version = DatasetVersion(
        dataset_id=dataset_id,
        version=parsed.version,
        is_latest=parsed.is_latest,
        retracted=parsed.retracted,
    )
    session.add(version)
    session.flush()
    return version


def _get_or_create_node(session: Session, data_node: str) -> DataNode:
    """Reuse the row for this data node if we have one, else create it."""
    existing = session.exec(
        select(DataNode).where(DataNode.data_node == data_node)
    ).one_or_none()
    if existing is not None:
        return existing

    node = DataNode(data_node=data_node)
    session.add(node)
    session.flush()
    return node


def _get_or_create_version_node_link(
    session: Session, dataset_version_id: int, data_node_id: int
) -> DatasetVersionDataNodeLink:
    """Link an edition to a data node, once."""
    existing = session.exec(
        select(DatasetVersionDataNodeLink).where(
            DatasetVersionDataNodeLink.dataset_version_id == dataset_version_id,
            DatasetVersionDataNodeLink.data_node_id == data_node_id,
        )
    ).one_or_none()
    if existing is not None:
        return existing

    link = DatasetVersionDataNodeLink(
        dataset_version_id=dataset_version_id, data_node_id=data_node_id
    )
    session.add(link)
    session.flush()
    return link


def _get_or_create_raw_doc(session: Session, parsed: ParsedDocument) -> DatasetRawDoc:
    """Store the raw JSON once, keyed by `esgf_doc_id`."""
    existing = session.exec(
        select(DatasetRawDoc).where(DatasetRawDoc.esgf_doc_id == parsed.esgf_doc_id)
    ).one_or_none()
    if existing is not None:
        return existing

    raw_doc = DatasetRawDoc(
        esgf_doc_id=parsed.esgf_doc_id,
        raw_json=parsed.raw_json,
        raw_docs_format_tag=parsed.raw_docs_format_tag,
    )
    session.add(raw_doc)
    session.flush()
    return raw_doc


def _get_or_create_link(
    session: Session, raw_doc_id: int, dataset_version_id: int
) -> RawDocVersionLink:
    """Link a raw document to an edition, once."""
    existing = session.exec(
        select(RawDocVersionLink).where(
            RawDocVersionLink.raw_doc_id == raw_doc_id,
            RawDocVersionLink.dataset_version_id == dataset_version_id,
        )
    ).one_or_none()
    if existing is not None:
        return existing

    link = RawDocVersionLink(
        raw_doc_id=raw_doc_id,
        dataset_version_id=dataset_version_id,
    )
    session.add(link)
    session.flush()
    return link
