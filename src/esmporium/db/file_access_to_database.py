"""
Filling in per-file access information for Solr results

Solr publishes `access`, `size` and `number_of_files` on a dataset document but **no
per-file URLs**: `type` is a document-type discriminator inside one index, and only
`type=File` documents carry URLs. So getting file access out of Solr is a second
request, which is the whole asymmetry with ESGF-NG, where a STAC item's `assets` are
its files and [ingest_parsed_documents][esmporium.db.ingest_parsed_documents] has
already recorded its routes by the time this could be called.

"Solr" here means both of our Solr-shaped APIs: classic ESGF-1 (`/esg-search`) and
the ESGF-1.5 bridge (`/esgf-1-5-bridge/`). Both answer `type=File` requests with
documents of the same shape, down to the `url` triples, so both take this path; what
decides it is the facade's
[raw_docs_format_tag][esmporium.search.apis.SearchAPI.raw_docs_format_tag] and never
the host.

**The fetching does not happen here.** Sending the request and paging through the
answer is `search`'s job
([collect_all_file_pages][esmporium.search.collect_all_file_pages]), exactly as it is
for a dataset search, and what is left here is the half that needs a database: which
stored version each answer belongs to, and writing it. The two meet at
[FileResultProcessor][esmporium.search.FileResultProcessor], the file-level mirror of
the [ResultProcessor][esmporium.search.ResultProcessor] that dataset results are
saved through.

[record_solr_file_access][(m).record_solr_file_access] drives both halves from here,
for the same reason [esmporium.db.search_tracking][] drives a live search from here:
it both fetches and writes, and `search` never imports `db`.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from esmporium.db.results_to_database import (
    IngestedDocument,
    flushed_id,
    save_data_access_payload,
)
from esmporium.db.schema import DataAccess
from esmporium.search import collect_all_file_pages

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from sqlmodel import Session

    from esmporium.search import (
        DatasetFacets,
        FileResultProcessor,
        ParsedFileDocument,
        SearchAPICallObserver,
        SearchAPIFacade,
    )

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 50
"""
How many datasets to ask about in one `type=File` request

A URL-length budget rather than a page size: a node-specific `dataset_id` runs to
about 100 characters and appears once per dataset in a `GET` query string, so this
keeps a request comfortably inside the roughly 8 kB that is safe to send. Paging
within one batch's answer is a separate thing, and automatic.
"""

DEFAULT_LIMIT = 10_000
"""Page size for a `type=File` request, i.e. the most files to take per page"""


class AmbiguousFileAttributionError(ValueError):
    """Raised when a batch cannot say which version a file would belong to"""

    def __init__(
        self,
        dataset_id: str,
        facet_values: Mapping[str, tuple[str, ...]],
        version_ids: tuple[int, int],
    ) -> None:
        self.dataset_id = dataset_id
        """The dataset id which two versions share"""

        self.facet_values = dict(facet_values)
        """The facet values the batch would have narrowed by"""

        self.version_ids = version_ids
        """The two version rows it could not choose between"""

        narrowed = (
            ", ".join(
                f"{facet}={'/'.join(values)}"
                for facet, values in sorted(self.facet_values.items())
            )
            or "nothing"
        )
        super().__init__(
            f"{dataset_id} maps to dataset versions {version_ids[0]} and "
            f"{version_ids[1]}, and a file-level request narrowed by {narrowed} "
            "cannot tell which of them a file belongs to. The facets to narrow by "
            "come from the facade's result parser "
            "(ResultParserProtocol.file_attribution_facets), so a parser whose "
            "documents map to several dataset rows has to name the facets which "
            "tell those rows apart."
        )


@dataclass(frozen=True)
class SolrFileBatch:
    """One `type=File` request's worth of work: which datasets, and how narrowed"""

    facet_values: Mapping[str, tuple[str, ...]]
    """
    The facets to narrow this request by, named canonically

    Empty when the datasets in this batch need no narrowing, which is a project whose
    documents are one dataset row each (CMIP6, CMIP7). For CMIP5 this is the batch's
    variable, which is what makes a CMIP5 answer attributable at all. Which facets
    these are is the facade's answer, not ours; see
    [file_attribution_facets][esmporium.search.SearchAPIFacade.file_attribution_facets].
    """

    dataset_version_id_by_dataset_id: Mapping[str, int]
    """
    Which dataset version each node-specific `dataset_id` in this batch belongs to

    Built without a database lookup: a Solr File document names its dataset as
    `dataset_id`, which is exactly the `<instance_id>|<data_node>` the dataset
    document published as its `id`, and so exactly
    [ParsedDocument.esgf_doc_id][esmporium.search.ParsedDocument.esgf_doc_id].

    This is the half of the work that cannot leave `db`: they are surrogate keys, and
    `search` has no business holding one.
    """


def attribution_key(
    facets: DatasetFacets, attribution_facets: Sequence[str]
) -> tuple[tuple[str, str], ...]:
    """
    Build the key which groups datasets into one `type=File` request

    Parameters
    ----------
    facets
        The dataset row to build a key for

    attribution_facets
        The facets to key on, named canonically

        From
        [file_attribution_facets][esmporium.search.SearchAPIFacade.file_attribution_facets].

    Returns
    -------
    :
        The value of each of `attribution_facets`, paired with its name

        Empty when `attribution_facets` is, which puts every dataset in one group.
    """
    return tuple((facet, getattr(facets, facet)) for facet in attribution_facets)


def check_attribution_is_unambiguous(
    dataset_version_id_by_dataset_id: Mapping[str, int],
    dataset_id: str,
    dataset_version_id: int,
    facet_values: Mapping[str, tuple[str, ...]],
) -> None:
    """
    Check a batch would not be asking about two versions it could not tell apart

    Within one batch a file is placed by its `dataset_id` alone, so the same
    `dataset_id` arriving twice for two different versions means the narrowing does
    not narrow enough and every file in the answer would be guessed at.

    Impossible today -- the one parser whose documents map to several dataset rows
    names the facet which tells them apart -- which is exactly why it is checked here
    rather than trusted: the facets come from the facade now, so a future parser can
    get this wrong, and this is where that would otherwise go unnoticed.

    Parameters
    ----------
    dataset_version_id_by_dataset_id
        What the batch has placed so far

    dataset_id
        The dataset id being placed

    dataset_version_id
        The version it is being placed against

    facet_values
        The facet values this batch narrows by, for the error message

    Raises
    ------
    AmbiguousFileAttributionError
        `dataset_id` is already in this batch, against a different version
    """
    already = dataset_version_id_by_dataset_id.get(dataset_id)
    if already is not None and already != dataset_version_id:
        raise AmbiguousFileAttributionError(
            dataset_id, facet_values, (already, dataset_version_id)
        )


def plan_solr_file_batches(
    ingested: Iterable[IngestedDocument],
    attribution_facets: Sequence[str] = (),
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> tuple[SolrFileBatch, ...]:
    """
    Work out the `type=File` requests needed for some ingested Solr documents

    Datasets are grouped by the values of `attribution_facets`, and each group is then
    chunked to `batch_size`. With no facets to group by that is one group and as many
    datasets per request as the URL budget allows; with `("variable",)` it is one
    group per variable.

    Which it is, is the facade's answer rather than this function's: see
    [file_attribution_facets][esmporium.search.SearchAPIFacade.file_attribution_facets]
    for why a CMIP5 request must narrow by variable and a CMIP6/CMIP7 one need not.

    Parameters
    ----------
    ingested
        The documents already written, as
        [ingest_parsed_documents][esmporium.db.ingest_parsed_documents] returned them

    attribution_facets
        The facets a request must narrow by for its answers to be placeable

        Read from the facade. Empty means a `dataset_id` already names one of our
        dataset rows.

    batch_size
        The most datasets to ask about in one request

        See [DEFAULT_BATCH_SIZE][(m).DEFAULT_BATCH_SIZE].

    Returns
    -------
    :
        The batches to ask for

    Raises
    ------
    AmbiguousFileAttributionError
        `attribution_facets` does not tell two versions of one dataset apart
    """
    by_key: dict[tuple[tuple[str, str], ...], dict[str, int]] = defaultdict(dict)
    for document in ingested:
        # `versions[i]` is the version of `parsed.datasets[i]`: the guarantee
        # `IngestedDocument.versions` documents, and what lets us group these by
        # facet value without going back to the database.
        for facets, version in zip(
            document.parsed.datasets, document.versions, strict=True
        ):
            key = attribution_key(facets, attribution_facets)
            check_attribution_is_unambiguous(
                by_key[key],
                document.parsed.esgf_doc_id,
                flushed_id(version),
                _facet_values(key),
            )
            by_key[key][document.parsed.esgf_doc_id] = flushed_id(version)

    batches = []
    for key, version_by_dataset_id in by_key.items():
        dataset_ids = list(version_by_dataset_id)
        for start in range(0, len(dataset_ids), batch_size):
            chunk = dataset_ids[start : start + batch_size]
            batches.append(
                SolrFileBatch(
                    facet_values=_facet_values(key),
                    dataset_version_id_by_dataset_id={
                        dataset_id: version_by_dataset_id[dataset_id]
                        for dataset_id in chunk
                    },
                )
            )

    return tuple(batches)


def _facet_values(
    key: tuple[tuple[str, str], ...],
) -> dict[str, tuple[str, ...]]:
    """Turn a grouping key back into facet values a request can be narrowed by"""
    return {facet: (value,) for facet, value in key}


def build_file_result_processor(
    session: Session, batch: SolrFileBatch, written: list[DataAccess]
) -> FileResultProcessor:
    """
    Build a processor that writes one batch's file documents into `session`

    The returned callback is what
    [collect_all_file_pages][esmporium.search.collect_all_file_pages] hands each page
    of file documents to as it arrives, so a batch which fails part way through has
    still written the pages it did get. The file-level mirror of
    [build_result_processor][esmporium.db.build_result_processor], with one
    difference: this does **not** commit. A file-level request is one step of a larger
    piece of work whose transaction boundary the caller owns, the same way
    [ingest_parsed_documents][esmporium.db.ingest_parsed_documents] leaves that to its
    caller.

    Writes through
    [save_data_access_payload][esmporium.db.save_data_access_payload], the same path
    ESGF-NG's results take, so the two sources of file information cannot grow two
    ways of being stored.

    Parameters
    ----------
    session
        The session the processor writes into

    batch
        The batch being answered, which says which version each answer belongs to

    written
        Appended to with the rows each page writes

        A list the caller owns rather than something the processor hands back,
        because a processor returns nothing -- that is the shape `search` calls it
        through -- and the rows are what
        [record_solr_file_access][(m).record_solr_file_access] answers with.

    Returns
    -------
    :
        A callback of the shape
        [FileResultProcessor][esmporium.search.FileResultProcessor]
    """

    def processor(
        facade: SearchAPIFacade, parsed_documents: tuple[ParsedFileDocument, ...]
    ) -> None:
        # `facade` is part of the FileResultProcessor callback contract but is not
        # needed here: everything project-specific happened on the way out, when the
        # request was built.
        for document in parsed_documents:
            written.extend(record_file_document(session, batch, document))

    return processor


def record_file_document(
    session: Session, batch: SolrFileBatch, document: ParsedFileDocument
) -> Sequence[DataAccess]:
    """
    Record one file document against the stored version it belongs to

    Parameters
    ----------
    session
        The session to write into

    batch
        The batch this document is an answer to

    document
        The document to record

    Returns
    -------
    :
        The route rows written or reused, or nothing if this document could not be
        placed
    """
    dataset_version_id = batch.dataset_version_id_by_dataset_id.get(document.dataset_id)
    if dataset_version_id is None:
        # A federated search can be generous: `distrib=true` means a node may answer
        # about datasets we did not ask about. Nothing is wrong, there is just
        # nowhere for this to go, so it is noted and skipped rather than guessed at.
        logger.debug(
            "skipping a file document for %r, which this batch did not ask about",
            document.dataset_id,
            extra={"dataset_id": document.dataset_id},
        )
        return ()

    saved = save_data_access_payload(session, dataset_version_id, document.payload)

    return saved.accesses


def record_solr_file_access(  # noqa: PLR0913 - one parameter per injection seam
    session: Session,
    facade: SearchAPIFacade,
    ingested: Iterable[IngestedDocument],
    *,
    client: httpx.Client | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit: int = DEFAULT_LIMIT,
    max_results: int | None = None,
    api_call_observer: SearchAPICallObserver | None = None,
) -> tuple[DataAccess, ...]:
    """
    Fill in per-file information for Solr results, as a second request

    A separate, explicit step rather than part of parsing, because it is a separate
    request: there is nothing about files on a Solr dataset document for a parser to
    read.

    Each batch is fetched and paged by
    [collect_all_file_pages][esmporium.search.collect_all_file_pages] and written by
    [build_file_result_processor][(m).build_file_result_processor], which is the same
    division of labour a dataset search has: `search` fetches and parses, `db` writes.

    Does not commit: the caller owns the transaction boundary, as
    [ingest_parsed_documents][esmporium.db.ingest_parsed_documents] does.

    Parameters
    ----------
    session
        Session to write with

    facade
        The facade the documents came from, which must be a Solr-shaped one

    ingested
        The documents already written, as
        [ingest_parsed_documents][esmporium.db.ingest_parsed_documents] returned them

    client
        The HTTP client to send with. If `None`, one is made and closed here.

    batch_size
        The most datasets to ask about in one request

    limit
        Page size

    max_results
        The most file documents to take from one batch before stopping with a
        [PaginationLimitError][esmporium.search.PaginationLimitError]

        `None`, the default, disables the cap.

    api_call_observer
        Observer to report each call to, for endpoint health

    Returns
    -------
    :
        The route rows written or reused, across every batch

    Raises
    ------
    CouldNotSearchError
        A batch's request failed, or its answer could not be read

        Raised rather than returned, because this function's answer is the rows it
        wrote and there is nowhere on that to hang a failure. Whatever earlier pages
        and batches did write is still in `session`, so a caller which commits
        anyway keeps them.

    NotASolrFacadeError
        `facade` is not Solr-shaped, so it has no `type=File` request to make

    PaginationLimitError
        A batch collected more than `max_results` file documents, or the endpoint
        asked us to re-request a page we had already requested
    """
    # Checked before anything is planned or fetched, so a facade which could never
    # answer this is refused loudly and immediately.
    facade.file_search_api()

    batches = plan_solr_file_batches(
        ingested, facade.file_attribution_facets, batch_size=batch_size
    )

    owns_client = client is None
    client = client if client is not None else httpx.Client(follow_redirects=True)
    try:
        written: list[DataAccess] = []
        for batch in batches:
            pages = collect_all_file_pages(
                client,
                facade,
                facade.build_file_search_request(
                    list(batch.dataset_version_id_by_dataset_id),
                    limit,
                    batch.facet_values,
                ),
                limit=limit,
                max_results=max_results,
                # A file-level request is made on a caller's behalf rather than at
                # their request, and a dataset with more files than fit in one page
                # of 10,000 is not something they chose or can narrow, so there is
                # nothing for them to do about a warning here.
                warn_on_pagination=False,
                api_call_observer=api_call_observer,
                processor=build_file_result_processor(session, batch, written),
            )
            if pages.failure is not None:
                raise pages.failure
    finally:
        if owns_client:
            client.close()

    return tuple(written)
