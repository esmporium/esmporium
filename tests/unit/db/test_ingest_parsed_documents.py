"""
Ingesting parsed search documents into the database

The round-trip test (`test_results_round_trip.py`) hand-builds rows to prove the schema
survives a save. This one drives the real path instead: it parses recorded search
responses with the facade, then writes them with `ingest_parsed_documents` /
`build_result_processor`, exactly as `search()` will. It stays offline -- the responses
are recordings, not live calls.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlmodel import Session, select

from esmporium.db import (
    Dataset,
    DatasetNodeInformation,
    DatasetRawDoc,
    DatasetVersion,
    RawDocVersionLink,
    build_result_processor,
    ingest_parsed_documents,
)
from esmporium.search import (
    DEFAULT_NORMALISERS,
    ESGF1_CMIP5_FACADE_PARAMETERS,
    ESGFNG_CMIP7_FACADE_PARAMETERS,
    SOLR_FORMAT_TAG,
    STAC_FORMAT_TAG,
    DataNodeInfo,
    DatasetFacets,
    ESGFNGCMIP7ResultParser,
    ParsedDocument,
    SearchAPIESGF1Solr,
    SearchAPIESGFNGSTAC,
    SearchAPIFacade,
    SolrVariableBundleResultParser,
    UnknownRawDocFormatTagError,
    build_transient_retrying,
    normalise_stored_document,
    stac_east_n_matches,
)

RECORDED_DIR = Path(__file__).parents[2] / "test-data" / "search"


def _facade(parameters, search_api_cls, result_parser) -> SearchAPIFacade:
    """Build a facade for parsing a recording (nothing is sent, so the host is fake)."""
    return SearchAPIFacade(
        parameters=parameters,
        search_api=search_api_cls("recorded.example", build_transient_retrying(1)),
        result_parser=result_parser,
    )


def _load(name: str) -> dict:
    return json.loads((RECORDED_DIR / f"{name}.json").read_text())


def _counts(session: Session) -> dict[str, int]:
    return {
        "datasets": len(session.exec(select(Dataset)).all()),
        "versions": len(session.exec(select(DatasetVersion)).all()),
        "nodes": len(session.exec(select(DatasetNodeInformation)).all()),
        "raw_docs": len(session.exec(select(DatasetRawDoc)).all()),
        "links": len(session.exec(select(RawDocVersionLink)).all()),
    }


def test_ingest_cmip5_writes_one_edition_per_dataset(engine):
    """Each CMIP5 bundle explodes into many variables, each its own dataset+edition."""
    facade = _facade(
        ESGF1_CMIP5_FACADE_PARAMETERS,
        SearchAPIESGF1Solr,
        SolrVariableBundleResultParser(),
    )
    documents = facade.parse_search_results(_load("esgf1-solr-cmip5-search"))
    expected_rows = sum(len(doc.datasets) for doc in documents)

    with Session(engine) as session:
        ingest_parsed_documents(session, documents)
        session.commit()
        counts = _counts(session)

    assert counts["datasets"] == expected_rows > len(documents)
    # A version belongs to a single dataset, so there is one edition per variable.
    assert counts["versions"] == expected_rows
    # The raw document is per bundle (per source doc), but it links to every one of that
    # bundle's per-variable editions.
    assert counts["raw_docs"] == len(documents)
    assert counts["links"] == expected_rows


def test_reingesting_the_same_documents_is_idempotent(engine):
    """A second ingest of the same response reuses rows rather than duplicating them."""
    facade = _facade(
        ESGF1_CMIP5_FACADE_PARAMETERS,
        SearchAPIESGF1Solr,
        SolrVariableBundleResultParser(),
    )
    documents = facade.parse_search_results(_load("esgf1-solr-cmip5-search"))

    with Session(engine) as session:
        ingest_parsed_documents(session, documents)
        session.commit()
        first = _counts(session)

        ingest_parsed_documents(session, documents)
        session.commit()
        second = _counts(session)

    assert first == second


def test_result_processor_commits_each_host(engine):
    """The `build_result_processor` processor persists a host's docs, committing."""
    facade = _facade(
        ESGF1_CMIP5_FACADE_PARAMETERS,
        SearchAPIESGF1Solr,
        SolrVariableBundleResultParser(),
    )
    documents = facade.parse_search_results(_load("esgf1-solr-cmip5-search"))

    with Session(engine) as session:
        processor = build_result_processor(session)
        processor("esg-dn1.nsc.liu.se", documents)

    # A brand-new session sees the rows, so the processor really committed.
    with Session(engine) as session:
        assert session.exec(select(Dataset)).first() is not None


def test_ingest_stac_cmip7_writes_one_dataset_per_document(engine):
    """A STAC CMIP7 document maps to one dataset row, ingested via the processor."""
    facade = _facade(
        ESGFNG_CMIP7_FACADE_PARAMETERS,
        SearchAPIESGFNGSTAC,
        ESGFNGCMIP7ResultParser(read_n_matches=stac_east_n_matches),
    )
    documents = facade.parse_search_results(_load("esgf-ng-stac-cmip7-east-search"))

    with Session(engine) as session:
        build_result_processor(session)("search.east.esgf.io", documents)
        rows = session.exec(select(Dataset).where(Dataset.project == "CMIP7")).all()

    assert len(rows) == len(documents)


def test_ingest_stamps_each_raw_doc_with_its_search_api_tag(engine):
    """The producing API's tag is stored on every raw doc, ready for load-time reads.

    Solr and STAC ingests are checked together so the tag really tracks the API that
    parsed the response rather than a constant.
    """
    solr = _facade(
        ESGF1_CMIP5_FACADE_PARAMETERS,
        SearchAPIESGF1Solr,
        SolrVariableBundleResultParser(),
    )
    stac = _facade(
        ESGFNG_CMIP7_FACADE_PARAMETERS,
        SearchAPIESGFNGSTAC,
        ESGFNGCMIP7ResultParser(read_n_matches=stac_east_n_matches),
    )

    with Session(engine) as session:
        ingest_parsed_documents(
            session, solr.parse_search_results(_load("esgf1-solr-cmip5-search"))
        )
        ingest_parsed_documents(
            session, stac.parse_search_results(_load("esgf-ng-stac-cmip7-east-search"))
        )
        session.commit()
        tags = {doc.search_api_tag for doc in session.exec(select(DatasetRawDoc)).all()}

    assert tags == {SOLR_FORMAT_TAG, STAC_FORMAT_TAG}


def test_bypassing_user_must_inject_a_normaliser_for_their_tag(engine):
    """A user's own search API can ingest, but its tag needs a matching normaliser.

    Our facade and search API are built so a user can bypass them with their own. When
    they do, their documents are stored under their own `search_api_tag`, and reading
    those back at load time needs the flattener for that tag: the default registry does
    not know it, so normalisation raises until the user injects their own.
    """
    custom = ParsedDocument(
        id_project_specific="my.native.id",
        datasets=(
            DatasetFacets(
                id_project_specific="my.native.id",
                project="CMIP6",
                model="M",
                institution="INST",
                experiment="historical",
                variant_label="r1i1p1f1",
                variable="tas",
                reporting_interval="mon",
                grid_label="gn",
                processing_id="Amon",
            ),
        ),
        version="20200101",
        is_latest=True,
        retracted=False,
        nodes=(DataNodeInfo("node.example"),),
        esgf_doc_id="my.native.id|node.example",
        raw_json=json.dumps({"blob": "product=output1"}),
        search_api_tag="acme-format",
    )

    with Session(engine) as session:
        ingest_parsed_documents(session, [custom])
        session.commit()
        stored = session.exec(select(DatasetRawDoc)).one()

    # The user's tag rode all the way to the row.
    assert stored.search_api_tag == "acme-format"
    raw = json.loads(stored.raw_json)

    # Load-time normalisation with the default registry cannot read an unknown tag.
    with pytest.raises(UnknownRawDocFormatTagError):
        normalise_stored_document(raw, stored.search_api_tag)

    # Injecting a flattener for that tag (alongside ours) makes it readable.
    normalisers = {
        **DEFAULT_NORMALISERS,
        "acme-format": lambda doc: dict(
            pair.split("=", 1) for pair in doc["blob"].split("|")
        ),
    }
    assert normalise_stored_document(raw, stored.search_api_tag, normalisers) == {
        "product": "output1"
    }
