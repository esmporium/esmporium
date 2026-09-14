"""
Identify a facet clash from documents already stored in the database.

Use the known edge case of CMIP5 product clash (unique dataset rows on
id_project_specific):

    cmip5.*.CMCC.CMCC-CM.piControl.mon.atmos.Amon.r1i1p1
        product=output1  -> version 20121008
        product=output2  -> version 20170725   (a re-release)
"""

from __future__ import annotations

import json

import pytest
from sqlmodel import Session, select

from esmporium.db import (
    Dataset,
    DatasetRawDoc,
    DatasetVersion,
    RawDocVersionLink,
    facet_differences,
    ingest_parsed_documents,
)
from esmporium.search import (
    SOLR_FORMAT_TAG,
    DataNodeInfo,
    DatasetFacets,
    ParsedDocument,
    normalise_stored_document,
)

PROJECT = "CMIP5"
MODEL = "CMCC-CM"
INSTITUTION = "CMCC"
EXPERIMENT = "piControl"
VARIANT = "r1i1p1"
FREQUENCY = "mon"
PROCESSING = "Amon"
VARIABLES = ("tas", "rlut")
DATA_NODE = "esgf.ceda.ac.uk"

# id_proejct_specific, varying by product
MASTER = {
    "output1": "cmip5.output1.CMCC.CMCC-CM.piControl.mon.atmos.Amon.r1i1p1",
    "output2": "cmip5.output2.CMCC.CMCC-CM.piControl.mon.atmos.Amon.r1i1p1",
}
VERSION = {"output1": "20121008", "output2": "20170725"}


def _bundle_document(product: str) -> ParsedDocument:
    """One CMIP5 raw document per product: one dataset row per variable."""
    master = MASTER[product]
    instance_id = f"{master}.v{VERSION[product]}"
    return ParsedDocument(
        id_project_specific=master,
        datasets=tuple(
            DatasetFacets(
                id_project_specific=master,
                project=PROJECT,
                model=MODEL,
                institution=INSTITUTION,
                experiment=EXPERIMENT,
                variant_label=VARIANT,
                variable=variable,
                reporting_interval=FREQUENCY,
                grid_label=None,  # CMIP5 has no grid label
                processing_id=PROCESSING,
            )
            for variable in VARIABLES
        ),
        version=VERSION[product],
        is_latest=True,
        retracted=False,
        nodes=(DataNodeInfo(DATA_NODE),),
        esgf_doc_id=f"{instance_id}|{DATA_NODE}",
        raw_json=json.dumps(
            {
                "instance_id": instance_id,
                "master_id": master,
                "product": [product],
                "variable": list(VARIABLES),
                "version": VERSION[product],
                "data_node": DATA_NODE,
            }
        ),
        raw_docs_format_tag=SOLR_FORMAT_TAG,
    )


@pytest.fixture
def populated(engine):
    """An engine whose database holds both products, written by the real ingest path."""
    with Session(engine) as session:
        ingest_parsed_documents(
            session, [_bundle_document("output1"), _bundle_document("output2")]
        )
        session.commit()
    return engine


def _query_by_generic_facets(session: Session) -> list[Dataset]:
    """Load datasets matching the user's query, which does NOT mention product."""
    statement = select(Dataset).where(
        Dataset.project == PROJECT,
        Dataset.model == MODEL,
        Dataset.experiment == EXPERIMENT,
        Dataset.variant_label == VARIANT,
        Dataset.reporting_interval == FREQUENCY,
        Dataset.processing_id == PROCESSING,
        Dataset.variable.in_(VARIABLES),  # type: ignore[attr-defined]
    )
    return list(session.exec(statement).all())


def _raw_doc_for(session: Session, dataset: Dataset) -> DatasetRawDoc:
    """Return the raw-document row behind a dataset (via its version + link)."""
    version = session.exec(
        select(DatasetVersion).where(DatasetVersion.dataset_id == dataset.id)
    ).one()
    return session.exec(
        select(DatasetRawDoc)
        .join(RawDocVersionLink, RawDocVersionLink.raw_doc_id == DatasetRawDoc.id)  # type: ignore[arg-type]
        .where(RawDocVersionLink.dataset_version_id == version.id)
    ).one()


def test_product_free_query_surfaces_the_clash_from_stored_raw_docs(populated):
    """A product-free query detects the two products, named from the stored raw docs.

    For each variable, more than one dataset matches; normalising each match's stored
    raw document (dispatching on its `raw_docs_format_tag`) and diffing by `Dataset.id`
    surfaces `product` as the id-linked facet a higher layer would offer as the choice.
    """
    with Session(populated) as session:
        rows = _query_by_generic_facets(session)

        for variable in VARIABLES:
            matches = sorted(
                (row for row in rows if row.variable == variable),
                key=lambda row: row.id_project_specific,
            )
            # More than one dataset for one variable == the ambiguity that must pop up.
            assert len(matches) > 1

            output1_row, output2_row = matches
            normalised_info = [
                (
                    row.id,
                    normalise_stored_document(
                        json.loads(raw.raw_json), raw.raw_docs_format_tag
                    ),
                )
                for row in matches
                for raw in (_raw_doc_for(session, row),)
            ]
            differences = facet_differences(tuple(normalised_info))

            # `product` is the id-linked facet a higher layer would surface to the user.
            assert differences["product"] == {
                output1_row.id: "output1",
                output2_row.id: "output2",
            }
            # The bottom layer also reports id-noise (version, master/instance id) that
            # differs too; filtering those to the id-linked facet is the higher layer's
            # job, deferred to the clash-resolution wrapper (a later PR).
            assert "version" in differences
