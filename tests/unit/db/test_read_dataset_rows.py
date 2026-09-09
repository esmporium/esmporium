"""
Parsing a controlled search document into savable `DatasetFacets`, end to end

These tests start from tiny hand-fabricated documents we own, *not* recorded live
responses (which drift whenever they are re-recorded). Each one builds a document in a
search API's own field names out of an expected
[`DatasetFacets`][esmporium.search.result_parsing.DatasetFacets], parses it back with
[`SearchAPIFacade.read_dataset_rows`][esmporium.search.search_api_facade.SearchAPIFacade.read_dataset_rows],
and checks that (a) the rows come back exactly as expected and (b) they actually save as
`Dataset` rows.

Together with `test_dataset_facets_mirror_dataset_columns` (in `test_schema.py`) this is
the guard the plan called for: change `Dataset` without matching the parsing and either
the parity test fails by name, or the save here fails on the missing column.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlmodel import Session, select

from esmporium.db import Dataset, save_dataset
from esmporium.search import (
    ESGF1_CMIP5_FACADE_PARAMETERS,
    ESGF1_CMIP6_FACADE_PARAMETERS,
    SINGLE_ROW_DOC_PARSER,
    VARIABLE_BUNDLE_DOC_PARSER,
    DatasetFacets,
    SearchAPIESGF1Solr,
    SearchAPIFacade,
    build_transient_retrying,
)


def _facade(
    parameters, search_api_cls, doc_parser=SINGLE_ROW_DOC_PARSER
) -> SearchAPIFacade:
    """A facade for parsing a fabricated document (nothing is sent, so the host is fake)."""  # noqa: E501
    return SearchAPIFacade(
        parameters=parameters,
        search_api=search_api_cls("fabricated.example", build_transient_retrying(1)),
        doc_parser=doc_parser,
    )


def _solr_doc(facade: SearchAPIFacade, *rows: DatasetFacets) -> dict:
    """Build a Solr document (facets as single-element lists) for the expected rows.

    Every scalar facet is shared across the rows; `variable` is the multi-valued axis a
    CMIP5 bundle explodes over, so it becomes a list of each row's variable. Field names
    come from the facade's own mapping, so the document is in exactly the API names
    `read_dataset_rows` reads back. A facet the project does not model (CMIP5's
    `grid_label`) has no mapping, so it is simply omitted.
    """
    first = rows[0]
    scalar_columns = set(DatasetFacets.model_fields) - {
        "id_project_specific",
        "variable",
    }
    field_of = facade.parameters.get_mapping_to_api_facet_names(
        scalar_columns | {"variable"}
    )

    doc: dict = {}
    for column in scalar_columns:
        api_field = field_of.get(column)
        value = getattr(first, column)
        if api_field is not None and value is not None:
            doc[api_field] = [value]
    doc[field_of["variable"]] = [row.variable for row in rows]
    return doc


def test_solr_cmip6_document_parses_and_saves(engine):
    """A single CMIP6 Solr document parses to one row that round-trips the database."""
    facade = _facade(ESGF1_CMIP6_FACADE_PARAMETERS, SearchAPIESGF1Solr)
    expected = DatasetFacets(
        id_project_specific="native.cmip6.id",
        project="CMIP6",
        model="ACCESS-CM2",
        institution="CSIRO",
        experiment="historical",
        variant_label="r1i1p1f1",
        variable="tas",
        reporting_interval="mon",
        grid_label="gn",
        processing_id="Amon",
    )

    rows = facade.read_dataset_rows(
        _solr_doc(facade, expected), expected.id_project_specific
    )

    assert rows == (expected,)

    with Session(engine) as session:
        save_dataset(session, Dataset(**rows[0].model_dump()))
        session.commit()
        stored = session.exec(select(Dataset)).one()

    # Every facet we parsed is the one that came back out of the database.
    assert {
        c: getattr(stored, c) for c in DatasetFacets.model_fields
    } == expected.model_dump()


def test_solr_cmip5_bundle_explodes_into_saved_rows(engine):
    """A CMIP5 Solr bundle explodes into one row per variable, each of which saves.

    The rows share `id_project_specific` and every other facet, differ only in
    `variable`, and (CMIP5 having no grid) all carry `grid_label=None`.
    """
    facade = _facade(
        ESGF1_CMIP5_FACADE_PARAMETERS,
        SearchAPIESGF1Solr,
        doc_parser=VARIABLE_BUNDLE_DOC_PARSER,
    )
    shared = {
        "id_project_specific": "native.cmip5.id",
        "project": "CMIP5",
        "model": "ACCESS1-0",
        "institution": "CSIRO-BOM",
        "experiment": "rcp45",
        "variant_label": "r1i1p1",
        "reporting_interval": "mon",
        "grid_label": None,
        "processing_id": "Amon",
    }
    tas = DatasetFacets(**shared, variable="tas")
    pr = DatasetFacets(**shared, variable="pr")

    rows = facade.read_dataset_rows(
        _solr_doc(facade, tas, pr), shared["id_project_specific"]
    )

    assert rows == (tas, pr)
    assert all(row.grid_label is None for row in rows)

    with Session(engine) as session:
        for row in rows:
            save_dataset(session, Dataset(**row.model_dump()))
        session.commit()
        stored_variables = {d.variable for d in session.exec(select(Dataset)).all()}

    assert stored_variables == {"tas", "pr"}


def test_dataset_facets_requires_every_non_optional_facet():
    """A row missing a required facet fails loudly at construction, in `search`.

    This is the typed-model upgrade over the old bare dict: the omission is caught here,
    naming the field, rather than surfacing as a NOT NULL error at commit time. Only
    `grid_label` is optional (CMIP5 has no grid).
    """
    with pytest.raises(ValidationError):
        DatasetFacets(id_project_specific="native.id", project="CMIP6")  # rest missing
