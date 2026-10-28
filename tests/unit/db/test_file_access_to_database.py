"""
Filling in per-file access for Solr results, as its own second request

Nothing here touches the network: the Solr answer is served by a mock transport, and
the requests the code builds are inspected on the way past. The live equivalent is in
`tests/integration/search/test_data_access_live.py`.
"""

from __future__ import annotations

import httpx
import pytest
from sqlmodel import Session, select

from esmporium.datasets import AccessService, ContainerFormat
from esmporium.db import (
    AmbiguousFileAttributionError,
    DataAccess,
    File,
    ingest_parsed_documents,
    plan_solr_file_batches,
    record_solr_file_access,
)
from esmporium.search import (
    ESGF1_CMIP5_FACADE_PARAMETERS,
    ESGF1_CMIP6_FACADE_PARAMETERS,
    ESGFNG_CMIP6_FACADE_PARAMETERS,
    SOLR_FORMAT_TAG,
    CouldNotGetSearchResponseError,
    DataNodeInfo,
    DatasetFacets,
    ESGFNGResultParser,
    NotASolrFacadeError,
    ParsedDocument,
    SearchAPIESGF1Solr,
    SearchAPIESGFNGSTAC,
    SearchAPIFacade,
    SolrSingleRowResultParser,
    SolrVariableBundleResultParser,
    build_transient_retrying,
)

HOST = "esgf.ceda.ac.uk"


def _solr_facade(parameters, result_parser) -> SearchAPIFacade:
    return SearchAPIFacade(
        parameters=parameters,
        search_api=SearchAPIESGF1Solr(HOST, build_transient_retrying(1)),
        result_parser=result_parser,
    )


def _facets(variable: str, project: str = "CMIP6") -> DatasetFacets:
    return DatasetFacets(
        id_project_specific=f"a.dataset.{variable}",
        project=project,
        model="ACCESS-ESM1-5",
        institution="CSIRO",
        experiment="historical",
        variant_label="r1i1p1f1",
        variable=variable,
        reporting_interval="mon",
        grid_label="gn",
        processing_id="Amon",
    )


def _document(doc_id: str, *variables: str) -> ParsedDocument:
    """A parsed dataset document, bundling one version per variable."""
    return ParsedDocument(
        id_project_specific="a.dataset",
        datasets=tuple(_facets(variable) for variable in variables),
        version="20200101",
        is_latest=True,
        retracted=False,
        nodes=(DataNodeInfo(HOST),),
        esgf_doc_id=doc_id,
        raw_json="{}",
        raw_docs_format_tag=SOLR_FORMAT_TAG,
    )


def _file_doc(dataset_id: str, filename: str, variable: str = "tas") -> dict:
    """A Solr `type=File` document, shaped the way CEDA really answers."""
    return {
        "type": "File",
        "title": filename,
        "size": 1232798772,
        "checksum": [
            "981f71dc16d5e378423d88dbf4ebfc663da28da799702e0bc8168a8115142fb3"
        ],
        "checksum_type": ["SHA256"],
        "tracking_id": ["hdl:21.14100/32ed136f-697a-477c-947f-f3431fb47394"],
        "data_node": [HOST],
        "dataset_id": dataset_id,
        "variable_id": [variable],
        "url": [
            f"https://{HOST}/thredds/fileServer/x/{filename}"
            "|application/netcdf|HTTPServer",
            f"https://{HOST}/thredds/dodsC/x/{filename}.html"
            "|application/opendap-html|OPENDAP",
        ],
    }


def _answering_client(docs, seen: list[httpx.Request] | None = None) -> httpx.Client:
    """A client answering every request with `docs`, recording what it was asked."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)

        return httpx.Response(
            200, json={"response": {"numFound": len(docs), "docs": docs}}
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_batches_are_grouped_by_variable_when_the_facade_asks_for_that(engine):
    """
    A CMIP5 bundle fans out into one batch per variable, which is what makes it work

    One CMIP5 document becomes one version per variable, and the variable is the only
    thing which says which of them a file belongs to -- so asking about the whole
    bundle at once would give back files with nowhere to go.

    Which facets to group by is not this function's decision: it is read off the
    facade's result parser, and `("variable",)` is what the CMIP5 one answers.
    """
    bundle = _document("a.bundle|esgf.ceda.ac.uk", "tas", "pr", "psl")

    with Session(engine) as session:
        ingested = ingest_parsed_documents(session, [bundle])
        session.commit()

        batches = plan_solr_file_batches(ingested, ("variable",))

    assert {batch.facet_values["variable"] for batch in batches} == {
        ("tas",),
        ("pr",),
        ("psl",),
    }
    # Each batch knows which version its answers belong to, with no lookup needed.
    version_ids = {
        version_id
        for batch in batches
        for version_id in batch.dataset_version_id_by_dataset_id.values()
    }
    assert len(version_ids) == 3


def test_datasets_needing_no_narrowing_go_into_one_request(engine):
    """
    A CMIP6/CMIP7 `dataset_id` names one dataset row, so one request can carry many

    This is what the facade answering `()` buys. Grouping by variable regardless
    would put every one of these in a batch of its own, because each has a variable
    of its own -- so a page of 50 datasets would cost 50 requests instead of 1, and
    the whole point of `batch_size` would be lost.
    """
    documents = [
        _document(f"CMIP6.a{i}.v1|esgf.ceda.ac.uk", variable)
        for i, variable in enumerate(("tas", "pr", "psl", "hurs"))
    ]

    with Session(engine) as session:
        ingested = ingest_parsed_documents(session, documents)
        session.commit()

        batches = plan_solr_file_batches(ingested, ())

    assert len(batches) == 1
    assert batches[0].facet_values == {}
    assert len(batches[0].dataset_version_id_by_dataset_id) == 4


def test_narrowing_which_cannot_tell_two_versions_apart_is_refused(engine):
    """
    A bundle grouped by nothing would place every file by guessing, so it raises

    Impossible with the parsers we have -- the bundling one names `variable` -- which
    is exactly why it is checked rather than assumed. The facets come from the facade
    now, so a parser which bundles rows without saying what tells them apart is a
    mistake this catches at planning time, before a single file is misfiled.
    """
    bundle = _document("a.bundle|esgf.ceda.ac.uk", "tas", "pr")

    with Session(engine) as session:
        ingested = ingest_parsed_documents(session, [bundle])
        session.commit()

        with pytest.raises(AmbiguousFileAttributionError, match="narrowed by nothing"):
            plan_solr_file_batches(ingested, ())


def test_the_join_key_is_the_document_id_the_dataset_search_published(engine):
    """
    A File document's `dataset_id` is the dataset document's own `id`

    Which is why no database lookup is needed to place an answer: the version each
    `dataset_id` belongs to is already in hand when the request is built.
    """
    document = _document("CMIP6.a.v1|esgf.ceda.ac.uk", "tas")

    with Session(engine) as session:
        ingested = ingest_parsed_documents(session, [document])
        session.commit()

        batches = plan_solr_file_batches(ingested, ())

    assert list(batches[0].dataset_version_id_by_dataset_id) == [
        "CMIP6.a.v1|esgf.ceda.ac.uk"
    ]


def test_batches_respect_the_batch_size(engine):
    """A node-specific dataset id is long, so a request can only carry so many"""
    documents = [_document(f"CMIP6.a{i}.v1|esgf.ceda.ac.uk", "tas") for i in range(5)]

    with Session(engine) as session:
        ingested = ingest_parsed_documents(session, documents)
        session.commit()

        batches = plan_solr_file_batches(ingested, ("variable",), batch_size=2)

    assert [len(batch.dataset_version_id_by_dataset_id) for batch in batches] == [
        2,
        2,
        1,
    ]
    assert {batch.facet_values["variable"] for batch in batches} == {("tas",)}


def test_a_stac_facade_is_refused_loudly(engine):
    """
    Calling this on ESGF-NG is a mistake, not a no-op

    STAC carries its files in the search response, so its routes are already recorded
    by the time this could be called. A caller which thinks it is filling in file
    access here is not, so saying nothing would hide that.
    """
    stac = SearchAPIFacade(
        parameters=ESGFNG_CMIP6_FACADE_PARAMETERS,
        search_api=SearchAPIESGFNGSTAC(
            "search.east.esgf.io", build_transient_retrying(1)
        ),
        result_parser=ESGFNGResultParser(),
    )

    with Session(engine) as session:
        with pytest.raises(NotASolrFacadeError, match="per-file information"):
            record_solr_file_access(session, stac, [])


def test_a_recorded_shaped_answer_becomes_files_and_routes(engine):
    """The second request's answer lands as one file and one route per url triple"""
    document = _document("CMIP6.a.v1|esgf.ceda.ac.uk", "tas")
    docs = [
        _file_doc("CMIP6.a.v1|esgf.ceda.ac.uk", "tas_Amon_185001-194912.nc"),
        _file_doc("CMIP6.a.v1|esgf.ceda.ac.uk", "tas_Amon_195001-201412.nc"),
    ]

    with Session(engine) as session:
        ingested = ingest_parsed_documents(session, [document])
        facade = _solr_facade(
            ESGF1_CMIP6_FACADE_PARAMETERS, SolrSingleRowResultParser()
        )

        with _answering_client(docs) as client:
            written = record_solr_file_access(session, facade, ingested, client=client)
        session.commit()

        files = session.exec(select(File)).all()
        routes = session.exec(select(DataAccess)).all()

    assert {stored.filename for stored in files} == {
        "tas_Amon_185001-194912.nc",
        "tas_Amon_195001-201412.nc",
    }
    assert len(routes) == len(written) == 4
    # ESGF-1 cannot express a whole-version route, so every route names its file.
    assert all(route.file_id is not None for route in routes)
    assert {route.service for route in routes} == {
        AccessService.HTTPS,
        AccessService.OPENDAP,
    }
    assert {route.container_format for route in routes} == {
        ContainerFormat.NETCDF,
        ContainerFormat.HTML,
    }


def test_the_request_asks_for_file_documents_and_narrows_no_further(engine):
    """
    The request that goes out, inspected: `type=File` and the ids OR-ed

    No variable, because a CMIP6 `dataset_id` already names one dataset row, so
    narrowing by one would only make the request longer and the batches smaller.
    """
    document = _document("CMIP6.a.v1|esgf.ceda.ac.uk", "tas")
    seen: list[httpx.Request] = []

    with Session(engine) as session:
        ingested = ingest_parsed_documents(session, [document])
        facade = _solr_facade(
            ESGF1_CMIP6_FACADE_PARAMETERS, SolrSingleRowResultParser()
        )

        with _answering_client([], seen=seen) as client:
            record_solr_file_access(session, facade, ingested, client=client)

    assert len(seen) == 1
    params = httpx.URL(seen[0].url).params
    assert params["type"] == "File"
    assert params["dataset_id"] == "CMIP6.a.v1|esgf.ceda.ac.uk"
    assert "variable_id" not in params


def test_a_cmip5_request_narrows_by_the_name_cmip5_uses(engine):
    """
    A CMIP5 request does narrow, and spells the facet the way CMIP5 spells it

    The facade translates the canonical `variable` into this API's own name, which is
    why neither spelling is hard-coded anywhere outside it. For CMIP5 the narrowing
    is load-bearing rather than merely tidy: its `dataset_id` is table-grained, so
    without it the answer covers every variable in the table.
    """
    bundle = _document("cmip5.a.v1|esgf.ceda.ac.uk", "tas")
    seen: list[httpx.Request] = []

    with Session(engine) as session:
        ingested = ingest_parsed_documents(session, [bundle])
        facade = _solr_facade(
            ESGF1_CMIP5_FACADE_PARAMETERS, SolrVariableBundleResultParser()
        )

        with _answering_client([], seen=seen) as client:
            record_solr_file_access(session, facade, ingested, client=client)

    params = httpx.URL(seen[0].url).params
    assert params["variable"] == "tas"
    assert "variable_id" not in params


def test_an_answer_about_a_dataset_we_did_not_ask_about_is_skipped(engine):
    """
    A federated search can be generous, and that is not an error

    `distrib=true` means a node may answer about datasets outside the batch. There is
    nowhere for those to go, so they are noted at DEBUG and skipped rather than
    attributed to whatever version happens to be nearby.
    """
    document = _document("CMIP6.a.v1|esgf.ceda.ac.uk", "tas")
    docs = [
        _file_doc("CMIP6.a.v1|esgf.ceda.ac.uk", "asked_about.nc"),
        _file_doc("CMIP6.somebody.else.v1|esgf.ceda.ac.uk", "not_asked_about.nc"),
    ]

    with Session(engine) as session:
        ingested = ingest_parsed_documents(session, [document])
        facade = _solr_facade(
            ESGF1_CMIP6_FACADE_PARAMETERS, SolrSingleRowResultParser()
        )

        with _answering_client(docs) as client:
            record_solr_file_access(session, facade, ingested, client=client)
        session.commit()

        assert [stored.filename for stored in session.exec(select(File)).all()] == [
            "asked_about.nc"
        ]


def test_recording_the_same_answer_twice_writes_nothing_new(engine):
    """Idempotent, because it writes through the same path ESGF-NG's results take"""
    document = _document("CMIP6.a.v1|esgf.ceda.ac.uk", "tas")
    docs = [_file_doc("CMIP6.a.v1|esgf.ceda.ac.uk", "tas_Amon_185001-194912.nc")]

    with Session(engine) as session:
        ingested = ingest_parsed_documents(session, [document])
        facade = _solr_facade(
            ESGF1_CMIP6_FACADE_PARAMETERS, SolrSingleRowResultParser()
        )

        with _answering_client(docs) as client:
            record_solr_file_access(session, facade, ingested, client=client)
            session.commit()
            first = len(session.exec(select(DataAccess)).all())

            record_solr_file_access(session, facade, ingested, client=client)
            session.commit()
            second = len(session.exec(select(DataAccess)).all())

    assert first == second == 2


def test_a_batch_which_fails_part_way_keeps_what_it_wrote_and_still_raises(engine):
    """
    The failure is loud, and the pages before it are in the session either way

    `record_solr_file_access` answers with the rows it wrote, so there is nowhere on
    its answer to hang a failure -- a caller which was told nothing would believe it
    had every file. The write side is unaffected: each page is written as it arrives,
    so whatever got in before the failure is still there for a caller which commits.
    """
    document = _document("CMIP6.a.v1|esgf.ceda.ac.uk", "tas")
    answers = [
        httpx.Response(
            200,
            json={
                "response": {
                    "numFound": 2,
                    "docs": [_file_doc("CMIP6.a.v1|esgf.ceda.ac.uk", "first.nc")],
                }
            },
        ),
        httpx.Response(500),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return answers.pop(0)

    with Session(engine) as session:
        ingested = ingest_parsed_documents(session, [document])
        facade = _solr_facade(
            ESGF1_CMIP6_FACADE_PARAMETERS, SolrSingleRowResultParser()
        )

        with (
            httpx.Client(transport=httpx.MockTransport(handler)) as client,
            pytest.raises(CouldNotGetSearchResponseError),
        ):
            record_solr_file_access(session, facade, ingested, client=client, limit=1)

        session.commit()

        assert [stored.filename for stored in session.exec(select(File)).all()] == [
            "first.nc"
        ]
