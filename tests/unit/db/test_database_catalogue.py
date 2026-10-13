"""
Tests of the database-backed catalogue

The headline is `test_solving_against_the_database_matches_the_in_memory_catalogue`:
the two implementations of [Catalogue][esmporium.requirements.Catalogue] have to give
the same answers, because that is the promise which lets everything in
`esmporium.requirements` be written and tested against the in-memory one.
"""

from __future__ import annotations

import json
from dataclasses import replace

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

    The same answer [matches][esmporium.requirements.matches] documents. It is a
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


def test_a_dataset_whose_only_version_is_retracted_is_not_available_by_default(engine, store):
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


def test_a_superseded_dataset_is_available_from_its_newest_live_version(engine, store):
    """
    A dataset whose latest version was retracted is kept, from the version before it

    The default's whole point: a dataset's newest usable version beats nothing. ESGF
    retracting the newest version does not unpublish the one before it, so a solve
    should be able to satisfy a role from that one rather than reporting the dataset
    missing.

    `latest_not_retracted` is the stricter question -- "only what is current" -- and
    still answers it, which is why the member survives the default moving. Both are
    asserted here so that the difference between them is visible in one place.
    """
    store(
        "superseded",
        variable="tas",
        versions=(
            ("20200101", False, False, None),
            ("20200202", True, True, None),
        ),
    )

    catalogue = DatabaseCatalogue(engine)

    assert found(catalogue, Query(variable="tas")) == ["superseded_ps"]
    assert (
        found(
            replace(catalogue, availability=Availability.LATEST_NOT_RETRACTED),
            Query(variable="tas"),
        )
        == []
    )


def test_the_default_describes_a_superseded_dataset_by_its_live_version(engine, store):
    """
    Availability chooses which version speaks for a dataset, not only whether it is kept

    Worth pinning separately, because it is the part of the default change which is
    easy to miss: the facet values an entry reports come from the newest version the
    setting *accepted*. Here the retracted latest says `ocean` and the live older one
    says `atmos`, so the default answers `atmos` -- a caller is described by the
    version they would actually get, rather than by one they were not offered.
    """
    store(
        "superseded",
        project="CMIP6",
        variable="tas",
        versions=(
            ("20200101", False, False, [{"realm": ["atmos"]}]),
            ("20200202", True, True, [{"realm": ["ocean"]}]),
        ),
    )

    catalogue = DatabaseCatalogue(engine)
    (entry,) = catalogue.find(Query(variable="tas"))

    assert entry.facet("realm") == "atmos"
    # `any` reaches past the retraction, and then the newest version does speak.
    (any_entry,) = replace(catalogue, availability=Availability.ANY).find(
        Query(variable="tas")
    )
    assert any_entry.facet("realm") == "ocean"


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
    # Which mapping it landed in, not just that something answers. `facet` reads
    # `other_terms` as well, so a `realm` recorded only there would satisfy the line
    # above while no longer matching `Query(realm=...)` -- see
    # `test_a_modelled_facet_recorded_only_in_other_terms_does_not_answer`.
    assert entry.project_facets["realm"] == "atmos"
    assert "realm" in entry.modelled_facets()


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
    """
    `realm` has no column, so only the stored document can answer -- and a query can

    `project` is named rather than left to the fixture default, and it has to be: a
    facet is only answerable as one we model when the row's project is one we have a
    query class for. The default label is not a real project, so these rows would
    record `realm` in `other_terms` alone and the query would correctly match nothing
    -- which would be testing the unmodelled-project rule rather than this.
    """
    store(
        "atmos", project="CMIP6", variable="tas", versions=live([{"realm": ["atmos"]}])
    )
    store(
        "ocean", project="CMIP6", variable="tas", versions=live([{"realm": ["ocean"]}])
    )

    catalogue = DatabaseCatalogue(engine)

    assert found(catalogue, Query(variable="tas", realm="atmos")) == ["atmos_ps"]


def test_a_project_we_have_no_query_class_for_models_nothing(engine, store):
    """
    Everything an unmodelled project records is `other_terms`, canonical spellings too

    We do not own that project's facet names, so we do not claim to translate them --
    not even one spelled exactly like a canonical facet of ours, because whether it
    means the same thing is a question about conventions we have not looked at. The
    route for a user is `other_terms`, which promises nothing and checks nothing; the
    route for a maintainer is a query class, which is what makes the names ours.
    """
    store(
        "cordex",
        project="CORDEX-CMIP6",
        variable="tas",
        versions=live([{"realm": ["atmos"], "driving_source_id": ["MPI-ESM1-2-HR"]}]),
    )

    catalogue = DatabaseCatalogue(engine)
    (entry,) = catalogue.find(Query(variable="tas"))

    assert entry.project_facets == {}
    assert entry.other_terms["realm"] == "atmos"
    assert entry.other_terms["driving_source_id"] == "MPI-ESM1-2-HR"

    # So the facet we model finds nothing, and the escape hatch finds it.
    assert found(catalogue, Query(variable="tas", realm="atmos")) == []
    assert found(
        catalogue, Query(variable="tas", other_terms={"realm": ("atmos",)})
    ) == ["cordex_ps"]


def test_a_modelled_facet_recorded_only_in_other_terms_does_not_answer(engine, store):
    """
    A document's own field does not stand in for the facet we model of that name

    A CMIP6 search document carries a `product` field, and it is not CMIP5's `product`
    -- the two mean different things, which is why CMIP5 declares the facet and CMIP6
    does not. So the value is recorded under the document's own spelling in
    `other_terms` and *not* in `project_facets`, and a lookup for the facet we model
    must not find it. Letting it through would undo the per-project decision and, worse
    than a stray match, would let a requirement group CMIP6 data on a CMIP5 facet.
    """
    store(
        "one",
        project="CMIP6",
        variable="tas",
        versions=live([{"product": ["model-output"], "realm": ["atmos"]}]),
    )

    (entry,) = DatabaseCatalogue(engine).find(Query(variable="tas"))

    assert entry.other_terms["product"] == "model-output"
    assert "product" not in entry.project_facets

    # `facet` still answers, because `group_by_other_terms` and `prefer_other_terms`
    # reach the escape hatch through it.
    assert entry.facet("product") == "model-output"
    # The modelled accessors do not, which is what matching and the solver's checks use.
    assert entry.modelled_facet("product") is None
    assert "product" not in entry.modelled_facets()
    assert "product" in entry.known_facets()


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

    # `replace` rather than two constructions, because that is what asking the same
    # database two questions actually costs: `DatabaseCatalogue` is a frozen dataclass
    # around the engine, so this builds no connection and copies no state. Availability
    # stays an attribute rather than an argument to `find` so that the catalogue still
    # satisfies [Catalogue][esmporium.requirements.Catalogue], which `solve` calls
    # knowing nothing about versions -- and `find_datasets` takes it per call for a
    # caller who wants one session across several.
    base = DatabaseCatalogue(engine)
    not_retracted = replace(base, availability=Availability.NOT_RETRACTED)
    anything = replace(base, availability=Availability.ANY)

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

    The cost of merging rather than taking the first document whole: the entry can end
    up asserting a combination no single node published, as it does here (`atmos` from
    the first, `activity` from the second). We merge anyway because the alternative
    loses facets that exist -- two ESGF-NG mirrors of the same project differ by ten
    field names in `tests/test-data/search/`, so a sparser first document would make
    `group_by` on the others silently impossible -- and because "first" is
    `DatasetRawDoc.id` ascending, i.e. earliest stored, which is arbitrary rather than
    authoritative. Disagreement is at least deterministic. If that trade is ever
    revisited, the thing to reach for is surfacing the disagreement (the repo already
    has the notion in `UnhandledDatasetClashError` and `facet_differences`) rather than
    discarding the data.
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

    Both facet mappings hold one value per facet, because that is what grouping and
    preferring compare. A list is left out rather than guessed at, which reads as
    "this entry does not know that facet" -- and so a genuinely multi-realm dataset
    cannot be grouped on `realm`, which is a known wart rather than a decision.
    """
    store(
        "one",
        project="CMIP6",
        variable="tas",
        versions=live([{"realm": ["atmos", "ocean"]}]),
    )

    (entry,) = DatabaseCatalogue(engine).find(Query(variable="tas"))

    assert entry.facet("realm") is None
    assert "realm" not in entry.known_facets()


@pytest.mark.parametrize("populated", [False, True], ids=["empty", "populated"])
def test_a_facet_no_document_knows_is_refused(engine, store, populated):
    """
    Asking for a facet nothing records finds nothing, whatever the table holds

    Parametrised over an empty and a populated table because the point is that the
    *answer* is the same either way: this is a fact about what we store, not about
    which rows happen to be present. It used to raise when populated and return `()`
    when empty, which is the defect the tier split removed.
    """
    if populated:
        store("one", variable="tas", versions=live([{"realm": ["atmos"]}]))

    catalogue = DatabaseCatalogue(engine)
    asked = Query(variable="tas", other_terms={"something_we_never_record": ("x",)})

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
                # `realm` is a facet we model, so it goes in `project_facets` under
                # its canonical name; the Solr document's own spelling of it is the
                # same word, so `other_terms` carries it too. Putting it in the wrong
                # mapping is exactly the mistake this test exists to catch, because
                # the database-backed catalogue fills both from the stored document.
                project_facets={"realm": realm},
                other_terms={"realm": realm},
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


# ------------------------------------- the two catalogues answering about a facet
# nothing records


def outcome(catalogue, query):
    """
    What a catalogue did with a query: what it found, or the error it raised

    Returned rather than asserted on directly, so that the two catalogues can be
    compared without this test caring which of the two answers is the right one.

    The `except` is kept although nothing in the matching path raises any more, so
    that a regression which reintroduced an error here would show up as a difference
    between the two catalogues rather than as this helper blowing up.
    """
    try:
        return tuple(entry.id_project_specific for entry in catalogue.find(query))
    except ValueError as exc:
        return f"{type(exc).__name__}: {exc}"


@pytest.mark.parametrize(
    "query",
    [
        pytest.param(
            Query(variable="tas", realm="atmos"), id="declared-column-matches"
        ),
        pytest.param(
            Query(variable="ta", realm="atmos"), id="declared-column-matches-nothing"
        ),
        pytest.param(
            Query(variable="tas", other_terms={"realm": ("atmos",)}),
            id="other-terms-column-matches",
        ),
        pytest.param(
            Query(variable="ta", other_terms={"realm": ("atmos",)}),
            id="other-terms-column-matches-nothing",
        ),
    ],
)
def test_both_catalogues_answer_alike_about_a_facet_nothing_records(
    engine, store, get_dataset_kwargs, query
):
    """
    Both catalogues give the same answer when asked for a facet nothing records

    `realm` is the facet here: a canonical facet every query class names, with no
    column on [Dataset][esmporium.db.schema.Dataset], so only a raw document can
    answer for it -- and the dataset stored here has no raw documents, so nothing
    can. Whether that is an error or an empty result is not what this test is about.
    What it is about is that both catalogues decide it the same way, because
    everything in `esmporium.requirements` is written against `InMemoryCatalogue`
    and solving against the database has to mean the same thing.

    `test_solving_against_the_database_matches_the_in_memory_catalogue` cannot see
    this: every entry in it records `realm`, so the question never arises.

    This used to disagree on `declared-column-matches-nothing`, and why is worth
    keeping: the two catalogues reach the facet by different routes -- the in-memory
    one checked every facet of every entry in alphabetical order (so `realm` before
    `variable`), while the database one filters on the columns in SQL and only asks
    the surviving rows about the rest. With `facet` raising, the in-memory catalogue
    still asked `realm` and raised, and the database one never asked at all. `facet`
    answering `None` is what makes the route stop mattering, so all four cases now
    find nothing. The parametrisation is kept to hold it that way.
    """
    store("one", variable="tas", versions=live())

    in_memory = InMemoryCatalogue(
        entries=(CatalogueEntry(id=1, **get_dataset_kwargs("one", variable="tas")),)
    )

    assert outcome(DatabaseCatalogue(engine), query) == outcome(in_memory, query)
    # Pinned, not just compared: two catalogues which both raised would also agree.
    assert outcome(DatabaseCatalogue(engine), query) == ()
