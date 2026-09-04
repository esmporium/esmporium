"""
Round-trip test for the CMIP5 multi-variable, multi-product edge case.

This is the first test of the results->database step. It does not go near the live
search APIs or the (not-yet-written) ingestion pipeline. Instead it hand-builds the
rows that ingestion *would* produce for the hardest case we know of, saves them, and
then loads them back the way the app eventually will when a user asks to *use* the
data. The point is to prove the schema can survive the round trip before we build
anything on top of it.

The edge case (a real one, found live on ESGF):

    cmip5.*.CMCC.CMCC-CM.piControl.mon.atmos.Amon.r1i1p1
        product=output1  -> version 20121008
        product=output2  -> version 20170725   (a re-release)

The user searched two variables (`tas` + `rlut`) and no product, so the results carry
BOTH products. Two things have to hold:

1. Both products survive the save. `product` is not one of our generic facet columns,
   so the only thing keeping `output1` and `output2` apart is the native id
   (`id_project_specific`, `cmip5.output1...` vs `cmip5.output2...`). The dataset
   identity index (over every column, not just these) is what makes that work.
2. On load, a query that does NOT mention product must be able to *detect* that two
   products match, offer them as a choice ("which product?"), and resolve to exactly
   one dataset per variable once a product is chosen.

Because CMIP5 bundles many variables into one edition, `tas` and `rlut` of one product
share a single edition (`DatasetVersionSpecific`, keyed on the bundle) and a single raw
document, so neither the edition nor the JSON is duplicated per variable.
"""

from __future__ import annotations

import json

import pytest
from sqlmodel import Session, select

from esmporium.db import (
    UnhandledDatasetClashError,
    facet_differences,
    save_dataset,
)
from esmporium.db.schema import (
    Dataset,
    DatasetRawDoc,
    DatasetVersionSpecific,
    RawDocVersionLink,
)

# --- The scenario, as constants ------------------------------------------------

PROJECT = "CMIP5"
MODEL = "CMCC-CM"
INSTITUTION = "CMCC"
EXPERIMENT = "piControl"
VARIANT = "r1i1p1"
FREQUENCY = "mon"
PROCESSING = "Amon"
VARIABLES = ("tas", "rlut")
DATA_NODE = "esgf.ceda.ac.uk"

# The two products' native ids (CMIP5 `master_id`: dataset-level, no variable, but it
# DOES carry the product token as its second segment). These are the only thing that
# tells the two products apart in our schema.
MASTER = {
    "output1": "cmip5.output1.CMCC.CMCC-CM.piControl.mon.atmos.Amon.r1i1p1",
    "output2": "cmip5.output2.CMCC.CMCC-CM.piControl.mon.atmos.Amon.r1i1p1",
}
VERSION = {"output1": "20121008", "output2": "20170725"}


def _generic_facets(variable: str) -> dict[str, object]:
    """The generic (project-agnostic) facet columns for a row.

    Note there is deliberately no `product` here: product is not a facet column, so it
    cannot be passed in. It lives only in `id_project_specific`.
    """
    return {
        "project": PROJECT,
        "model": MODEL,
        "institution": INSTITUTION,
        "experiment": EXPERIMENT,
        "variant_label": VARIANT,
        "variable": variable,
        "reporting_interval": FREQUENCY,
        "grid_label": None,  # CMIP5 has no grid label
        "processing_id": PROCESSING,
    }


def _save_scenario(engine) -> None:
    """Save the four (variable x product) datasets, their editions and raw docs.

    Mirrors what ingestion would do. Versions are per *bundle* (per product edition),
    not per variable: `tas` and `rlut` of one product share ONE version row, keyed on
    the bundle's `id_project_specific`. Each product's single raw document is linked to
    that one shared version.
    """
    with Session(engine) as session:
        # Datasets first (four: two products x two variables).
        for product, master in MASTER.items():
            for variable in VARIABLES:
                save_dataset(
                    session,
                    Dataset(id_project_specific=master, **_generic_facets(variable)),
                )
        session.commit()

        # One version per product bundle, shared across its variables.
        versions: dict[str, DatasetVersionSpecific] = {}
        for product, master in MASTER.items():
            version = DatasetVersionSpecific(
                version_id=f"{master}.v{VERSION[product]}",
                id_project_specific=master,
                version=VERSION[product],
                is_latest=True,
                retracted=False,
            )
            session.add(version)
            versions[product] = version
        session.commit()

        # One raw document per product (the CMIP5 bundle lists every variable), linked
        # to that product's single shared version.
        for product, master in MASTER.items():
            instance_id = f"{master}.v{VERSION[product]}"
            raw = DatasetRawDoc(
                esgf_doc_id=f"{instance_id}|{DATA_NODE}",
                search_host="esg-dn1.nsc.liu.se",
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
            )
            session.add(raw)
            session.commit()  # assign raw.id
            session.add(
                RawDocVersionLink(
                    raw_id=raw.id, version_id=versions[product].version_id
                )
            )
        session.commit()


@pytest.fixture
def populated(engine):
    """An engine whose database holds the saved scenario."""
    _save_scenario(engine)
    return engine


# --- Disambiguation via the raw documents -------------------------------------
#
# The "which facet differs?" logic now lives in `src`
# (`esmporium.db.facet_differences`): it reads the distinguishing facet's name and
# values straight out of the stored raw JSON, so it never hard-codes `product` and
# never splits the native id on `.`. The helper below is just the plumbing that gets a
# dataset's raw document back out of the database for it.


def _raw_doc_for(session: Session, dataset: Dataset) -> dict:
    """Return the parsed raw document behind a dataset (via its bundle edition + link).

    A dataset reaches its edition by matching `id_project_specific` (the bundle), since
    editions are now per bundle rather than per variable.
    """
    version = session.exec(
        select(DatasetVersionSpecific).where(
            DatasetVersionSpecific.id_project_specific == dataset.id_project_specific
        )
    ).one()
    raw = session.exec(
        select(DatasetRawDoc)
        .join(RawDocVersionLink, RawDocVersionLink.raw_id == DatasetRawDoc.id)  # type: ignore[arg-type]
        .where(RawDocVersionLink.version_id == version.version_id)
    ).one()
    return json.loads(raw.raw_json)


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


# --- Tests --------------------------------------------------------------------


def test_save_keeps_both_products_and_both_variables(populated):
    """All four (variable x product) rows survive the save without collision."""
    with Session(populated) as session:
        rows = _query_by_generic_facets(session)

    assert len(rows) == 4
    # Two products, distinguishable only by their native id.
    assert {row.id_project_specific for row in rows} == set(MASTER.values())
    # Two variables.
    assert {row.variable for row in rows} == set(VARIABLES)


def test_identity_rejects_a_true_duplicate(populated):
    """Re-adding a dataset identical in every column is refused, loudly.

    Same native id + same variable (+ same everything) is the same dataset, and must
    not be stored twice; `save_dataset` turns the clash into a clear error.
    """
    with Session(populated) as session:
        with pytest.raises(UnhandledDatasetClashError):
            save_dataset(
                session,
                Dataset(
                    id_project_specific=MASTER["output1"], **_generic_facets("tas")
                ),
            )


def test_load_detects_the_product_clash_and_offers_a_choice(populated):
    """A product-free query surfaces the clash; the raw docs name it as `product`."""
    with Session(populated) as session:
        rows = _query_by_generic_facets(session)

        for variable in VARIABLES:
            matches = sorted(
                (row for row in rows if row.variable == variable),
                key=lambda row: row.id_project_specific,
            )
            # More than one dataset for one variable == the ambiguity that must pop up.
            assert len(matches) > 1
            # The choice offered to the user, read from the raw docs (no facet named).
            output1_row, output2_row = matches
            assert facet_differences(
                _raw_doc_for(session, output1_row),
                _raw_doc_for(session, output2_row),
                output1_row.id_project_specific,
                output2_row.id_project_specific,
            ) == {"product": ("output1", "output2")}


def test_choosing_a_product_resolves_to_one_dataset_per_variable(populated):
    """Picking `product=output2` resolves to exactly one dataset per variable."""
    chosen = "output2"
    with Session(populated) as session:
        rows = _query_by_generic_facets(session)

        for variable in VARIABLES:
            resolved = [
                row
                for row in rows
                if row.variable == variable
                and _raw_doc_for(session, row)["product"] == [chosen]
            ]
            assert len(resolved) == 1
            assert resolved[0].id_project_specific == MASTER["output2"]


def test_one_edition_and_document_are_shared_by_both_variables(populated):
    """Per-bundle: output1's tas and rlut share one edition and one raw document."""
    with Session(populated) as session:
        # The two output1 datasets (tas, rlut)...
        output1_datasets = session.exec(
            select(Dataset).where(Dataset.id_project_specific == MASTER["output1"])
        ).all()
        assert {d.variable for d in output1_datasets} == set(VARIABLES)

        # ...both reach the SAME edition (keyed on the bundle), stored once.
        versions = {
            session.exec(
                select(DatasetVersionSpecific).where(
                    DatasetVersionSpecific.id_project_specific == d.id_project_specific
                )
            )
            .one()
            .version_id
            for d in output1_datasets
        }
        assert len(versions) == 1

        # ...which is backed by exactly one raw document.
        (version_id,) = versions
        raw_ids = session.exec(
            select(RawDocVersionLink.raw_id).where(
                RawDocVersionLink.version_id == version_id
            )
        ).all()
        assert len(raw_ids) == 1


def test_each_product_is_preserved_as_a_distinct_raw_document(populated):
    """Safety net: the raw layer keeps both publications, under distinct ids."""
    with Session(populated) as session:
        raw_docs = session.exec(select(DatasetRawDoc)).all()

    esgf_doc_ids = {raw.esgf_doc_id for raw in raw_docs}
    assert len(esgf_doc_ids) == 2  # nothing silently overwritten
    assert any("output1" in doc_id for doc_id in esgf_doc_ids)
    assert any("output2" in doc_id for doc_id in esgf_doc_ids)
