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

from sqlmodel import Session, select

from esmporium.db import (
    Dataset,
    DatasetNodeInformation,
    DatasetRawDoc,
    DatasetVersionSpecific,
    RawDocVersionLink,
    build_result_processor,
    ingest_parsed_documents,
)
from esmporium.search import (
    ESGF1_CMIP5_FACADE_PARAMETERS,
    ESGFNG_CMIP7_FACADE_PARAMETERS,
    SearchAPIESGF1Solr,
    SearchAPIESGFNGSTAC,
    SearchAPIFacade,
    build_transient_retrying,
)

RECORDED_DIR = Path(__file__).parents[2] / "test-data" / "search"


def _facade(parameters, search_api_cls) -> SearchAPIFacade:
    """Build a facade for parsing a recording (nothing is sent, so the host is fake)."""
    return SearchAPIFacade(
        parameters=parameters,
        search_api=search_api_cls("recorded.example", build_transient_retrying(1)),
    )


def _load(name: str) -> dict:
    return json.loads((RECORDED_DIR / f"{name}.json").read_text())


def _counts(session: Session) -> dict[str, int]:
    return {
        "datasets": len(session.exec(select(Dataset)).all()),
        "versions": len(session.exec(select(DatasetVersionSpecific)).all()),
        "nodes": len(session.exec(select(DatasetNodeInformation)).all()),
        "raw_docs": len(session.exec(select(DatasetRawDoc)).all()),
        "links": len(session.exec(select(RawDocVersionLink)).all()),
    }


def test_ingest_cmip5_shares_one_edition_per_bundle(engine):
    """The two CMIP5 bundles each explode into many variables under a single edition."""
    facade = _facade(ESGF1_CMIP5_FACADE_PARAMETERS, SearchAPIESGF1Solr)
    documents = facade.parse_search_results(_load("esgf1-solr-cmip5-search"))
    expected_rows = sum(len(doc.datasets) for doc in documents)

    with Session(engine) as session:
        ingest_parsed_documents(session, "esg-dn1.nsc.liu.se", documents)
        session.commit()
        counts = _counts(session)

    assert counts["datasets"] == expected_rows > len(documents)
    # One edition and one raw document per bundle (per source doc), not per variable.
    assert counts["versions"] == len(documents)
    assert counts["raw_docs"] == len(documents)
    assert counts["links"] == len(documents)


def test_reingesting_the_same_documents_is_idempotent(engine):
    """A second ingest of the same response reuses rows rather than duplicating them."""
    facade = _facade(ESGF1_CMIP5_FACADE_PARAMETERS, SearchAPIESGF1Solr)
    documents = facade.parse_search_results(_load("esgf1-solr-cmip5-search"))

    with Session(engine) as session:
        ingest_parsed_documents(session, "esg-dn1.nsc.liu.se", documents)
        session.commit()
        first = _counts(session)

        ingest_parsed_documents(session, "esg-dn1.nsc.liu.se", documents)
        session.commit()
        second = _counts(session)

    assert first == second


def test_result_processor_commits_each_host(engine):
    """The `build_result_processor` processor persists a host's docs, committing."""
    facade = _facade(ESGF1_CMIP5_FACADE_PARAMETERS, SearchAPIESGF1Solr)
    documents = facade.parse_search_results(_load("esgf1-solr-cmip5-search"))

    with Session(engine) as session:
        processor = build_result_processor(session)
        processor("esg-dn1.nsc.liu.se", documents)

    # A brand-new session sees the rows, so the processor really committed.
    with Session(engine) as session:
        assert session.exec(select(Dataset)).first() is not None


def test_ingest_stac_cmip7_writes_one_dataset_per_document(engine):
    """A STAC CMIP7 document maps to one dataset row, ingested via the processor."""
    facade = _facade(ESGFNG_CMIP7_FACADE_PARAMETERS, SearchAPIESGFNGSTAC)
    documents = facade.parse_search_results(_load("esgf-ng-stac-cmip7-east-search"))

    with Session(engine) as session:
        build_result_processor(session)("search.east.esgf.io", documents)
        rows = session.exec(select(Dataset).where(Dataset.project == "CMIP7")).all()

    assert len(rows) == len(documents)
