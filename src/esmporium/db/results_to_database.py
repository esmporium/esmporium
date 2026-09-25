"""
Writing search results into the database
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from sqlalchemy import Engine, func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from esmporium.db.dataset_uniqueness import facet_differences
from esmporium.db.engine import is_sqlite_configured_for_concurrency
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
from esmporium.search.result_normalisation import (
    DEFAULT_NORMALISERS,
    NormaliseFunc,
    UnknownRawDocFormatTagError,
    normalise_stored_document,
)
from esmporium.search.result_parsing import (
    DatasetFacets,
    ParsedDocument,
    ResultProcessor,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping
    from contextlib import AbstractContextManager

    from esmporium.search.search_api_facade import SearchAPIFacade


class UnhandledDatasetClashError(Exception):
    """
    Two datasets are identical over every column, including id_project_specific

    We did not expect data of this shape.
    The data is presumably different in some facet(s) we do not model,
    and which also do not appear in id_project_specific,
    so that difference is invisible to [`Dataset`][esmporium.db.schema.Dataset].

    For example, CORDEX data could differ by driving climate model,
    without the driving climate model appearing in id_project_specific.
    To help find the facet our model is missing,
    `differences` holds the facets
    that differ between the raw documents of the clashing datasets (if we have them).
    """

    def __init__(
        self,
        dataset: Dataset,
        differences: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """
        Initialise the error

        Parameters
        ----------
        dataset
            The dataset that could not be saved

        differences
            The facets that differ between the raw documents
            of the stored dataset and `dataset`,
            as returned by [`facet_differences`][esmporium.db.facet_differences].

            `None` if these could not be worked out
            (e.g. no raw document was supplied for `dataset`).
        """
        self.dataset = dataset
        self.differences = differences
        identity = {
            "id_project_specific": dataset.id_project_specific,
            **{column: getattr(dataset, column) for column in DATASET_FACET_COLUMNS},
        }
        if differences is None:
            differences_msg = (
                "To find the differing facet(s), "
                "flatten the clashing datasets' raw documents with "
                "esmporium.search.normalise_stored_document "
                "and compare them with esmporium.db.facet_differences "
                "(save_dataset does this for you if you pass it `raw_doc`), "
                "and quote that difference in the issue too."
            )
        elif not differences:
            differences_msg = (
                "The raw documents of the clashing datasets agree on every facet, "
                "so they do not explain the clash either."
            )
        else:
            differences_msg = (
                "These facets differ between the raw documents "
                "of the clashing datasets "
                "(one of them is likely the facet our model is missing), "
                f"please quote them in the issue too: {differences!r}"
            )

        super().__init__(
            "Two datasets are identical across every column our model records "
            f"({identity!r}), so our dataset model cannot tell them apart. "
            "This clash is not handled: "
            "the data presumably differs in a facet we do not model. "
            "Please raise an issue at https://github.com/esmporium/esmporium/issues "
            "to discuss your use case, quoting the identity above. "
            f"{differences_msg}"
        )


class UnconfiguredSQLiteEngineError(Exception):
    """
    Results were about to be saved into a SQLite engine that has not been configured

    See [`esmporium.db.configure_sqlite_for_concurrency`][]
    for why this is needed.
    """


def save_dataset(
    session: Session,
    dataset: Dataset,
    raw_doc: DatasetRawDoc | None = None,
    normalisers: Mapping[str, NormaliseFunc] = DEFAULT_NORMALISERS,
) -> Dataset:
    """
    Add a dataset, turning an identity clash into a clear error

    Parameters
    ----------
    session
        The session to add the dataset to

    dataset
        The dataset to add

    raw_doc
        The raw document `dataset` was parsed from.

        This is not saved.
        It is only used to diagnose a clash, see `Raises`.

    normalisers
        Normalisers to use when diagnosing a clash,
        see [`esmporium.search.normalise_stored_document`][].

    Returns
    -------
    :
        The same `dataset`, now flushed into `session`

    Raises
    ------
    UnhandledDatasetClashError
        `dataset` is identical, in every column our model records, to one already
        stored (see [`Dataset`][esmporium.db.schema.Dataset]'s identity index).

        If `raw_doc` is supplied and the stored dataset has raw documents,
        the error's `differences` holds the facets that differ between them.

    UnconfiguredSQLiteEngineError
        `session` is bound to a SQLite engine that has not been configured with
        [`esmporium.db.configure_sqlite_for_concurrency`][].
    """
    _check_engine_is_configured(session)

    return _get_or_create(
        session,
        # Nothing to reuse: saving a dataset that is already stored is the clash.
        lookup=lambda: None,
        build=lambda: dataset,
        on_unresolved=lambda row, exc: _raise_if_dataset_clash(
            session, row, exc, raw_doc, normalisers
        ),
    )


def _check_engine_is_configured(session: Session) -> None:
    """
    Raise if `session` writes to a SQLite engine without real transactions

    Saving uses savepoints,
    which only behave on SQLite once the engine is configured
    (see [`esmporium.db.configure_sqlite_for_concurrency`][]).
    Without that, rows would be committed one by one as they are written,
    so we fail loudly instead.
    """
    bind = session.get_bind()
    engine = bind if isinstance(bind, Engine) else bind.engine
    if engine.dialect.name != "sqlite" or is_sqlite_configured_for_concurrency(engine):
        return

    msg = (
        f"{engine!r} is a SQLite engine "
        "that has not been configured for saving results. "
        "Without configuration, SQLite commits each row as it is written, "
        "so a failed save would leave partial results behind. "
        "Call esmporium.db.configure_sqlite_for_concurrency on the engine "
        "straight after creating it, before it is first used."
    )
    raise UnconfiguredSQLiteEngineError(msg)


def _raise_if_dataset_clash(
    session: Session,
    dataset: Dataset,
    exc: IntegrityError,
    raw_doc: DatasetRawDoc | None,
    normalisers: Mapping[str, NormaliseFunc],
) -> None:
    """
    Turn a violation of the dataset identity index into an `UnhandledDatasetClashError`

    Any other integrity error is left for the caller to re-raise.
    """
    if DATASET_IDENTITY_INDEX not in str(exc.orig):
        return

    differences = (
        None
        if raw_doc is None
        else _get_clash_facet_differences(session, dataset, raw_doc, normalisers)
    )
    raise UnhandledDatasetClashError(dataset, differences) from exc


def _get_clash_facet_differences(
    session: Session,
    dataset: Dataset,
    raw_doc: DatasetRawDoc,
    normalisers: Mapping[str, NormaliseFunc],
) -> dict[str, dict[str, Any]] | None:
    """
    Diff the normalised raw documents of a clashing dataset and the stored one

    Every raw document linked to the stored dataset is included
    (it may have several, e.g. one per version or data node),
    so facets like `version` or `data_node` may show up as noise too.

    Returns `None` if there is nothing to diff against
    (the stored dataset has no raw documents)
    or a raw document's format tag has no normaliser.
    Keys are `"stored: <esgf_doc_id>"` and `"new: <esgf_doc_id>"`.
    """
    # Find the stored dataset the same way the identity index does,
    # i.e. with `grid_label` coalesced, so NULL and '' match.
    identity_conditions = [
        getattr(Dataset, column) == getattr(dataset, column)
        for column in ("id_project_specific", *DATASET_FACET_COLUMNS)
        if column != "grid_label"
    ]
    identity_conditions.append(
        func.coalesce(col(Dataset.grid_label), "") == (dataset.grid_label or "")
    )
    stored_raw_docs = session.exec(
        select(DatasetRawDoc)
        .join(
            RawDocVersionLink,
            col(RawDocVersionLink.raw_doc_id) == col(DatasetRawDoc.id),
        )
        .join(
            DatasetVersion,
            col(DatasetVersion.id) == col(RawDocVersionLink.dataset_version_id),
        )
        .join(Dataset, col(Dataset.id) == col(DatasetVersion.dataset_id))
        .where(*identity_conditions)
        .distinct()
        .order_by(col(DatasetRawDoc.id))
    ).all()
    if not stored_raw_docs:
        return None

    labelled_raw_docs = [
        *((f"stored: {doc.esgf_doc_id}", doc) for doc in stored_raw_docs),
        (f"new: {raw_doc.esgf_doc_id}", raw_doc),
    ]
    try:
        normalised_info = tuple(
            (
                label,
                normalise_stored_document(
                    json.loads(doc.raw_json), doc.raw_docs_format_tag, normalisers
                ),
            )
            for label, doc in labelled_raw_docs
        )
    except UnknownRawDocFormatTagError:
        # This is only a diagnostic, so don't let it hide the clash itself.
        # The clash error explains how to do the diff by hand.
        return None

    return facet_differences(normalised_info)


def ingest_parsed_documents(
    session: Session,
    parsed_documents: Iterable[ParsedDocument],
    normalisers: Mapping[str, NormaliseFunc] = DEFAULT_NORMALISERS,
) -> None:
    """
    Write already-parsed documents into the database

    Parameters
    ----------
    session
        The session to write into.
        This does NOT commit; the caller controls the transaction boundary
        (the processor from [build_result_processor][(m).build_result_processor]
        commits once per page of results,
        so each page is saved all or nothing).

    parsed_documents
        A page of documents a host answered with, already parsed by the facade's
        `parse_search_results`.

    normalisers
        Normalisers to use if a dataset clash has to be diagnosed,
        see [save_dataset][(m).save_dataset].

        If you ingest documents from your own search API
        with its own `raw_docs_format_tag`,
        pass a mapping that includes a normaliser for that tag
        (e.g. `{**DEFAULT_NORMALISERS, <your tag>: <your normaliser>}`).

    Raises
    ------
    UnconfiguredSQLiteEngineError
        `session` is bound to a SQLite engine that has not been configured with
        [`esmporium.db.configure_sqlite_for_concurrency`][].
    """
    _check_engine_is_configured(session)
    for parsed in parsed_documents:
        _ingest_document(session, parsed, normalisers)


def build_result_processor(
    session: Session,
    normalisers: Mapping[str, NormaliseFunc] = DEFAULT_NORMALISERS,
) -> ResultProcessor:
    """
    Build a processor that persists one facade's parsed results into `session`

    The returned callback is what
    [`esmporium.search.search_single_project`][] calls
    with each page of results as it arrives:
    it ingests that page's documents and commits,
    so each page is saved all or nothing
    and is durable as soon as it arrives.
    Inject it as
    `search_single_project(..., processor=build_result_processor(session))`.
    For the multi-query [`esmporium.search.search`][], which wants a fresh processor per
    sub-query, use [build_result_processor_factory][(m).] instead.

    Parameters
    ----------
    session
        The session the processor writes and commits into.

    normalisers
        Normalisers to use if a dataset clash has to be diagnosed,
        see [save_dataset][(m).save_dataset].

        If you ingest documents from your own search API
        with its own `raw_docs_format_tag`,
        pass a mapping that includes a normaliser for that tag
        (e.g. `{**DEFAULT_NORMALISERS, <your tag>: <your normaliser>}`).

    Returns
    -------
    :
        A callback of the shape
        [`ResultProcessor`][esmporium.search.result_parsing.ResultProcessor].
    """

    def processor(
        facade: SearchAPIFacade, parsed_documents: tuple[ParsedDocument, ...]
    ) -> None:
        # `facade` is part of the ResultProcessor callback contract
        # but is currently not used here (that may change in future).
        ingest_parsed_documents(session, parsed_documents, normalisers)
        session.commit()

    return processor


def build_result_processor_factory(
    engine: Engine,
    normalisers: Mapping[str, NormaliseFunc] = DEFAULT_NORMALISERS,
) -> Callable[[], AbstractContextManager[ResultProcessor]]:
    """
    Build a factory that makes a fresh saving processor per sub-query

    This is the database-saving [ProcessorFactory][esmporium.search.ProcessorFactory] to
    hand to [`esmporium.search.search`][]: it is called once per sub-query and opens a
    fresh `sqlmodel.Session` (and so a fresh transaction) for that sub-query,
    yields a [build_result_processor][(m).] bound to it, and closes it afterwards.
    A session per sub-query keeps each sub-query's writes in transactions of its own
    (one per page, see [build_result_processor][(m).]),
    and lets a parallel search give each worker its own session.

    Parameters
    ----------
    engine
        The database engine each sub-query's session is opened on.

    normalisers
        Passed through to [build_result_processor][(m).] for each session.

    Returns
    -------
    :
        A factory of the shape [ProcessorFactory][esmporium.search.ProcessorFactory],
        i.e. a callable that returns a fresh saving processor as a context manager.

    Examples
    --------
    >>> from esmporium.search import search  # doctest: +SKIP
    >>> search(  # doctest: +SKIP
    ...     queries,
    ...     processor_factory=build_result_processor_factory(engine),
    ... )
    """

    @contextmanager
    def factory() -> Iterator[ResultProcessor]:
        with Session(engine) as session:
            yield build_result_processor(session, normalisers)

    return factory


class _SurrogateKeyRow(Protocol):
    """
    A row whose surrogate `id` is assigned by the database when the row is flushed.

    In this context, flushed means writing results to the database
    (effectively, it's a bit more complicated than this in reality,
    but this is near enough for our own mental model).
    This is the step that converts IDs set by the database from `None`
    to an actual value.

    This class represents a database row whose id value may be `int` or `None`,
    but we expect to become `int` only
    once the row has been written to the database (i.e. flushed).
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


def _ingest_document(
    session: Session,
    parsed: ParsedDocument,
    normalisers: Mapping[str, NormaliseFunc],
) -> None:
    """Write one parsed document: its datasets, versions, nodes, raw doc and links."""
    datasets = [
        _get_or_create_dataset(session, facets, parsed, normalisers)
        for facets in parsed.datasets
    ]
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
        _get_or_create_raw_doc_dataset_version_link(
            session, _flushed_id(raw_doc), _flushed_id(version)
        )


_Row = TypeVar("_Row")


def _get_or_create(
    session: Session,
    lookup: Callable[[], _Row | None],
    build: Callable[[], _Row],
    on_unresolved: Callable[[_Row, IntegrityError], None] | None = None,
) -> _Row:
    """
    Return the row `lookup` finds, else insert `build()`

    The insert goes in through a savepoint,
    so an insert that trips a uniqueness constraint can be rolled back
    without losing the rest of the transaction.
    We then re-run `lookup`, and reuse the row if it now finds one.
    That covers another transaction inserting the same row
    after our first lookup and committing before our insert,
    which can happen on backends that let transactions write concurrently
    and show each statement the latest committed rows
    (e.g. PostgreSQL's default READ COMMITTED isolation).
    Under snapshot isolation (REPEATABLE READ or SERIALIZABLE)
    the re-run cannot see the other transaction's row, so the error propagates.
    On SQLite the race cannot happen at all,
    because [`esmporium.db.configure_sqlite_for_concurrency`][]
    makes writing transactions take turns.

    If the re-run still finds nothing,
    `on_unresolved` (if given) is called with the row and the error,
    so it can raise a more specific error.
    Otherwise, or if it returns, the integrity error is re-raised.
    """
    existing = lookup()
    if existing is not None:
        return existing

    # The savepoint is opened *before* the add so that rolling it back also expunges the
    # pending row; otherwise it would linger and be retried on the next flush,
    # resurfacing as a confusing error against an unrelated row.
    savepoint = session.begin_nested()
    row = build()
    session.add(row)
    try:
        session.flush()
    except IntegrityError as exc:
        savepoint.rollback()
        raced = lookup()
        if raced is not None:
            return raced
        if on_unresolved is not None:
            on_unresolved(row, exc)
        raise
    else:
        savepoint.commit()

    return row


def _get_or_create_dataset(
    session: Session,
    facets: DatasetFacets,
    parsed: ParsedDocument,
    normalisers: Mapping[str, NormaliseFunc],
) -> Dataset:
    """Reuse an identical dataset if we have one, else save a new one.

    `facets` is the search layer's typed row; `model_dump()` turns it into `Dataset`
    kwargs. Building the `Dataset` here is also the loud boundary check: a field on
    `DatasetFacets` that `Dataset` does not accept fails here, not silently dropped.

    Matching on *every* facet (an equal `grid_label` NULL included) keeps re-ingestion
    idempotent without merging two datasets that differ on any single column.

    If the insert trips the identity index and the lookup still finds nothing, the clash
    is genuine (plain equality cannot see a NULL-vs-'' `grid_label` the identity index
    coalesces), so it is raised as an `UnhandledDatasetClashError`, with `parsed` and
    `normalisers` used to report which facets differ
    (see [save_dataset][(m).save_dataset]).
    """
    facet_values = facets.model_dump()
    conditions = [
        getattr(Dataset, column) == value for column, value in facet_values.items()
    ]

    return _get_or_create(
        session,
        # We match on every column,
        # so the identity index (see `Dataset.__table_args__`)
        # guarantees at most one row can satisfy this.
        # `one_or_none` encodes exactly that expectation:
        # it returns the row or `None`, and raises `MultipleResultsFound`
        # if a second ever exists
        # which means that a corrupt or mis-migrated database fails loudly.
        lambda: session.exec(select(Dataset).where(*conditions)).one_or_none(),
        lambda: Dataset(**facet_values),
        on_unresolved=lambda dataset, exc: _raise_if_dataset_clash(
            session, dataset, exc, _build_raw_doc(parsed), normalisers
        ),
    )


def _upsert_version(
    session: Session, dataset_id: int, parsed: ParsedDocument
) -> DatasetVersion:
    """Insert this dataset's version, or refresh its snapshot flags if seen before."""
    version = _get_or_create(
        session,
        lambda: session.exec(
            select(DatasetVersion).where(
                DatasetVersion.dataset_id == dataset_id,
                DatasetVersion.version == parsed.version,
            )
        ).one_or_none(),
        lambda: DatasetVersion(
            dataset_id=dataset_id,
            version=parsed.version,
            is_latest=parsed.is_latest,
            retracted=parsed.retracted,
        ),
    )
    # Refresh the snapshot flags in case this row was seen on an earlier ingest,
    # so a re-ingest keeps `is_latest`/`retracted` current.
    # The row is already in the session,
    # so the next flush (at the latest, on commit) writes any change.
    version.is_latest = parsed.is_latest
    version.retracted = parsed.retracted
    return version


def _get_or_create_node(session: Session, data_node: str) -> DataNode:
    """Reuse the row for this data node if we have one, else create it."""
    return _get_or_create(
        session,
        lambda: session.exec(
            select(DataNode).where(DataNode.data_node == data_node)
        ).one_or_none(),
        lambda: DataNode(data_node=data_node),
    )


def _get_or_create_version_node_link(
    session: Session, dataset_version_id: int, data_node_id: int
) -> DatasetVersionDataNodeLink:
    """Link a version to a data node, once."""
    return _get_or_create(
        session,
        lambda: session.exec(
            select(DatasetVersionDataNodeLink).where(
                DatasetVersionDataNodeLink.dataset_version_id == dataset_version_id,
                DatasetVersionDataNodeLink.data_node_id == data_node_id,
            )
        ).one_or_none(),
        lambda: DatasetVersionDataNodeLink(
            dataset_version_id=dataset_version_id, data_node_id=data_node_id
        ),
    )


def _get_or_create_raw_doc(session: Session, parsed: ParsedDocument) -> DatasetRawDoc:
    """Store the raw JSON once, keyed by `esgf_doc_id`."""
    return _get_or_create(
        session,
        lambda: session.exec(
            select(DatasetRawDoc).where(DatasetRawDoc.esgf_doc_id == parsed.esgf_doc_id)
        ).one_or_none(),
        lambda: _build_raw_doc(parsed),
    )


def _build_raw_doc(parsed: ParsedDocument) -> DatasetRawDoc:
    """Build (but do not add to any session) the raw doc row for a parsed document."""
    return DatasetRawDoc(
        esgf_doc_id=parsed.esgf_doc_id,
        raw_json=parsed.raw_json,
        raw_docs_format_tag=parsed.raw_docs_format_tag,
    )


def _get_or_create_raw_doc_dataset_version_link(
    session: Session, raw_doc_id: int, dataset_version_id: int
) -> RawDocVersionLink:
    """Link a raw document to a version, once."""
    return _get_or_create(
        session,
        lambda: session.exec(
            select(RawDocVersionLink).where(
                RawDocVersionLink.raw_doc_id == raw_doc_id,
                RawDocVersionLink.dataset_version_id == dataset_version_id,
            )
        ).one_or_none(),
        lambda: RawDocVersionLink(
            raw_doc_id=raw_doc_id, dataset_version_id=dataset_version_id
        ),
    )
