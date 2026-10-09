"""
The database-backed catalogue against a migrated database

The unit tests in `tests/unit/db/test_catalogue.py` build the schema with
`create_all`, which is what our models say it should be. This checks the catalogue
against what the *migrations* actually produce, which is what a real database has.
"""

from __future__ import annotations

import json

from sqlmodel import Session

from esmporium.datasets import DATASET_FACET_COLUMNS
from esmporium.db import Availability, DatabaseCatalogue
from esmporium.db.migrate import upgrade_to_head
from esmporium.db.schema import (
    Dataset,
    DatasetRawDoc,
    DatasetVersion,
    RawDocVersionLink,
)
from esmporium.query import Query


def test_finds_rows_in_a_migrated_database(engine):
    """
    A dataset saved into a migrated database reads back as a catalogue entry

    Covers the whole read path -- the facet columns, the availability `EXISTS`, and
    `extra` from the raw document -- against the migrated schema.
    """
    upgrade_to_head(engine)

    facets = dict.fromkeys(DATASET_FACET_COLUMNS, "placeholder")
    facets.update(project="CMIP6", variable="tas", grid_label="gn")

    with Session(engine) as session:
        dataset = Dataset(id_project_specific="a-dataset", **facets)
        session.add(dataset)
        session.flush()

        version = DatasetVersion(
            dataset_id=dataset.id, version="20200101", is_latest=True, retracted=False
        )
        session.add(version)
        session.flush()

        document = DatasetRawDoc(
            esgf_doc_id="a-document",
            raw_json=json.dumps({"realm": ["atmos"], "activity_id": ["CMIP"]}),
            raw_docs_format_tag="solr",
        )
        session.add(document)
        session.flush()
        session.add(
            RawDocVersionLink(raw_doc_id=document.id, dataset_version_id=version.id)
        )
        session.commit()
        saved_id = dataset.id

    (entry,) = DatabaseCatalogue(engine).find(Query(project="CMIP6", variable="tas"))

    assert entry.id == saved_id
    assert entry.id_project_specific == "a-dataset"
    assert entry.grid_label == "gn"
    # Only the raw document knows these, so finding them proves that half works too.
    assert entry.facet("realm") == "atmos"
    assert entry.facet("activity") == "CMIP"


def test_a_retracted_dataset_is_not_available_in_a_migrated_database(engine):
    """The availability `EXISTS` subquery works against the migrated schema"""
    upgrade_to_head(engine)

    facets = dict.fromkeys(DATASET_FACET_COLUMNS, "placeholder")
    facets.update(project="CMIP6", variable="tas")

    with Session(engine) as session:
        dataset = Dataset(id_project_specific="retracted", **facets)
        session.add(dataset)
        session.flush()
        session.add(
            DatasetVersion(
                dataset_id=dataset.id,
                version="20200101",
                is_latest=True,
                retracted=True,
            )
        )
        session.commit()

    assert DatabaseCatalogue(engine).find(Query(variable="tas")) == ()
    assert (
        len(
            DatabaseCatalogue(engine, availability=Availability.ANY).find(
                Query(variable="tas")
            )
        )
        == 1
    )
