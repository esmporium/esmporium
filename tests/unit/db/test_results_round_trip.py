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
   (`id_project_specific`, `cmip5.output1...` vs `cmip5.output2...`). The natural key
   `UNIQUE(id_project_specific, variable)` is what makes that work.
2. On load, a query that does NOT mention product must be able to *detect* that two
   products match, offer them as a choice ("which product?"), and resolve to exactly
   one dataset per variable once a product is chosen.

Because CMIP5 bundles many variables into one document, `tas` and `rlut` share a
single raw document. The `RawDocVersionLink` junction is what lets one document back
several per-variable versions, so the JSON is stored once.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

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
    """Save the four (variable x product) datasets, their versions and raw docs.

    Mirrors what ingestion would do: one Dataset + one version per (variable, product),
    one raw document per product (the CMIP5 bundle), and link rows joining each
    document to the two per-variable versions it describes.
    """
    with Session(engine) as session:
        # Datasets first, so the database can hand back their integer ids.
        datasets: dict[tuple[str, str], Dataset] = {}
        for product, master in MASTER.items():
            for variable in VARIABLES:
                dataset = Dataset(
                    id_project_specific=master, **_generic_facets(variable)
                )
                session.add(dataset)
                datasets[(product, variable)] = dataset
        session.commit()

        # One version per dataset. `version_id` embeds the (now known) dataset id, so
        # the two products get separate version rows even though they are the "same"
        # dataset in the generic view.
        versions: dict[tuple[str, str], DatasetVersionSpecific] = {}
        for (product, variable), dataset in datasets.items():
            version = DatasetVersionSpecific(
                version_id=f"{dataset.id}.v{VERSION[product]}",
                dataset_id=dataset.id,
                version=VERSION[product],
                is_latest=True,
                retracted=False,
            )
            session.add(version)
            versions[(product, variable)] = version
        session.commit()

        # One raw document per product. In real CMIP5 this bundle lists every variable;
        # we store it once and link it to each per-variable version below.
        raw_docs: dict[str, DatasetRawDoc] = {}
        for product, master in MASTER.items():
            instance_id = f"{master}.v{VERSION[product]}"
            raw = DatasetRawDoc(
                esgf_doc_id=f"{instance_id}|{DATA_NODE}",
                source_api="esgf1-solr",
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
            raw_docs[product] = raw
        session.commit()

        # The many-to-many: each product's single document backs BOTH variables.
        for product in MASTER:
            for variable in VARIABLES:
                session.add(
                    RawDocVersionLink(
                        raw_id=raw_docs[product].id,
                        version_id=versions[(product, variable)].version_id,
                    )
                )
        session.commit()


@pytest.fixture
def populated(engine):
    """An engine whose database holds the saved scenario."""
    _save_scenario(engine)
    return engine


# --- The generic "which facet differs?" helper --------------------------------
#
# This is the disambiguation mechanism, kept generic on purpose: it never mentions
# `product`. Given the native ids of a clashing group, it returns the values that
# differ between them. For CMIP5 those turn out to be the products; for an unknown
# CMIP6/7 clash it would return whatever token actually differs. When the load feature
# is built for real this moves into `src`; it lives here for now.


def differing_tokens(native_ids: set[str]) -> set[str]:
    """Return the dot-separated token values that differ across `native_ids`."""
    token_lists = [nid.split(".") for nid in native_ids]
    choices: set[str] = set()
    for position in range(min(len(tokens) for tokens in token_lists)):
        values = {tokens[position] for tokens in token_lists}
        if len(values) > 1:
            choices |= values
    return choices


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


def test_natural_key_rejects_a_true_duplicate(populated):
    """Re-adding the same (id_project_specific, variable) is refused.

    This is the constraint that makes the two products distinct rows rather than one:
    same native id + same variable is the same dataset, and must not be stored twice.
    """
    with Session(populated) as session:
        session.add(
            Dataset(id_project_specific=MASTER["output1"], **_generic_facets("tas"))
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_load_detects_the_product_clash_and_offers_a_choice(populated):
    """A product-free query surfaces >1 product per variable, as popup choices."""
    with Session(populated) as session:
        rows = _query_by_generic_facets(session)

    for variable in VARIABLES:
        native_ids = {
            row.id_project_specific for row in rows if row.variable == variable
        }
        # More than one native id for one variable == the ambiguity that must pop up.
        assert len(native_ids) > 1
        # The choice offered to the user, derived generically (no facet is named).
        assert differing_tokens(native_ids) == {"output1", "output2"}


def test_choosing_a_product_resolves_to_one_dataset_per_variable(populated):
    """Once the user picks `output2`, exactly one dataset remains per variable."""
    chosen = "output2"
    with Session(populated) as session:
        rows = _query_by_generic_facets(session)

    for variable in VARIABLES:
        resolved = [
            row
            for row in rows
            if row.variable == variable
            and chosen in differing_tokens({row.id_project_specific, MASTER["output1"]})
        ]
        assert len(resolved) == 1
        assert resolved[0].id_project_specific == MASTER["output2"]


def test_one_raw_document_backs_both_variables(populated):
    """The many-to-many holds: output1's single document backs tas AND rlut."""
    with Session(populated) as session:
        # The two output1 datasets (tas, rlut)...
        output1_datasets = session.exec(
            select(Dataset).where(Dataset.id_project_specific == MASTER["output1"])
        ).all()
        assert {d.variable for d in output1_datasets} == set(VARIABLES)

        raw_ids_per_variable: dict[str, set[int]] = {}
        for dataset in output1_datasets:
            version = session.exec(
                select(DatasetVersionSpecific).where(
                    DatasetVersionSpecific.dataset_id == dataset.id
                )
            ).one()
            links = session.exec(
                select(RawDocVersionLink).where(
                    RawDocVersionLink.version_id == version.version_id
                )
            ).all()
            # Each version is backed by exactly one raw document here.
            assert len(links) == 1
            raw_ids_per_variable[dataset.variable] = {link.raw_id for link in links}

    # tas and rlut point at the SAME raw document -> stored once, not per variable.
    assert raw_ids_per_variable["tas"] == raw_ids_per_variable["rlut"]


def test_each_product_is_preserved_as_a_distinct_raw_document(populated):
    """Safety net: the raw layer keeps both publications, under distinct ids."""
    with Session(populated) as session:
        raw_docs = session.exec(select(DatasetRawDoc)).all()

    esgf_doc_ids = {raw.esgf_doc_id for raw in raw_docs}
    assert len(esgf_doc_ids) == 2  # nothing silently overwritten
    assert any("output1" in doc_id for doc_id in esgf_doc_ids)
    assert any("output2" in doc_id for doc_id in esgf_doc_ids)
