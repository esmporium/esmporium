"""
Ingesting parsed search documents into the database
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from sqlmodel import Session, create_engine, select

from esmporium.db import (
    METADATA,
    DataNode,
    Dataset,
    DatasetRawDoc,
    DatasetVersion,
    RawDocVersionLink,
    UnhandledDatasetClashError,
    build_result_processor,
    configure_sqlite_for_concurrency,
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
    ESGFNGResultParser,
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
        "nodes": len(session.exec(select(DataNode)).all()),
        "raw_docs": len(session.exec(select(DatasetRawDoc)).all()),
        "links": len(session.exec(select(RawDocVersionLink)).all()),
    }


def test_ingest_cmip5_writes_one_version_per_dataset(engine):
    """Each CMIP5 bundle explodes into many variables, each its own dataset+version."""
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
    # A version belongs to a single dataset, so there is one version per variable.
    assert counts["versions"] == expected_rows
    # The raw document is per bundle (per source doc), but it links to every one of that
    # bundle's per-variable versions.
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


def test_ingest_propagates_a_dataset_clash(engine):
    """A dataset clash surfacing mid-ingest is raised, not swallowed on the write path."""  # noqa E508

    def cmip5_document(grid_label: str | None, esgf_doc_id: str) -> ParsedDocument:
        return ParsedDocument(
            id_project_specific="cmip5.output1.native.id",
            datasets=(
                DatasetFacets(
                    id_project_specific="cmip5.output1.native.id",
                    project="CMIP5",
                    model="ACCESS1-0",
                    institution="CSIRO-BOM",
                    experiment="historical",
                    variant_label="r1i1p1",
                    variable="tas",
                    reporting_interval="mon",
                    grid_label=grid_label,
                    processing_id="Amon",
                ),
            ),
            version="20110101",
            is_latest=True,
            retracted=False,
            nodes=(DataNodeInfo("node.example"),),
            esgf_doc_id=esgf_doc_id,
            raw_json="{}",
            raw_docs_format_tag=SOLR_FORMAT_TAG,
        )

    # Distinct esgf_doc_ids, so if the clash somehow did not fire on the dataset it
    # would not be masked by a raw-doc collision instead.
    clashing = [
        cmip5_document(None, "cmip5.output1.native.id|node.a"),
        cmip5_document("", "cmip5.output1.native.id|node.b"),
    ]

    with Session(engine) as session:
        with pytest.raises(UnhandledDatasetClashError) as excinfo:
            ingest_parsed_documents(session, clashing)

    # The raw docs are passed through and found for the stored dataset
    # (despite its grid_label being NULL rather than ''),
    # but they are identical so no facet explains the clash.
    assert excinfo.value.differences == {}


@pytest.mark.parametrize(
    "ingest",
    (
        pytest.param(ingest_parsed_documents, id="ingest_parsed_documents"),
        pytest.param(
            lambda session, documents, normalisers: build_result_processor(
                session, normalisers
            )("node.example", tuple(documents)),
            id="build_result_processor",
        ),
    ),
)
def test_injected_normalisers_reach_clash_diagnosis(engine, ingest):
    """
    A user's normalisers, passed to an ingest entry point, are used to diagnose a clash

    Without them, documents with the user's own `raw_docs_format_tag`
    could not be normalised, so the clash could not say which facets differ.
    """

    def custom_document(grid_label: str | None, driving_model: str) -> ParsedDocument:
        return ParsedDocument(
            id_project_specific="my.native.id",
            datasets=(
                DatasetFacets(
                    id_project_specific="my.native.id",
                    project="CORDEX",
                    model="M",
                    institution="INST",
                    experiment="historical",
                    variant_label="r1i1p1f1",
                    variable="tas",
                    reporting_interval="mon",
                    grid_label=grid_label,
                    processing_id="Amon",
                ),
            ),
            version="20200101",
            is_latest=True,
            retracted=False,
            nodes=(DataNodeInfo("node.example"),),
            esgf_doc_id=f"my.native.id|{driving_model}",
            raw_json=json.dumps({"blob": f"driving_model={driving_model}"}),
            raw_docs_format_tag="acme-format",
        )

    # NULL vs '' grid_label gets past the get-or-create lookup
    # but clashes on the identity index.
    clashing = [custom_document(None, "MPI-ESM"), custom_document("", "CNRM-CM6")]
    normalisers = {
        **DEFAULT_NORMALISERS,
        "acme-format": lambda doc: dict([doc["blob"].split("=", 1)]),
    }

    with Session(engine) as session:
        with pytest.raises(UnhandledDatasetClashError) as excinfo:
            ingest(session, clashing, normalisers)

    assert excinfo.value.differences == {
        "driving_model": {
            "stored: my.native.id|MPI-ESM": "MPI-ESM",
            "new: my.native.id|CNRM-CM6": "CNRM-CM6",
        }
    }


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
    facade = _facade(
        ESGFNG_CMIP7_FACADE_PARAMETERS,
        SearchAPIESGFNGSTAC,
        ESGFNGResultParser(read_n_matches=stac_east_n_matches),
    )
    documents = facade.parse_search_results(_load("esgf-ng-stac-cmip7-east-search"))

    with Session(engine) as session:
        build_result_processor(session)("search.east.esgf.io", documents)
        rows = session.exec(select(Dataset).where(Dataset.project == "CMIP7")).all()

    assert len(rows) == len(documents)


def test_ingest_stamps_each_raw_doc_with_its_raw_docs_format_tag(engine):
    """The producing API's tag is stored on every raw doc.

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
        ESGFNGResultParser(read_n_matches=stac_east_n_matches),
    )

    with Session(engine) as session:
        ingest_parsed_documents(
            session, solr.parse_search_results(_load("esgf1-solr-cmip5-search"))
        )
        ingest_parsed_documents(
            session, stac.parse_search_results(_load("esgf-ng-stac-cmip7-east-search"))
        )
        session.commit()
        tags = {
            doc.raw_docs_format_tag for doc in session.exec(select(DatasetRawDoc)).all()
        }

    assert tags == {SOLR_FORMAT_TAG, STAC_FORMAT_TAG}


def test_bypassing_user_must_inject_a_normaliser_for_their_tag(engine):
    """A user's own search API can ingest, but its tag needs a matching normaliser.

    Our facade and search API are built so a user can bypass them with their own. When
    they do, their documents are stored under their own `raw_docs_format_tag`, and
    reading those back at load time needs the flattener for that tag: the default
    registry does not know it, so normalisation raises until the user injects
    their own.
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
        raw_docs_format_tag="acme-format",
    )

    with Session(engine) as session:
        ingest_parsed_documents(session, [custom])
        session.commit()
        stored = session.exec(select(DatasetRawDoc)).one()

    # The user's tag rode all the way to the row.
    assert stored.raw_docs_format_tag == "acme-format"
    raw = json.loads(stored.raw_json)

    # Load-time normalisation with the default registry cannot read an unknown tag.
    with pytest.raises(UnknownRawDocFormatTagError):
        normalise_stored_document(raw, stored.raw_docs_format_tag)

    # Injecting a flattener for that tag (alongside ours) makes it readable.
    normalisers = {
        **DEFAULT_NORMALISERS,
        "acme-format": lambda doc: dict(
            pair.split("=", 1) for pair in doc["blob"].split("|")
        ),
    }
    assert normalise_stored_document(raw, stored.raw_docs_format_tag, normalisers) == {
        "product": "output1"
    }


def _same_cmip6_document(
    esgf_doc_id: str = "CMIP6.same|node.example",
) -> ParsedDocument:
    """One CMIP6 document that two workers can each try to ingest and clash on."""
    return ParsedDocument(
        id_project_specific="CMIP6.same",
        datasets=(
            DatasetFacets(
                id_project_specific="CMIP6.same",
                project="CMIP6",
                model="ACCESS",
                institution="CSIRO",
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
        esgf_doc_id=esgf_doc_id,
        raw_json="{}",
        raw_docs_format_tag=SOLR_FORMAT_TAG,
    )


def test_concurrent_workers_ingesting_the_same_dataset_reuse_one_row(tmp_path):
    """
    Two parallel workers ingesting the *same* dataset keep one row, without erroring

    This is the parallel-search case the get-or-create must survive: two overlapping
    sub-queries return the same dataset, and their workers (each its own session, its
    own connection to one WAL SQLite database) race to write it. Each worker reads first
    (finding nothing) and only then, past the barrier, ingests -- so both see "no such
    dataset" and both try to insert it, exactly the race a single-threaded re-ingest
    never hits. The loser must reuse the winner's row (idempotent), not raise
    `UnhandledDatasetClashError`: it is the same dataset, not two our model cannot tell
    apart.
    """
    engine = configure_sqlite_for_concurrency(
        create_engine(f"sqlite:///{tmp_path / 'esmporium.db'}")
    )
    METADATA.create_all(engine)

    document = _same_cmip6_document()
    both_have_read = threading.Barrier(2, timeout=5)
    errors: list[Exception] = []
    errors_lock = threading.Lock()

    def worker() -> None:
        try:
            with Session(engine) as session:
                # Fix this worker's read snapshot before anyone commits: both workers
                # see no dataset yet, so both go on to insert and genuinely race.
                session.exec(select(Dataset)).all()
                both_have_read.wait()
                ingest_parsed_documents(session, [document])
                session.commit()
        except Exception as exc:  # recorded so the assertion can surface it
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    try:
        assert errors == [], f"a worker failed on the race: {errors!r}"
        with Session(engine) as session:
            counts = _counts(session)
        # The whole document collapses to one of each row, exactly as a serial re-ingest
        # would leave it.
        assert counts == {
            "datasets": 1,
            "versions": 1,
            "nodes": 1,
            "raw_docs": 1,
            "links": 1,
        }
    finally:
        engine.dispose()


def test_concurrent_workers_sharing_a_data_node_keep_one_node(tmp_path):
    """
    Two workers writing *different* datasets that share a data node keep one node row

    This is the race in the get-or-create helpers below the dataset: the workers insert
    their own (distinct) datasets fine, but both also try to insert the one shared
    `DataNode`. As with the dataset, the loser must reuse the node the winner committed
    rather than fail on its uniqueness constraint.
    """
    engine = configure_sqlite_for_concurrency(
        create_engine(f"sqlite:///{tmp_path / 'esmporium.db'}")
    )
    METADATA.create_all(engine)

    def document(variable: str, esgf_doc_id: str) -> ParsedDocument:
        # Same node on purpose; the datasets differ only by variable.
        base = _same_cmip6_document(esgf_doc_id)
        (facets,) = base.datasets
        return ParsedDocument(
            id_project_specific=f"CMIP6.{variable}",
            datasets=(facets.model_copy(update={"variable": variable}),),
            version=base.version,
            is_latest=base.is_latest,
            retracted=base.retracted,
            nodes=base.nodes,
            esgf_doc_id=esgf_doc_id,
            raw_json=base.raw_json,
            raw_docs_format_tag=base.raw_docs_format_tag,
        )

    documents = {
        "tas": document("tas", "CMIP6.tas|node.example"),
        "pr": document("pr", "CMIP6.pr|node.example"),
    }
    both_have_read = threading.Barrier(2, timeout=5)
    errors: list[Exception] = []
    errors_lock = threading.Lock()

    def worker(variable: str) -> None:
        try:
            with Session(engine) as session:
                session.exec(select(DataNode)).all()
                both_have_read.wait()
                ingest_parsed_documents(session, [documents[variable]])
                session.commit()
        except Exception as exc:  # recorded so the assertion can surface it
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(v,)) for v in documents]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    try:
        assert errors == [], f"a worker failed on the race: {errors!r}"
        with Session(engine) as session:
            counts = _counts(session)
        assert counts["datasets"] == 2
        assert counts["nodes"] == 1
    finally:
        engine.dispose()
