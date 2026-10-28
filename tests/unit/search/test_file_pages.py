"""
Paging through a `type=File` answer

The file path shares its paging engine with the dataset path
([collect_all_pages][esmporium.search.collect_all_pages]), so what is tested here is
that it really does: the protections against an endpoint which loops, an answer
larger than we meant to pull, and a page which fails part way through all apply to
files too. Those are the hazards a bespoke second loop would have had to grow its own
answers to, and files are where they bite hardest -- a file-level request against a
whole batch of datasets is the largest answer we ever ask for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from esmporium.search import (
    ESGF1_CMIP6_FACADE_PARAMETERS,
    ESGFNG_CMIP6_FACADE_PARAMETERS,
    CouldNotGetSearchResponseError,
    ESGFNGResultParser,
    NotASolrFacadeError,
    PaginationLimitError,
    ParsedFileDocument,
    Request,
    SearchAPIESGF1Solr,
    SearchAPIESGFNGSTAC,
    SearchAPIFacade,
    SolrSingleRowResultParser,
    build_transient_retrying,
    collect_all_file_pages,
)

HOST = "node.example"


@dataclass(frozen=True)
class StuckSolrAPI(SearchAPIESGF1Solr):
    """
    A Solr API whose pagination never advances

    Solr pages by offset, so a real Solr endpoint cannot ask us for a page we have
    already had -- it is the token-based APIs which can get stuck. This stands in for
    an endpoint which does, which is the only way to put the shared guard under test
    from the Solr side.
    """

    def next_page_request(self, request: Request, raw: dict[str, Any]) -> Request:
        """Offer the page we have just had, forever"""
        return request


def solr_facade(host: str = HOST) -> SearchAPIFacade:
    """A CMIP6/ESGF1 facade, i.e. one with a `type=File` request to make"""
    return SearchAPIFacade(
        parameters=ESGF1_CMIP6_FACADE_PARAMETERS,
        search_api=SearchAPIESGF1Solr(host, build_transient_retrying(1)),
        result_parser=SolrSingleRowResultParser(),
    )


def stac_facade(host: str = "stac.example") -> SearchAPIFacade:
    """A CMIP6/ESGF-NG facade, which carries its files in the search response"""
    return SearchAPIFacade(
        parameters=ESGFNG_CMIP6_FACADE_PARAMETERS,
        search_api=SearchAPIESGFNGSTAC(host, build_transient_retrying(1)),
        result_parser=ESGFNGResultParser(),
    )


def file_doc(filename: str, dataset_id: str = "CMIP6.a.v1|node.example") -> dict:
    """One Solr `type=File` document"""
    return {
        "type": "File",
        "title": filename,
        "data_node": [HOST],
        "dataset_id": dataset_id,
        "url": [f"https://{HOST}/x/{filename}|application/netcdf|HTTPServer"],
    }


def page(num_found: int, *docs: dict) -> httpx.Response:
    """A Solr-shaped page reporting `num_found` matches overall"""
    return httpx.Response(
        200, json={"response": {"numFound": num_found, "docs": list(docs)}}
    )


def client_for(handler) -> httpx.Client:
    """Build an httpx client whose requests are answered by `handler`"""
    return httpx.Client(transport=httpx.MockTransport(handler))


def collect(facade: SearchAPIFacade, client: httpx.Client, *, limit: int = 2, **kwargs):
    """Page through a file request for one dataset, with the usual defaults"""
    seen: list[tuple[ParsedFileDocument, ...]] = []
    options = {
        "limit": limit,
        "max_results": None,
        "warn_on_pagination": False,
        "api_call_observer": None,
        "processor": lambda facade, parsed: seen.append(parsed),
        **kwargs,
    }

    pages = collect_all_file_pages(
        client,
        facade,
        facade.build_file_search_request(["CMIP6.a.v1|node.example"], limit),
        **options,
    )

    return pages, seen


def test_every_page_is_fetched_and_handed_over_as_it_arrives():
    """
    A batch's answer can run to several pages, and each is written as it lands

    Handing each page over immediately is what makes a failure part way through
    leave the earlier pages saved rather than thrown away.
    """
    answers = [
        page(3, file_doc("a.nc"), file_doc("b.nc")),
        page(3, file_doc("c.nc")),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return answers.pop(0)

    with client_for(handler) as client:
        pages, seen = collect(solr_facade(), client)

    assert pages.completed
    assert pages.n_matches == 3
    assert [document.payload.files[0].filename for document in pages.collected] == [
        "a.nc",
        "b.nc",
        "c.nc",
    ]
    # Two pages, handed over separately rather than at the end.
    assert [len(batch) for batch in seen] == [2, 1]


def test_an_endpoint_which_re_offers_a_page_is_refused_rather_than_paged_forever():
    """
    The protection the bespoke file loop did not have, and most needed

    A `type=File` request can match millions of documents, so an endpoint whose
    pagination does not advance is not a slow search: it is one that never ends.
    """

    facade = SearchAPIFacade(
        parameters=ESGF1_CMIP6_FACADE_PARAMETERS,
        search_api=StuckSolrAPI(HOST, build_transient_retrying(1)),
        result_parser=SolrSingleRowResultParser(),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return page(10, file_doc("stuck.nc"))

    with (
        client_for(handler) as client,
        pytest.raises(PaginationLimitError, match="would page forever"),
    ):
        collect(facade, client)


def test_a_cap_can_be_put_on_how_much_of_an_answer_we_take():
    """`max_results` applies here too, so a request cannot pull an index unbounded"""
    docs = [file_doc(f"{i}.nc") for i in range(4)]

    def handler(request: httpx.Request) -> httpx.Response:
        return page(4, *docs)

    with (
        client_for(handler) as client,
        pytest.raises(PaginationLimitError, match="max_results safety cap of 2"),
    ):
        collect(solr_facade(), client, max_results=2)


def test_a_failed_page_keeps_what_came_before_it_and_reports_why():
    """
    The failure is on the result rather than raised, so earlier pages are not lost

    Which is what lets the caller write what it did get and still hear about the
    failure.
    """
    answers = [page(3, file_doc("a.nc")), httpx.Response(500)]

    def handler(request: httpx.Request) -> httpx.Response:
        return answers.pop(0)

    with client_for(handler) as client:
        pages, _ = collect(solr_facade(), client, limit=1)

    assert not pages.completed
    assert [document.payload.files[0].filename for document in pages.collected] == [
        "a.nc"
    ]
    assert isinstance(pages.failure, CouldNotGetSearchResponseError)


def test_a_stac_facade_is_refused_before_a_request_goes_out():
    """
    ESGF-NG has no `type=File` request to make, and nothing is sent finding out

    Checked up front rather than when the first page is read, so a mistaken caller
    does not put a pointless request on a node first.
    """
    facade = stac_facade()

    def never_asked(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"unexpected request to {request.url}")

    with (
        client_for(never_asked) as client,
        pytest.raises(NotASolrFacadeError, match="per-file information"),
    ):
        collect_all_file_pages(
            client,
            facade,
            solr_facade().build_file_search_request(["CMIP6.a.v1|node.example"], 2),
            limit=2,
            max_results=None,
            warn_on_pagination=False,
            api_call_observer=None,
            processor=None,
        )
