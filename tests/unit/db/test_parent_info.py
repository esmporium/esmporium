"""
Reading a CMIP7 record's declared parent, and recording it

The parsing tests go through the real recorded ESGF-NG responses rather than
hand-built features, because the shape of those responses is the thing most likely to
be misread. The one hand-built feature is the case ESGF gives us no recording of: a
record which declares no parent by carrying none of the keys.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import pytest
from sqlmodel import Session, select

from esmporium.db import (
    PARENT_FACT_COLUMNS,
    DatasetVersionParent,
    ParentFactsChangedWarning,
    ParentInfoSource,
    ParentResolution,
    ingest_parsed_documents,
    parent_fact_differences,
    save_parent_info,
)
from esmporium.search import (
    INBUILT_SEARCH_API_FACADE_STORE,
    ParentInfo,
    stac_parent_info,
)

RECORDED_DIR = Path(__file__).parents[2] / "test-data" / "search"


def recorded_documents(name: str, project: str, host: str):
    """Parse one recorded response through the facade that really serves it."""
    facade = INBUILT_SEARCH_API_FACADE_STORE.get_api_facade_for_project_from_host(
        project, host
    )
    raw = json.loads((RECORDED_DIR / name).read_text())

    return facade.parse_search_results(raw)


A_PARENT = ParentInfo(
    parent_mip_era="CMIP7",
    parent_activity_id="CMIP",
    parent_model="UKESM1-3-LL",
    parent_experiment="piControl",
    parent_variant_label="r1i1p1f1",
    parent_time_units="days since 1850-01-01",
)

NO_PARENT = ParentInfo(
    parent_mip_era=None,
    parent_activity_id=None,
    parent_model=None,
    parent_experiment=None,
    parent_variant_label=None,
    parent_time_units=None,
)


@pytest.mark.parametrize(
    ("name", "host", "expected_experiment", "expected_model"),
    (
        pytest.param(
            "esgf-ng-stac-cmip7-east-search.json",
            "search.east.esgf.io",
            "esm-hist",
            "UKESM1-3-LL",
            id="east",
        ),
        pytest.param(
            "esgf-ng-stac-cmip7-west-search.json",
            "search.west.esgf.io",
            "piControl",
            "CanESM5-1",
            id="west",
        ),
    ),
)
def test_cmip7_stac_declares_its_parent(
    name, host, expected_experiment, expected_model
):
    """Every CMIP7 ESGF-NG document carries the parent its record published."""
    documents = recorded_documents(name, "CMIP7", host)

    assert documents
    for document in documents:
        assert document.parent is not None
        assert document.parent.declares_a_parent
        assert document.parent.parent_experiment == expected_experiment
        assert document.parent.parent_model == expected_model
        assert document.parent.parent_mip_era == "CMIP7"


@pytest.mark.parametrize(
    ("name", "project", "host"),
    (
        pytest.param(
            "esgf-ng-stac-cmip6-east-search.json",
            "CMIP6",
            "search.east.esgf.io",
            id="cmip6-stac",
        ),
        pytest.param(
            "esgf1-solr-cmip6-search.json", "CMIP6", "esgf.nci.org.au", id="cmip6-solr"
        ),
        pytest.param(
            "esgf1-solr-cmip5-search.json", "CMIP5", "esgf.nci.org.au", id="cmip5-solr"
        ),
    ),
)
def test_a_project_without_published_parents_reads_none(name, project, host):
    """
    A project which publishes no parent facts reports nothing looked, not "no parent"

    The difference matters: an all-`None` `ParentInfo` would claim every CMIP6 dataset
    in existence is a root of the parent tree.
    """
    for document in recorded_documents(name, project, host):
        assert document.parent is None


def test_a_record_carrying_no_parent_keys_declares_no_parent():
    """
    CMIP7 says "this has no parent" by carrying none of the keys, which `amip` does

    There is no sentinel value to look for: a literal `parent_experiment_id` of
    `"no parent"` is a CMIP6 convention and does not occur in CMIP7.
    """
    amip_shaped = {
        "id": "MIP-DRS7.CMIP7.CMIP.UKNCSP.UKCM2a-0-HH.amip.r1i1p1f1.glb.mon.zg.v1",
        "properties": {
            "cmip7:experiment_id": "amip",
            "cmip7:source_id": "UKCM2a-0-HH",
            "cmip7:variant_label": "r1i1p1f1",
        },
    }

    parent = stac_parent_info(amip_shaped)

    assert not parent.declares_a_parent
    assert parent == NO_PARENT


def test_ingesting_a_cmip7_document_records_its_parent(engine):
    """A parent row lands for every version a CMIP7 document described."""
    documents = recorded_documents(
        "esgf-ng-stac-cmip7-east-search.json", "CMIP7", "search.east.esgf.io"
    )

    with Session(engine) as session:
        ingested = ingest_parsed_documents(session, documents)
        session.commit()

        assert [len(one.parent_info) for one in ingested] == [
            len(one.versions) for one in ingested
        ]
        rows = session.exec(select(DatasetVersionParent)).all()
        assert len(rows) == sum(len(one.versions) for one in ingested)
        for row in rows:
            assert row.source == ParentInfoSource.SEARCH_METADATA
            assert row.resolution == ParentResolution.UNRESOLVED
            assert row.parent_experiment == "esm-hist"
            # Provenance: which document said so, not merely which version it is about.
            assert row.raw_doc_id is not None


def test_ingesting_a_project_without_parents_records_nothing(engine):
    """A CMIP6 ingest leaves the parent table alone."""
    documents = recorded_documents(
        "esgf-ng-stac-cmip6-east-search.json", "CMIP6", "search.east.esgf.io"
    )

    with Session(engine) as session:
        ingested = ingest_parsed_documents(session, documents)
        session.commit()

        assert all(one.parent_info == () for one in ingested)
        assert session.exec(select(DatasetVersionParent)).all() == []


def test_re_ingesting_the_same_document_reuses_the_row(engine):
    """Searching again tomorrow does not duplicate what we already hold."""
    documents = recorded_documents(
        "esgf-ng-stac-cmip7-east-search.json", "CMIP7", "search.east.esgf.io"
    )

    with Session(engine) as session:
        ingest_parsed_documents(session, documents)
        session.commit()
        before = len(session.exec(select(DatasetVersionParent)).all())

        ingest_parsed_documents(session, documents)
        session.commit()

        assert len(session.exec(select(DatasetVersionParent)).all()) == before


def test_a_silent_source_does_not_erase_what_we_hold(engine, a_dataset_version):
    """
    Silence is silence, in either direction

    This is the rule that keeps the two search APIs from overwriting one another:
    they do not publish the same set of parent facts for the same project, so one
    saying nothing about a fact is not a claim that the fact is nothing.
    """
    with Session(engine) as session:
        version_id = a_dataset_version(session)
        save_parent_info(
            session, version_id, A_PARENT, ParentInfoSource.SEARCH_METADATA
        )
        session.commit()

        partial = ParentInfo(
            parent_mip_era="CMIP7",
            parent_activity_id=None,
            parent_model="UKESM1-3-LL",
            parent_experiment="piControl",
            parent_variant_label="r1i1p1f1",
            parent_time_units=None,
        )
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            stored = save_parent_info(
                session, version_id, partial, ParentInfoSource.SEARCH_METADATA
            )
        session.commit()

        assert stored.parent_activity_id == "CMIP"
        assert stored.parent_time_units == "days since 1850-01-01"


def test_a_disagreeing_source_warns_and_keeps_the_newer_value(
    engine, a_dataset_version
):
    """A real change of parent is kept, and said out loud."""
    with Session(engine) as session:
        version_id = a_dataset_version(session)
        save_parent_info(
            session, version_id, A_PARENT, ParentInfoSource.SEARCH_METADATA
        )
        session.commit()

        moved = ParentInfo(
            parent_mip_era="CMIP7",
            parent_activity_id="CMIP",
            parent_model="UKESM1-3-LL",
            parent_experiment="esm-piControl",
            parent_variant_label="r1i1p1f1",
            parent_time_units="days since 1850-01-01",
        )
        with pytest.warns(ParentFactsChangedWarning, match="esm-piControl"):
            stored = save_parent_info(
                session, version_id, moved, ParentInfoSource.SEARCH_METADATA
            )
        session.commit()

        assert stored.parent_experiment == "esm-piControl"


def test_two_sources_each_get_their_own_row(engine, a_dataset_version):
    """
    A header read or a fix does not overwrite what the index published

    Holding both is the whole reason `source` is part of this table's identity: which
    one to believe differs by project, and that is a decision for whoever reads.
    """
    with Session(engine) as session:
        version_id = a_dataset_version(session)
        save_parent_info(
            session, version_id, A_PARENT, ParentInfoSource.SEARCH_METADATA
        )
        corrected = ParentInfo(
            parent_mip_era="CMIP7",
            parent_activity_id="CMIP",
            parent_model="UKESM1-3-LL",
            parent_experiment="esm-piControl",
            parent_variant_label="r9i1p1f1",
            parent_time_units="days since 1850-01-01",
        )
        save_parent_info(session, version_id, corrected, ParentInfoSource.FIX)
        session.commit()

        rows = session.exec(
            select(DatasetVersionParent).order_by(DatasetVersionParent.source)
        ).all()
        assert [row.source for row in rows] == [
            ParentInfoSource.FIX,
            ParentInfoSource.SEARCH_METADATA,
        ]
        assert {row.parent_variant_label for row in rows} == {"r1i1p1f1", "r9i1p1f1"}


def test_a_record_declaring_no_parent_is_recorded_as_a_root(engine, a_dataset_version):
    """
    "Declares no parent" is written down, so it is not re-examined forever

    Leaving no row would make a root of the tree indistinguishable from a version
    nobody has looked at.
    """
    with Session(engine) as session:
        version_id = a_dataset_version(session)
        stored = save_parent_info(
            session, version_id, NO_PARENT, ParentInfoSource.SEARCH_METADATA
        )
        session.commit()

        assert stored.resolution == ParentResolution.NO_PARENT_DECLARED
        assert all(getattr(stored, column) is None for column in PARENT_FACT_COLUMNS)


def test_parent_fact_differences_ignores_silence():
    """Only a real disagreement counts, which is what the merge rule rests on."""
    held = DatasetVersionParent(
        dataset_version_id=1,
        source=ParentInfoSource.SEARCH_METADATA,
        resolution=ParentResolution.UNRESOLVED,
        **{column: getattr(A_PARENT, column) for column in PARENT_FACT_COLUMNS},
    )

    assert parent_fact_differences(held, A_PARENT) == {}
    assert parent_fact_differences(held, NO_PARENT) == {}
    assert parent_fact_differences(
        held,
        ParentInfo(
            parent_mip_era=None,
            parent_activity_id=None,
            parent_model=None,
            parent_experiment="amip",
            parent_variant_label=None,
            parent_time_units=None,
        ),
    ) == {"parent_experiment": ("piControl", "amip")}
