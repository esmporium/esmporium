"""
Writing search results into the database
"""

from __future__ import annotations

import json
import warnings
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from sqlalchemy import Engine, func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from esmporium.datasets import DATASET_FACET_COLUMNS
from esmporium.db.dataset_uniqueness import facet_differences
from esmporium.db.engine import is_sqlite_configured_for_concurrency
from esmporium.db.schema import (
    DATASET_IDENTITY_INDEX,
    DataAccess,
    DataNode,
    Dataset,
    DatasetRawDoc,
    DatasetVersion,
    DatasetVersionDataNodeLink,
    File,
    RawDocVersionLink,
)
from esmporium.search.result_normalisation import (
    DEFAULT_NORMALISERS,
    NormaliseFunc,
    UnknownRawDocFormatTagError,
    normalise_stored_document,
)
from esmporium.search.result_parsing import (
    DataAccessInfo,
    DataAccessPayload,
    DatasetFacets,
    FileInfo,
    ParsedDocument,
    ResultProcessor,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
    from contextlib import AbstractContextManager

    from esmporium.search.search_api_facade import SearchAPIFacade


class UnhandledDatasetClashError(Exception):
    """
    Two datasets are identical over every column, including id_project_specific

    We did not expect data of this shape.
    The data is presumably different in some facet(s) we do not model,
    and which also do not appear in id_project_specific,
    so that difference is invisible to [Dataset][esmporium.db.schema.Dataset].

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
            as returned by [facet_differences][esmporium.db.facet_differences].

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

    See [esmporium.db.configure_sqlite_for_concurrency][]
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
        see [esmporium.search.normalise_stored_document][].

    Returns
    -------
    :
        The same `dataset`, now flushed into `session`

    Raises
    ------
    UnhandledDatasetClashError
        `dataset` is identical, in every column our model records, to one already
        stored (see [Dataset][esmporium.db.schema.Dataset]'s identity index).

        If `raw_doc` is supplied and the stored dataset has raw documents,
        the error's `differences` holds the facets that differ between them.

    UnconfiguredSQLiteEngineError
        `session` is bound to a SQLite engine that has not been configured with
        [esmporium.db.configure_sqlite_for_concurrency][].
    """
    check_engine_is_configured(session)

    return get_or_create(
        session,
        # Nothing to reuse: saving a dataset that is already stored is the clash.
        lookup=lambda: None,
        build=lambda: dataset,
        on_unresolved=lambda row, exc: _raise_if_dataset_clash(
            session, row, exc, raw_doc, normalisers
        ),
    )


def check_engine_is_configured(session: Session) -> None:
    """
    Raise if `session` writes to a SQLite engine without real transactions

    Saving uses savepoints,
    which only behave on SQLite once the engine is configured
    (see [esmporium.db.configure_sqlite_for_concurrency][]).
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


@dataclass(frozen=True)
class IngestedDocument:
    """
    One document which was written, and the version rows it turned into

    A document describes one version of one dataset, except in CMIP5, where one document
    bundles several datasets' versions together -- hence `versions` being a tuple.
    """

    parsed: ParsedDocument
    """The document, as the facade parsed it"""

    versions: tuple[DatasetVersion, ...]
    """
    The version rows it was written to, flushed, so each one's `id` is set

    One per dataset the document described, **index-aligned with
    `parsed.datasets`**: `versions[i]` is the version of `parsed.datasets[i]`.

    That alignment was always true and is now relied on, so it is written down:
    [plan_solr_file_batches][esmporium.db.plan_solr_file_batches] uses it to work out
    which variable each of a CMIP5 bundle's versions is for, without a lookup.
    """

    files: tuple[File, ...] = ()
    """
    The file rows written for this document, flushed, so each one's `id` is set

    Empty for a document that described no files, which is every Solr *dataset*
    document -- those get their files from
    [record_solr_file_access][esmporium.db.record_solr_file_access] instead.

    Returned because the next step needs them: dataset ancestry reads a file's header
    and has to be able to say which file a fact came from.
    """

    accesses: tuple[DataAccess, ...] = ()
    """
    The route rows written for this document, flushed

    Empty for the same documents as `files`.
    """


def ingest_parsed_documents(
    session: Session,
    parsed_documents: Iterable[ParsedDocument],
    normalisers: Mapping[str, NormaliseFunc] = DEFAULT_NORMALISERS,
) -> tuple[IngestedDocument, ...]:
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

    Returns
    -------
    :
        One [IngestedDocument][(m).] per document written, in the order they were given.

        Saving does not need these; tracking a search over time does. See
        [esmporium.db.search_tracking][], which records what one run saw and cannot work
        that out from the tables afterwards: the version rows are refreshed by the next
        run, and the node links accumulate over every run rather than saying which run
        saw which node.

    Raises
    ------
    UnconfiguredSQLiteEngineError
        `session` is bound to a SQLite engine that has not been configured with
        [esmporium.db.configure_sqlite_for_concurrency][].
    """
    check_engine_is_configured(session)

    return tuple(
        _ingest_document(session, parsed, normalisers) for parsed in parsed_documents
    )


def build_result_processor(
    session: Session,
    normalisers: Mapping[str, NormaliseFunc] = DEFAULT_NORMALISERS,
) -> ResultProcessor:
    """
    Build a processor that persists one facade's parsed results into `session`

    The returned callback is what
    [esmporium.search.search_single_project][] calls
    with each page of results as it arrives:
    it ingests that page's documents and commits,
    so each page is saved all or nothing
    and is durable as soon as it arrives.
    Inject it as
    `search_single_project(..., processor=build_result_processor(session))`.
    For the multi-query [esmporium.search.search][], which wants a fresh processor per
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
        [ResultProcessor][esmporium.search.result_parsing.ResultProcessor].
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
    Build a factory that makes a fresh saving processor per sub-search

    This is the database-saving [ProcessorFactory][esmporium.search.ProcessorFactory] to
    hand to [esmporium.search.search][]: it is called once per sub-search
    (one leaf of the requirement, against one project) and opens a
    fresh `sqlmodel.Session` (and so a fresh transaction) for that sub-search,
    yields a [build_result_processor][(m).] bound to it, and closes it afterwards.
    A session per sub-search keeps each sub-search's writes in transactions of its own
    (one per page, see [build_result_processor][(m).]),
    and lets a parallel search give each worker its own session.

    Everything a search returns is saved, not just what the requirement asked for.
    That is deliberate: working out what the requirement adds up to is
    [solve][esmporium.requirements.solve]'s job, against the stored data, so that
    the same question can be asked again tomorrow and the two answers compared.

    Parameters
    ----------
    engine
        The database engine each sub-search's session is opened on.

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
    ...     requirement,
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


def flushed_id(row: _SurrogateKeyRow) -> int:
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
) -> IngestedDocument:
    """Write one parsed document: its datasets, versions, nodes, raw doc and links."""
    datasets = [
        _get_or_create_dataset(session, facets, parsed, normalisers)
        for facets in parsed.datasets
    ]
    dataset_versions = [
        _upsert_version(session, flushed_id(dataset), parsed) for dataset in datasets
    ]

    nodes = [_get_or_create_node(session, node.data_node) for node in parsed.nodes]
    for version in dataset_versions:
        for node in nodes:
            _get_or_create_version_node_link(
                session, flushed_id(version), flushed_id(node)
            )

    raw_doc = _get_or_create_raw_doc(session, parsed)
    for version in dataset_versions:
        _get_or_create_raw_doc_dataset_version_link(
            session, flushed_id(raw_doc), flushed_id(version)
        )

    # Files and routes come last, because a route is the most derived thing here: it
    # needs its version, its file and its node to exist first.
    check_access_payload_is_attributable(parsed)
    saved = (
        save_data_access_payload(
            session,
            flushed_id(dataset_versions[0]),
            DataAccessPayload(files=parsed.files, accesses=parsed.accesses),
        )
        if parsed.files or parsed.accesses
        else SavedDataAccess()
    )

    return IngestedDocument(
        parsed=parsed,
        versions=tuple(dataset_versions),
        files=saved.files,
        accesses=saved.accesses,
    )


class AmbiguousDataAccessError(ValueError):
    """
    Raised when a document described files but maps to more than one dataset version
    """

    def __init__(self, parsed: ParsedDocument) -> None:
        self.parsed = parsed
        """The document we refused"""

        super().__init__(
            f"The document {parsed.esgf_doc_id!r} described "
            f"{len(parsed.files)} file(s) and {len(parsed.accesses)} route(s), "
            f"but maps to {len(parsed.datasets)} dataset rows "
            f"({sorted(row.variable for row in parsed.datasets)}). "
            "A file belongs to one dataset version, and a document which bundles "
            "many variables carries nothing that says which of them a file is part "
            "of, so attributing its files to any one of them would be a guess. "
            "Fill these in with `esmporium.db.record_solr_file_access` instead, "
            "which asks one variable at a time and so knows the answer."
        )


class UnknownRouteFileError(ValueError):
    """Raised when a route names a file neither its payload nor the database has"""

    def __init__(self, info: DataAccessInfo, dataset_version_id: int) -> None:
        self.info = info
        """The route we could not place"""

        super().__init__(
            f"A route to {info.url!r} says it serves the file "
            f"{info.filename!r} of dataset version {dataset_version_id}, "
            "but that version has no such file, either in the payload alongside "
            "this route or already stored. "
            "A route and the file it serves are parsed together, so either the two "
            "have been taken apart somewhere between parsing and here, or the "
            "route has been attributed to the wrong version "
            "(the same filename under another version is a different file)."
        )


def check_access_payload_is_attributable(parsed: ParsedDocument) -> None:
    """
    Check that a document's files and routes can be attributed to one version

    Parameters
    ----------
    parsed
        The document to check

    Raises
    ------
    AmbiguousDataAccessError
        It carries files or routes, and maps to more than one dataset version
    """
    carries_access = bool(parsed.files or parsed.accesses)
    if carries_access and len(parsed.datasets) > 1:
        raise AmbiguousDataAccessError(parsed)


FILE_FACT_COLUMNS: tuple[str, ...] = (
    "size",
    "checksum",
    "checksum_type",
    "tracking_id",
)
"""
The columns of [File][esmporium.db.schema.File] which describe the bytes

i.e. everything a document *asserts* about a file, as opposed to the two columns which
identify the row (`dataset_version_id` and `filename`). These are the ones
[save_data_access_payload][(m).save_data_access_payload] refreshes, and so the ones
[check_file_facts_agree][(m).check_file_facts_agree] compares.
"""


class FileFactsChangedWarning(UserWarning):
    """
    Warned when two documents describe the same file differently

    Same version, same filename, different bytes: either two nodes disagree about a
    replica, or data was republished under a version that already existed. Both are
    upstream problems rather than ours, and neither is something our model can
    represent -- there is one [File][esmporium.db.schema.File] row per
    `(dataset_version_id, filename)`, so one of the two answers is all we can hold.

    This is a warning rather than an error on purpose. It is ESGF's inconsistency to
    fix, the same way replicas lagging on retraction is -- which is recorded the same
    way, in the note on how a version row's snapshot flags are refreshed -- and
    raising here would abort an entire search over one file: the processors which
    write results are called from inside paging, which does not catch this, so a
    raise would discard every page not yet committed.
    """


def file_fact_differences(stored: File, info: FileInfo) -> dict[str, tuple[Any, Any]]:
    """
    Find the facts a document asserts about a file which disagree with what we hold

    A fact the document is silent about (`None`) is not a disagreement. A document can
    be silent about any of these: a checksum we cannot decode is stored as published
    with `checksum_type` left `None` (see
    [File.checksum][esmporium.db.schema.File.checksum]), so a STAC document can hand
    us a digest without naming its algorithm where a Solr one names it. Reading that
    `None` as "the algorithm is now nothing" would erase what the other API told us.
    Silence is silence, in either direction.

    Parameters
    ----------
    stored
        The file row we already hold

    info
        What this document says about the same file

    Returns
    -------
    :
        The held and asserted value of each of
        [FILE_FACT_COLUMNS][(m).FILE_FACT_COLUMNS] on which the two disagree, keyed by
        column. Empty when they agree, or when one of the two is silent.
    """
    differences: dict[str, tuple[Any, Any]] = {}
    for column in FILE_FACT_COLUMNS:
        held = getattr(stored, column)
        asserted = getattr(info, column)
        if held is None or asserted is None or held == asserted:
            continue

        differences[column] = (held, asserted)

    return differences


def check_file_facts_agree(
    stored: File, info: FileInfo, data_nodes: Sequence[str] = ()
) -> None:
    """
    Check a document describes a file we already hold the same way, and say so if not

    Parameters
    ----------
    stored
        The file row we already hold

    info
        What this document says about the same file

    data_nodes
        The nodes this document says serve the file, named in the warning

        Read from the routes travelling with the file rather than from the file
        itself, because a [File][esmporium.db.schema.File] is node-independent by
        design and carries no node of its own.

    Warns
    -----
    FileFactsChangedWarning
        The document asserts a different value for something we already hold
    """
    differences = file_fact_differences(stored, info)
    if not differences:
        return

    served_by = ", ".join(data_nodes) if data_nodes else "a node we cannot name"
    changes = "; ".join(
        f"{column}: we hold {held!r}, this document says {asserted!r}"
        for column, (held, asserted) in sorted(differences.items())
    )
    warnings.warn(
        f"{info.filename} of dataset version {stored.dataset_version_id} is "
        f"described differently by two documents. {changes}. The newer value is "
        f"kept, so the one we held is now gone: a file document's own JSON is not "
        f"stored anywhere, so it cannot be recovered from the database. This "
        f"document came from {served_by}. The same filename under the same version "
        "should describe the same bytes on every replica, so this means either two "
        "nodes disagree or the data was republished under a version that already "
        "existed. Both are ESGF's to fix, and we cannot ask upstream without cases: "
        "please raise an issue at https://github.com/esmporium/esmporium/issues.",
        FileFactsChangedWarning,
        stacklevel=2,
    )


@dataclass(frozen=True)
class SavedDataAccess:
    """The rows one payload turned into, flushed, so every `id` is set"""

    files: tuple[File, ...] = ()
    """The file rows written"""

    accesses: tuple[DataAccess, ...] = ()
    """The route rows written"""


def save_data_access_payload(
    session: Session, dataset_version_id: int, payload: DataAccessPayload
) -> SavedDataAccess:
    """
    Write one dataset version's files and routes, reusing anything already stored

    Public, unlike the `_get_or_create_*` helpers it is built from, because the Solr
    file step ([record_solr_file_access][esmporium.db.record_solr_file_access])
    writes through exactly this path. The two sources of file information must not
    grow two ways of storing it.

    Idempotent, the way the rest of ingestion is: re-recording the same file or the
    same route reuses the row rather than duplicating it. A file is matched on
    `(dataset_version_id, filename)` and a route on
    `(dataset_version_id, service, container_format, url)` -- see the identity notes
    on [File][esmporium.db.schema.File] and
    [DataAccess][esmporium.db.schema.DataAccess].

    On a file we already hold, each of [FILE_FACT_COLUMNS][(m).FILE_FACT_COLUMNS] is
    refreshed from what this document *asserts*, and left alone where this document is
    silent (`None`). Both halves matter, and they are not the same rule:

    - **Asserted wins**, so a re-ingest keeps what we hold current, the way
      [DatasetVersion][esmporium.db.schema.DatasetVersion]'s snapshot flags are
      refreshed.
    - **Silence changes nothing**, because the two APIs are silent about different
      things. A checksum we cannot decode leaves `checksum_type` `None` while the
      digest is stored as published, so one API can be silent about the algorithm
      where the other names it; taking that `None` as an assertion would erase the
      name we had.

    A document which asserts something *different* from what we hold is neither of
    those, and warns: see
    [FileFactsChangedWarning][(m).FileFactsChangedWarning]. Measured against live
    answers, that does not happen -- of 153 files served by more than one node within
    one version, every node agreed on all four node-independent facts -- so if it
    fires, it is news.

    Does not commit: the caller owns the transaction boundary.

    Parameters
    ----------
    session
        Session to write with

    dataset_version_id
        The version these files and routes belong to

    payload
        The files and routes to write

    Returns
    -------
    :
        The rows written or reused, flushed

    Raises
    ------
    UnknownRouteFileError
        A route named a file which is neither in `payload` nor already stored for
        this version
    """
    nodes_of = _data_nodes_by_filename(payload)
    files = [
        _get_or_create_file(
            session, dataset_version_id, info, nodes_of.get(info.filename, ())
        )
        for info in payload.files
    ]
    by_filename = {stored.filename: stored for stored in files}

    accesses = []
    for info in payload.accesses:
        file_id = None
        if info.filename is not None:
            # Both lookups are scoped to this version -- `by_filename` holds the
            # files just written against it, and `_find_file` filters on it -- so a
            # route can only ever be given the id of a file belonging to the version
            # the route is for. That is the invariant on `DataAccess.file_id`, held
            # by construction rather than by a runtime check.
            #
            # Scoping is also required, not merely convenient: the same filename
            # legitimately exists under two versions, so an unfiltered lookup by
            # name would find two rows and raise.
            stored_file = by_filename.get(info.filename) or _find_file(
                session, dataset_version_id, info.filename
            )
            if stored_file is None:
                raise UnknownRouteFileError(info, dataset_version_id)

            file_id = flushed_id(stored_file)

        node = _get_or_create_node(session, info.data_node)
        accesses.append(
            _get_or_create_data_access(
                session,
                dataset_version_id,
                file_id,
                flushed_id(node),
                info,
            )
        )

    return SavedDataAccess(files=tuple(files), accesses=tuple(accesses))


def _find_file(session: Session, dataset_version_id: int, filename: str) -> File | None:
    """Find a file we already hold for this version, by name."""
    return session.exec(
        select(File).where(
            File.dataset_version_id == dataset_version_id,
            File.filename == filename,
        )
    ).one_or_none()


def _data_nodes_by_filename(payload: DataAccessPayload) -> dict[str, list[str]]:
    """
    Work out which nodes a payload says serve each of its files

    Read from the routes, because that is where a node is named: a
    [File][esmporium.db.schema.File] is node-independent by design, so
    [FileInfo][esmporium.search.FileInfo] carries no node either. Only used to name
    the nodes in [FileFactsChangedWarning][(m).FileFactsChangedWarning].
    """
    nodes: dict[str, list[str]] = {}
    for info in payload.accesses:
        if info.filename is None:
            continue

        for_file = nodes.setdefault(info.filename, [])
        if info.data_node not in for_file:
            for_file.append(info.data_node)

    return nodes


def _get_or_create_file(
    session: Session,
    dataset_version_id: int,
    info: FileInfo,
    data_nodes: Sequence[str] = (),
) -> File:
    """Reuse the row for this version's file if we have one, else create it."""
    stored = get_or_create(
        session,
        lambda: _find_file(session, dataset_version_id, info.filename),
        lambda: File(
            dataset_version_id=dataset_version_id,
            filename=info.filename,
            size=info.size,
            checksum=info.checksum,
            checksum_type=info.checksum_type,
            tracking_id=info.tracking_id,
        ),
    )

    # On a row we just built these all agree, so this says nothing; on one we already
    # held, it is the only place a disagreement is visible, because the refresh below
    # is about to overwrite what we held with what this document says.
    check_file_facts_agree(stored, info, data_nodes)

    # Asserted wins, silence changes nothing: see `save_data_access_payload`.
    for column in FILE_FACT_COLUMNS:
        asserted = getattr(info, column)
        if asserted is not None:
            setattr(stored, column, asserted)

    session.add(stored)
    session.flush()

    return stored


def _get_or_create_data_access(
    session: Session,
    dataset_version_id: int,
    file_id: int | None,
    data_node_id: int,
    info: DataAccessInfo,
) -> DataAccess:
    """Reuse the row for this route if we have one, else create it."""
    return get_or_create(
        session,
        lambda: session.exec(
            select(DataAccess).where(
                DataAccess.dataset_version_id == dataset_version_id,
                DataAccess.service == info.service,
                DataAccess.container_format == info.container_format,
                DataAccess.url == info.url,
            )
        ).one_or_none(),
        lambda: DataAccess(
            dataset_version_id=dataset_version_id,
            file_id=file_id,
            data_node_id=data_node_id,
            service=info.service,
            container_format=info.container_format,
            url=info.url,
            raw_json=info.raw_json,
        ),
    )


_Row = TypeVar("_Row")


def get_or_create(
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
    because [esmporium.db.configure_sqlite_for_concurrency][]
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
    """
    Reuse an identical dataset if we have one, else save a new one.

    `facets` is the search layer's typed row; `model_dump()` turns it into `Dataset`
    kwargs. Building the `Dataset` here is also the loud boundary check: a field on
    `DatasetFacets` that `Dataset` does not accept fails here, not silently dropped.

    Matching on *every* facet (e.g. an equal `grid_label` NULL included)
    keeps re-ingestion idempotent
    without merging two datasets that differ on any single column.

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

    return get_or_create(
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
    """
    Insert this dataset's version, or refresh its snapshot flags if seen before

    The flags are refreshed from *every* document, so for a version described by
    several documents this is **last-write-wins** -- the opposite convention to the
    first-wins facet merge in [esmporium.db.database_catalogue][]. There is no
    "first" to prefer for a boolean snapshot: unlike a facet, where an absent value
    means the document is silent, every document asserts an `is_latest` and a
    `retracted`.

    It matters more than the facet merge does. Replicas lag on retraction, so two
    nodes disagreeing here is likely rather than exotic, and the consequence is that
    the dataset appears or vanishes under
    [VersionFilter.NOT_RETRACTED][esmporium.requirements.VersionFilter] rather than
    reading oddly -- which, because groups are discovered, shows up as a group that
    is missing rather than one that is unsatisfied. Which answer we store depends on
    ingest order. If you hit it, please raise an issue at
    https://github.com/esmporium/esmporium/issues: the disagreement is ESGF's to
    fix, and we cannot ask upstream without cases.
    """
    version = get_or_create(
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
    return get_or_create(
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
    return get_or_create(
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
    return get_or_create(
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
    return get_or_create(
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
