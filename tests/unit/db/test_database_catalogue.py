"""
Tests of the database-backed catalogue

The headline is `test_solving_against_the_database_matches_the_in_memory_catalogue`:
the two implementations of [`Catalogue`][esmporium.requirements.Catalogue] have to give
the same answers, because that is the promise which lets everything in
`esmporium.requirements` be written and tested against the in-memory one.
"""

from __future__ import annotations

import json

import pytest
from sqlmodel import Session

from esmporium.db import (
    Availability,
    DatabaseCatalogue,
    UnknownAvailabilityError,
    facet_filters,
    find_datasets,
    to_catalogue_entry,
)
from esmporium.db.schema import (
    Dataset,
    DatasetRawDoc,
    DatasetVersion,
    RawDocVersionLink,
)
from esmporium.query import ClashingFacetsError, Query, QueryCMIP5, QueryCMIP6
from esmporium.requirements import (
    Catalogue,
    CatalogueEntry,
    InMemoryCatalogue,
    UnrecordedFacetError,
    all_of,
    leaf,
    requirement,
    solve,
)
from esmporium.search import UnknownRawDocFormatTagError

SOLR = "solr"
"""The format tag of the raw documents these tests store"""


@pytest.fixture
def store(engine, get_dataset_kwargs):
    """
    Get a factory which stores a dataset, its versions, and their raw documents

    Each version is given as `(version, is_latest, retracted, raw)`, where `raw` is the
    raw search document to attach (or `None` for none). Facet values come from the
    column names via `get_dataset_kwargs`, so only the facets a test cares about have
    to be named.
    """

    def factory(label, *, versions=((("20200101"), True, False, None),), **facets):
        with Session(engine) as session:
            dataset = Dataset(**get_dataset_kwargs(label, **facets))
            session.add(dataset)
            session.flush()

            for version, is_latest, retracted, raw in versions:
                row = DatasetVersion(
                    dataset_id=dataset.id,
                    version=version,
                    is_latest=is_latest,
                    retracted=retracted,
                )
                session.add(row)
                session.flush()

                for index, document in enumerate(raw or []):
                    doc = DatasetRawDoc(
                        esgf_doc_id=f"{label}-{version}-{index}",
                        raw_json=json.dumps(document),
                        raw_docs_format_tag=SOLR,
                    )
                    session.add(doc)
                    session.flush()
                    session.add(
                        RawDocVersionLink(raw_doc_id=doc.id, dataset_version_id=row.id)
                    )

            session.commit()
            return dataset.id

    return factory


def live(raw=None):
    """One version, latest and not retracted, optionally carrying raw documents"""
    return (("20200101", True, False, raw),)


def found(catalogue, query) -> list[str]:
    """The `id_project_specific` of everything a query finds, in the order given"""
    return [entry.id_project_specific for entry in catalogue.find(query)]


# ---------------------------------------------------------------- matching on facets


def test_finds_the_rows_matching_every_facet(engine, store):
    store("wanted", project="CMIP6", variable="tas")
    store("wrong-variable", project="CMIP6", variable="pr")
    store("wrong-project", project="CMIP5", variable="tas")

    catalogue = DatabaseCatalogue(engine)

    assert found(catalogue, Query(project="CMIP6", variable="tas")) == ["wanted_ps"]


def test_several_values_for_one_facet_are_an_or(engine, store):
    store("tas", variable="tas")
    store("pr", variable="pr")
    store("rlut", variable="rlut")

    catalogue = DatabaseCatalogue(engine)

    assert sorted(found(catalogue, Query(variable=("tas", "pr")))) == [
        "pr_ps",
        "tas_ps",
    ]


def test_a_facet_the_query_does_not_set_does_not_narrow(engine, store):
    store("gn", variable="tas", grid_label="gn")
    store("gr", variable="tas", grid_label="gr")

    catalogue = DatabaseCatalogue(engine)

    assert sorted(found(catalogue, Query(variable="tas"))) == ["gn_ps", "gr_ps"]


def test_a_query_setting_no_facets_finds_everything(engine, store):
    """
    An empty query constrains nothing, so it matches every row

    The same answer [`matches`][esmporium.requirements.matches] documents. It is a
    sharp edge one level up, which is why a requirement's leaf refuses an empty query.
    """
    store("one")
    store("two", variable="pr")

    catalogue = DatabaseCatalogue(engine)

    assert len(catalogue.find(Query())) == 2


def test_entries_come_back_in_a_stable_order(engine, store):
    """Ordered by ID, which is the order they were first saved in"""
    first = store("zebra", variable="tas")
    second = store("aardvark", variable="pr")

    catalogue = DatabaseCatalogue(engine)

    assert [entry.id for entry in catalogue.find(Query())] == [first, second]


def test_a_query_in_any_style_is_translated(engine, store):
    """A CMIP6-style query finds a row stored under the canonical column names"""
    store("wanted", project="CMIP6", variable="tas", reporting_interval="mon")
    store("other", project="CMIP6", variable="tas", reporting_interval="day")

    catalogue = DatabaseCatalogue(engine)

    asked = QueryCMIP6(variable_id="tas", frequency="mon")
    assert found(catalogue, asked) == ["wanted_ps"]


def test_a_row_with_no_grid_never_matches_a_grid_query(engine, store):
    """
    A `NULL` grid label does not match any grid a query can name

    CMIP5 has no concept of a grid, so its rows carry `NULL`. `IN` with a `NULL`
    left-hand side yields `NULL`, which is the same answer the in-memory catalogue
    gives (`None not in ("gn",)`), and a query cannot ask for `NULL` because facet
    values are strings.
    """
    store("cmip5", project="CMIP5", variable="tas", grid_label=None)
    store("cmip6", project="CMIP6", variable="tas", grid_label="gn")

    catalogue = DatabaseCatalogue(engine)

    assert found(catalogue, Query(variable="tas", grid_label="gn")) == ["cmip6_ps"]


def test_a_facet_set_in_two_homes_is_refused(engine, store):
    store("one", variable="tas")

    catalogue = DatabaseCatalogue(engine)

    with pytest.raises(ClashingFacetsError):
        catalogue.find(Query(variable="tas", other_terms={"variable": ("tas",)}))


def test_facet_filters_refuses_a_non_column(engine):
    """
    The low-level filter builder refuses anything it cannot turn into SQL

    A caller mistake rather than a user one -- `find_datasets` splits the facets
    before it gets here -- so it is pinned to stop the split silently changing.
    """
    with pytest.raises(UnrecordedFacetError, match="realm"):
        facet_filters({"realm": ("atmos",)})


# ------------------------------------------------------------------- availability


def test_a_dataset_whose_only_version_is_retracted_is_not_available(engine, store):
    store("retracted", variable="tas", versions=(("20200101", True, True, None),))

    assert found(DatabaseCatalogue(engine), Query(variable="tas")) == []


def test_availability_any_includes_a_retracted_dataset(engine, store):
    store("retracted", variable="tas", versions=(("20200101", True, True, None),))

    catalogue = DatabaseCatalogue(engine, availability=Availability.ANY)

    assert found(catalogue, Query(variable="tas")) == ["retracted_ps"]


def test_a_dataset_with_a_live_and_a_retracted_version_is_available(engine, store):
    store(
        "mixed",
        variable="tas",
        versions=(
            ("20200101", False, True, None),
            ("20200202", True, False, None),
        ),
    )

    assert found(DatabaseCatalogue(engine), Query(variable="tas")) == ["mixed_ps"]


def test_latest_not_retracted_excludes_a_superseded_version(engine, store):
    """
    A dataset whose only live version is no longer the latest is not available

    `not_retracted` keeps it, which is the setting for "we have not re-searched this
    yet, and what we have is still usable".
    """
    store(
        "superseded",
        variable="tas",
        versions=(
            ("20200101", False, False, None),
            ("20200202", True, True, None),
        ),
    )

    assert found(DatabaseCatalogue(engine), Query(variable="tas")) == []
    assert found(
        DatabaseCatalogue(engine, availability=Availability.NOT_RETRACTED),
        Query(variable="tas"),
    ) == ["superseded_ps"]


def test_a_dataset_with_no_versions_is_only_found_under_any(engine, store):
    store("no-versions", variable="tas", versions=())

    assert found(DatabaseCatalogue(engine), Query(variable="tas")) == []
    assert (
        found(
            DatabaseCatalogue(engine, availability=Availability.NOT_RETRACTED),
            Query(variable="tas"),
        )
        == []
    )
    assert found(
        DatabaseCatalogue(engine, availability=Availability.ANY), Query(variable="tas")
    ) == ["no-versions_ps"]


def test_an_availability_we_do_not_know_is_refused(engine):
    with pytest.raises(UnknownAvailabilityError, match="latest_not_retracted"):
        DatabaseCatalogue(engine, availability="whatever").find(Query(variable="tas"))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- extra


def test_realm_comes_from_the_raw_document(engine, store):
    """
    A canonical facet with no column is answered from the stored search document

    `realm`, `activity` and `resolution` are facets a query may name and the APIs
    answer for, but `Dataset` has no column for them, so the only place left is the
    raw document the search kept.
    """
    store("one", project="CMIP6", variable="tas", versions=live([{"realm": ["atmos"]}]))

    (entry,) = DatabaseCatalogue(engine).find(Query(variable="tas"))

    assert entry.facet("realm") == "atmos"


def test_a_facet_is_translated_out_of_the_document_into_its_canonical_name(
    engine, store
):
    """
    `activity_id` in the document answers for `activity` in a query

    A document is keyed by the names the API uses; a query asks with canonical names.
    The project's query class declares the mapping between the two, so that is where
    it is read from.
    """
    store(
        "one",
        project="CMIP6",
        variable="tas",
        versions=live([{"activity_id": ["CMIP"], "nominal_resolution": ["100 km"]}]),
    )

    (entry,) = DatabaseCatalogue(engine).find(Query(variable="tas"))

    assert entry.facet("activity") == "CMIP"
    assert entry.facet("resolution") == "100 km"
    # The document's own spelling still answers, so an `other_terms` facet written the
    # way the API writes it is not left unanswerable.
    assert entry.facet("activity_id") == "CMIP"


def test_a_project_specific_facet_comes_from_the_raw_document(engine, store):
    """CMIP5's `product` has no canonical equivalent, and is answered all the same"""
    store(
        "one",
        project="CMIP5",
        variable="tas",
        grid_label=None,
        versions=live([{"product": ["output1"]}]),
    )

    catalogue = DatabaseCatalogue(engine)

    (entry,) = catalogue.find(Query(variable="tas"))
    assert entry.facet("product") == "output1"
    assert found(catalogue, QueryCMIP5(variable="tas", product="output1")) == ["one_ps"]


def test_a_query_can_select_on_a_facet_only_the_document_knows(engine, store):
    store("atmos", variable="tas", versions=live([{"realm": ["atmos"]}]))
    store("ocean", variable="tas", versions=live([{"realm": ["ocean"]}]))

    catalogue = DatabaseCatalogue(engine)

    assert found(catalogue, Query(variable="tas", realm="atmos")) == ["atmos_ps"]


def test_extra_comes_from_the_version_the_availability_filter_matched(engine, store):
    """
    The document which answers belongs to the newest version the caller *accepted*

    Not simply the newest version. Here the latest version is retracted and says
    `ocean`, while the older live one says `atmos`. A caller who excluded the retracted
    version should not then be described by it.
    """
    store(
        "one",
        project="CMIP6",
        variable="tas",
        versions=(
            ("20200101", False, False, [{"realm": ["atmos"]}]),
            ("20200202", True, True, [{"realm": ["ocean"]}]),
        ),
    )

    not_retracted = DatabaseCatalogue(engine, availability=Availability.NOT_RETRACTED)
    anything = DatabaseCatalogue(engine, availability=Availability.ANY)

    (live_entry,) = not_retracted.find(Query(variable="tas"))
    (any_entry,) = anything.find(Query(variable="tas"))

    assert live_entry.facet("realm") == "atmos"
    assert any_entry.facet("realm") == "ocean"


def test_the_newest_accepted_version_wins_over_an_older_one(engine, store):
    store(
        "one",
        project="CMIP6",
        variable="tas",
        versions=(
            ("20200101", False, False, [{"realm": ["ocean"]}]),
            ("20200202", True, False, [{"realm": ["atmos"]}]),
        ),
    )

    (entry,) = DatabaseCatalogue(engine).find(Query(variable="tas"))

    assert entry.facet("realm") == "atmos"


def test_several_documents_for_one_version_merge_deterministically(engine, store):
    """
    One version can be described by several documents (one per data node)

    They can disagree, and there is no principled way to choose, so the first one
    stored wins and the rule is written down rather than left to chance.
    """
    store(
        "one",
        project="CMIP6",
        variable="tas",
        versions=live(
            [{"realm": ["atmos"]}, {"realm": ["ocean"], "activity_id": ["CMIP"]}]
        ),
    )

    (entry,) = DatabaseCatalogue(engine).find(Query(variable="tas"))

    assert entry.facet("realm") == "atmos"
    # A facet only the second document carries is still picked up.
    assert entry.facet("activity") == "CMIP"


def test_a_non_scalar_document_value_is_left_out(engine, store):
    """
    A facet with several values has no single answer, so the entry does not claim one

    `extra` holds one value per facet, because that is what grouping and preferring
    compare. A list is left out rather than guessed at, which reads as "this entry
    does not know that facet".
    """
    store(
        "one",
        project="CMIP6",
        variable="tas",
        versions=live([{"realm": ["atmos", "ocean"]}]),
    )

    (entry,) = DatabaseCatalogue(engine).find(Query(variable="tas"))

    with pytest.raises(UnrecordedFacetError, match="realm"):
        entry.facet("realm")


@pytest.mark.parametrize("populated", [False, True], ids=["empty", "populated"])
def test_a_facet_no_document_knows_is_refused(engine, store, populated):
    """
    Asking for a facet nothing records raises rather than quietly finding nothing

    Parametrised over an empty and a populated table because the point is that the
    *answer* is the same either way: this is a fact about what we store, not about
    which rows happen to be present.
    """
    if populated:
        store("one", variable="tas", versions=live([{"realm": ["atmos"]}]))

    catalogue = DatabaseCatalogue(engine)
    asked = Query(variable="tas", other_terms={"something_we_never_record": ("x",)})

    if populated:
        with pytest.raises(UnrecordedFacetError, match="something_we_never_record"):
            catalogue.find(asked)
    else:
        # Nothing matched the column facets, so no entry was ever asked. Documented
        # rather than asserted as desirable: the in-memory catalogue does exactly the
        # same, and `test_solving_against_the_database_matches_the_in_memory_catalogue`
        # is what holds the two together.
        assert catalogue.find(asked) == ()


def test_an_unknown_format_tag_is_refused(engine, get_dataset_kwargs):
    """A stored document we have no flattener for raises rather than being skipped"""
    with Session(engine) as session:
        dataset = Dataset(**get_dataset_kwargs("one", variable="tas"))
        session.add(dataset)
        session.flush()
        version = DatasetVersion(
            dataset_id=dataset.id, version="20200101", is_latest=True, retracted=False
        )
        session.add(version)
        session.flush()
        doc = DatasetRawDoc(
            esgf_doc_id="one-doc",
            raw_json=json.dumps({"realm": "atmos"}),
            raw_docs_format_tag="a-format-we-never-shipped",
        )
        session.add(doc)
        session.flush()
        session.add(RawDocVersionLink(raw_doc_id=doc.id, dataset_version_id=version.id))
        session.commit()

    with pytest.raises(UnknownRawDocFormatTagError):
        DatabaseCatalogue(engine).find(Query(variable="tas"))


# ------------------------------------------------------------- the protocol itself


def test_it_satisfies_the_catalogue_protocol(engine):
    """Structural conformance, which the type checkers also read off this line"""
    catalogue: Catalogue = DatabaseCatalogue(engine)

    assert catalogue.find(Query(variable="tas")) == ()


def test_find_datasets_and_the_catalogue_agree(engine, store):
    """The session-level primitive gives what the engine-level wrapper gives"""
    store("one", project="CMIP6", variable="tas", versions=live([{"realm": ["atmos"]}]))
    store("two", project="CMIP6", variable="pr", versions=live([{"realm": ["atmos"]}]))

    query = Query(variable="tas")
    with Session(engine) as session:
        directly = find_datasets(session, query)

    assert directly == DatabaseCatalogue(engine).find(query)


def test_to_catalogue_entry_refuses_an_unsaved_row(get_dataset_kwargs):
    """A row with no ID yet describes no stored dataset, so it has no entry"""
    with pytest.raises(ValueError, match="has not been saved"):
        to_catalogue_entry(Dataset(**get_dataset_kwargs("unsaved")))


def test_solving_against_the_database_matches_the_in_memory_catalogue(engine, store):
    """
    The two catalogues answer a whole solve identically

    This is the promise the database-backed one has to keep: everything in
    `esmporium.requirements` is written and tested against `InMemoryCatalogue`, so if
    the two diverge then none of those tests mean anything about real data.

    The requirement is deliberately awkward -- a group discovered from the data, a
    `prefer` tie-break, one leaf nothing was stored for, and a facet only the raw
    documents know about -- so that all three of `satisfied`, `unsatisfied` and the
    grouping are exercised through both.
    """
    rows = [
        ("tas-gn", {"variable": "tas", "grid_label": "gn", "model": "A"}, "atmos"),
        ("tas-gr", {"variable": "tas", "grid_label": "gr", "model": "A"}, "atmos"),
        ("rlut-gn", {"variable": "rlut", "grid_label": "gn", "model": "A"}, "atmos"),
        ("tas-b", {"variable": "tas", "grid_label": "gn", "model": "B"}, "atmos"),
    ]
    for label, facets, realm in rows:
        store(
            label,
            project="CMIP6",
            versions=live([{"realm": [realm]}]),
            **facets,
        )

    in_memory = InMemoryCatalogue(
        entries=tuple(
            CatalogueEntry(
                id=index + 1,
                id_project_specific=f"{label}_ps",
                project="CMIP6",
                institution="institution",
                experiment="experiment",
                variant_label="variant_label",
                reporting_interval="reporting_interval",
                processing_id="processing_id",
                model=facets["model"],
                variable=facets["variable"],
                grid_label=facets["grid_label"],
                extra={"realm": realm},
            )
            for index, (label, facets, realm) in enumerate(rows)
        )
    )

    req = requirement(
        name="needs-tas-and-rlut",
        tree=all_of(
            leaf(Query(variable="tas"), "tas"),
            leaf(Query(variable="rlut"), "rlut"),
        ),
        group_by=("model",),
        where=Query(project="CMIP6", realm="atmos"),
        prefer={"grid_label": ("gn",)},
    )

    from_db = solve(req, DatabaseCatalogue(engine))
    from_memory = solve(req, in_memory)

    assert set(from_db.satisfied) == set(from_memory.satisfied)
    assert set(from_db.unsatisfied) == set(from_memory.unsatisfied)
    assert set(from_db.ambiguous) == set(from_memory.ambiguous)
    # Not just the same verdicts: the same datasets chosen, named the one way both
    # catalogues agree on.
    for key, group in from_db.satisfied.items():
        other = from_memory.satisfied[key]
        assert {
            role: tuple(entry.id_project_specific for entry in entries)
            for role, entries in group.roles.items()
        } == {
            role: tuple(entry.id_project_specific for entry in entries)
            for role, entries in other.roles.items()
        }
