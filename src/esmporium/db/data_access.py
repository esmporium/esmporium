"""
Filling in per-file access information for ESGF-1 results

ESGF-1 Solr publishes `access`, `size` and `number_of_files` on a dataset document
but **no per-file URLs**: `type` is a document-type discriminator inside one index,
and only `type=File` documents carry URLs. So getting file access out of ESGF-1 is a
second request, which is the whole asymmetry with ESGF-NG, where a STAC item's
`assets` are its files and
[ingest_parsed_documents][esmporium.db.ingest_parsed_documents] has already recorded
its routes by the time this could be called.

This lives in `db` rather than `search` because it both fetches and writes, which is
how [esmporium.db.search_tracking][] already drives a live search. `search` never
imports `db`.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

import httpx

from esmporium.db.results_to_database import (
    IngestedDocument,
    flushed_id,
    save_data_access_payload,
)
from esmporium.db.schema import DataAccess
from esmporium.search.apis import Request, SearchAPI
from esmporium.search.apis.esgf1 import solr_file_access
from esmporium.search.result_normalisation import SOLR_FORMAT_TAG
from esmporium.search.search import fire

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from sqlmodel import Session

    from esmporium.search.health import SearchAPICallObserver
    from esmporium.search.search_api_facade import SearchAPIFacade

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


class SolrSearchAPI(SearchAPI, Protocol):
    """
    A Solr-shaped search API, i.e. one with a `type=File` request to make

    [SearchAPI][esmporium.search.apis.SearchAPI] deliberately does not carry
    `build_file_search_request`: asking for File documents is Solr-only, and putting
    it on the protocol would force
    [SearchAPIESGFNGSTAC][esmporium.search.apis.SearchAPIESGFNGSTAC] to implement
    something meaningless. This is the narrower shape that does carry it, so that
    what [solr_api][(m).solr_api] checks at runtime is also what the types say.
    """

    def build_file_search_request(
        self,
        dataset_ids: Sequence[str],
        limit: int,
        facet_values: Mapping[str, Sequence[str]] | None = None,
    ) -> Request:
        """Build the `type=File` request for some datasets"""
        ...


class NotASolrFacadeError(ValueError):
    """Raised when the Solr file step is handed a facade that is not Solr-shaped"""

    def __init__(self, facade: SearchAPIFacade) -> None:
        self.facade = facade
        """The facade we refused"""

        super().__init__(
            f"{facade.search_api.host} answers in the "
            f"{facade.search_api.raw_docs_format_tag!r} format, not "
            f"{SOLR_FORMAT_TAG!r}, so it has no `type=File` request to make. "
            "ESGF-NG carries per-file information in the search response itself, so "
            "its routes are already recorded by the time this could be called. "
            "This is a mistake rather than a no-op, so it is loud: a caller which "
            "thinks it is filling in file access here is not."
        )


@dataclass(frozen=True)
class SolrFileBatch:
    """One `type=File` request's worth of work: which datasets, and which variable"""

    variable: str
    """
    The variable to narrow by

    Narrowing matters for CMIP5, whose `dataset_id` is *table*-grained: without it,
    asking about one CMIP5 dataset returns every variable in its table. It is also
    what makes a CMIP5 answer attributable at all -- the variable is the only thing
    that says which of a bundle's versions a file belongs to.
    """

    dataset_version_id_by_dataset_id: Mapping[str, int]
    """
    Which dataset version each node-specific `dataset_id` in this batch belongs to

    Built without a database lookup: a Solr File document names its dataset as
    `dataset_id`, which is exactly the `<instance_id>|<data_node>` the dataset
    document published as its `id`, and so exactly
    [ParsedDocument.esgf_doc_id][esmporium.search.ParsedDocument.esgf_doc_id].
    """


def plan_solr_file_batches(
    ingested: Iterable[IngestedDocument],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> tuple[SolrFileBatch, ...]:
    """
    Work out the `type=File` requests needed for some ingested Solr documents

    Batches are grouped **by variable**, not merely chunked into `batch_size`.
    CMIP5 needs that: one document bundles many variables into many of our dataset
    versions, so the variable is the only thing which says which version a file
    belongs to. For CMIP6 and CMIP7, where a `dataset_id` already names one variable,
    it costs one redundant facet on the request.

    Parameters
    ----------
    ingested
        The documents already written, as
        [ingest_parsed_documents][esmporium.db.ingest_parsed_documents] returned them

    batch_size
        The most datasets to ask about in one request

        See [DEFAULT_BATCH_SIZE][(m).DEFAULT_BATCH_SIZE].

    Returns
    -------
    :
        The batches to ask for
    """
    by_variable: dict[str, dict[str, int]] = defaultdict(dict)
    for document in ingested:
        # `versions[i]` is the version of `parsed.datasets[i]`: the guarantee
        # `IngestedDocument.versions` documents, and what lets us name the variable
        # of each version without going back to the database.
        for facets, version in zip(
            document.parsed.datasets, document.versions, strict=True
        ):
            by_variable[facets.variable][document.parsed.esgf_doc_id] = flushed_id(
                version
            )

    batches = []
    for variable, version_by_dataset_id in by_variable.items():
        dataset_ids = list(version_by_dataset_id)
        for start in range(0, len(dataset_ids), batch_size):
            chunk = dataset_ids[start : start + batch_size]
            batches.append(
                SolrFileBatch(
                    variable=variable,
                    dataset_version_id_by_dataset_id={
                        dataset_id: version_by_dataset_id[dataset_id]
                        for dataset_id in chunk
                    },
                )
            )

    return tuple(batches)


def check_facade_is_solr(facade: SearchAPIFacade) -> None:
    """
    Check that a facade is one with a `type=File` request to make

    Parameters
    ----------
    facade
        The facade to check

    Raises
    ------
    NotASolrFacadeError
        It does not answer in the Solr format
    """
    if facade.search_api.raw_docs_format_tag != SOLR_FORMAT_TAG:
        raise NotASolrFacadeError(facade)


def solr_api(facade: SearchAPIFacade) -> SolrSearchAPI:
    """
    Check that a facade is Solr-shaped, and give back its API as that shape

    The check and the narrowing are one function so that the narrowed type cannot be
    had without the check.

    Parameters
    ----------
    facade
        The facade to read

    Returns
    -------
    :
        Its search API

    Raises
    ------
    NotASolrFacadeError
        It does not answer in the Solr format
    """
    check_facade_is_solr(facade)

    # Established by the check above, which the type system cannot see.
    return cast("SolrSearchAPI", facade.search_api)


def record_solr_file_access(  # noqa: PLR0913 - one parameter per injection seam
    session: Session,
    facade: SearchAPIFacade,
    ingested: Iterable[IngestedDocument],
    *,
    client: httpx.Client | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit: int = DEFAULT_LIMIT,
    api_call_observer: SearchAPICallObserver | None = None,
) -> tuple[DataAccess, ...]:
    """
    Fill in per-file information for Solr results, as a second request

    A separate, explicit step rather than part of parsing, because it is a separate
    request: there is nothing about files on a Solr dataset document for a parser to
    read. Writes through
    [save_data_access_payload][esmporium.db.save_data_access_payload], the same path
    ESGF-NG's results take, so the two sources of file information cannot grow two
    ways of being stored.

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

    api_call_observer
        Observer to report each call to, for endpoint health

    Returns
    -------
    :
        The route rows written or reused

    Raises
    ------
    NotASolrFacadeError
        `facade` is not Solr-shaped, so it has no `type=File` request to make
    """
    api = solr_api(facade)

    # CMIP5 spells this facet `variable` and CMIP6/CMIP7 spell it `variable_id`, so
    # it is read from the facade rather than hard-coded either way.
    variable_field = facade.parameters.get_mapping_to_api_facet_names({"variable"})[
        "variable"
    ]

    owns_client = client is None
    client = client if client is not None else httpx.Client(follow_redirects=True)
    try:
        written: list[DataAccess] = []
        for batch in plan_solr_file_batches(ingested, batch_size=batch_size):
            written.extend(
                _record_one_batch(
                    session,
                    api,
                    batch,
                    client=client,
                    variable_field=variable_field,
                    limit=limit,
                    api_call_observer=api_call_observer,
                )
            )
    finally:
        if owns_client:
            client.close()

    return tuple(written)


def _record_one_batch(  # noqa: PLR0913 - one parameter per injection seam
    session: Session,
    api: SolrSearchAPI,
    batch: SolrFileBatch,
    *,
    client: httpx.Client,
    variable_field: str,
    limit: int,
    api_call_observer: SearchAPICallObserver | None,
) -> list[DataAccess]:
    """
    Ask for, and record, one batch's worth of file documents

    Pages with an explicit loop rather than
    [collect_all_pages][esmporium.search.collect_all_pages], because that parses each
    page with the facade's result parser, which would try to read File documents as
    datasets.
    """
    page: Request | None = api.build_file_search_request(
        list(batch.dataset_version_id_by_dataset_id),
        limit,
        {variable_field: (batch.variable,)},
    )

    written: list[DataAccess] = []
    while page is not None:
        raw = fire(client, api, page, api_call_observer)
        for doc in api.extract_result_documents(raw):
            written.extend(_record_one_document(session, batch, doc))

        page = api.next_page_request(page, raw)

    return written


def _record_one_document(
    session: Session, batch: SolrFileBatch, doc: dict[str, Any]
) -> Sequence[DataAccess]:
    """Record one File document's file and routes against the version it belongs to"""
    dataset_id = str(doc.get("dataset_id"))
    dataset_version_id = batch.dataset_version_id_by_dataset_id.get(dataset_id)
    if dataset_version_id is None:
        # A federated search can be generous: `distrib=true` means a node may answer
        # about datasets we did not ask about. Nothing is wrong, there is just
        # nowhere for this to go, so it is noted and skipped rather than guessed at.
        logger.debug(
            "skipping a file document for %r, which this batch did not ask about",
            dataset_id,
            extra={"dataset_id": dataset_id},
        )
        return ()

    saved = save_data_access_payload(session, dataset_version_id, solr_file_access(doc))

    return saved.accesses
