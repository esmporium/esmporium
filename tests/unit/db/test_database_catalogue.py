"""
Tests of the database-backed catalogue

The headline is `test_solving_against_the_database_matches_the_in_memory_catalogue`:
the two implementations of [Catalogue][esmporium.requirements.Catalogue] have to give
the same answers, because that is the promise which lets everything in
`esmporium.requirements` be written and tested against the in-memory one.
"""

from __future__ import annotations

import inspect
import json

import pytest
from sqlmodel import Session

from esmporium.datasets import DATASET_FACET_COLUMNS
from esmporium.db import (
    DatabaseCatalogue,
    UnknownVersionFilterError,
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
    UnloadedFacetError,
    UnrecordedFacetError,
    VersionFilter,
    all_of,
    leaf,
    requirement,
    solve,
)
from esmporium.search import (
    DEFAULT_NORMALISERS,
    VERBATIM_NORMALISERS,
    UnknownRawDocFormatTagError,
)

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


def found(catalogue, query, **kwargs) -> list[str]:
    """The `id_project_specific` of everything a query finds, in the order given"""
    return [entry.id_project_specific for entry in catalogue.find(query, **kwargs)]


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


# ------------------------------------------------------------------- versions


def test_a_dataset_whose_only_version_is_retracted_is_not_available_by_default(
    engine, store
):
    store("retracted", variable="tas", versions=(("20200101", True, True, None),))

    assert found(DatabaseCatalogue(engine), Query(variable="tas")) == []


def test_version_filter_any_includes_a_retracted_dataset(engine, store):
    store("retracted", variable="tas", versions=(("20200101", True, True, None),))

    catalogue = DatabaseCatalogue(engine)

    assert found(catalogue, Query(variable="tas"), versions=VersionFilter.ANY) == [
        "retracted_ps"
    ]


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
            catalogue,
            Query(variable="tas"),
            versions=VersionFilter.LATEST_NOT_RETRACTED,
        )
        == []
    )


def test_the_default_describes_a_superseded_dataset_by_its_live_version(engine, store):
    """
    The filter chooses which version speaks for a dataset, not only whether it is kept

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
    (any_entry,) = catalogue.find(Query(variable="tas"), versions=VersionFilter.ANY)
    assert any_entry.facet("realm") == "ocean"


def test_a_dataset_with_no_versions_is_only_found_under_any(engine, store):
    store("no-versions", variable="tas", versions=())

    catalogue = DatabaseCatalogue(engine)

    assert found(catalogue, Query(variable="tas")) == []
    assert (
        found(catalogue, Query(variable="tas"), versions=VersionFilter.NOT_RETRACTED)
        == []
    )
    assert found(catalogue, Query(variable="tas"), versions=VersionFilter.ANY) == [
        "no-versions_ps"
    ]


def test_a_version_filter_we_do_not_know_is_refused(engine):
    with pytest.raises(UnknownVersionFilterError, match="latest_not_retracted"):
        DatabaseCatalogue(engine).find(Query(variable="tas"), versions="whatever")  # type: ignore[arg-type]


# -------------------------------------------------- project_facets and other_terms


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


def test_document_facets_come_from_the_version_the_version_filter_matched(
    engine, store
):
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

    catalogue = DatabaseCatalogue(engine)

    (live_entry,) = catalogue.find(Query(variable="tas"))
    (any_entry,) = catalogue.find(Query(variable="tas"), versions=VersionFilter.ANY)

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


# A note for developers:
# The cost of merging rather than taking the first document whole: the entry can end
# up asserting a combination no single node published, as it does here (`atmos` from
# the first, `activity` from the second). We merge anyway because the alternative
# loses facets that exist -- two ESGF-NG mirrors of the same project differ by ten
# field names in `tests/test-data/search/`, so a sparser first document would make
# `group_by` on the others silently impossible -- and because "first" is
# `DatasetRawDoc.id` ascending, i.e. earliest stored, which is arbitrary rather than
# authoritative. Disagreement is at least deterministic. If that trade is ever
# revisited, the thing to reach for is surfacing the disagreement (the repo already
# has the notion in `UnhandledDatasetClashError` and `facet_differences`) rather than
# discarding the data.
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
    """
    if populated:
        store("one", variable="tas", versions=live([{"realm": ["atmos"]}]))

    catalogue = DatabaseCatalogue(engine)
    asked = Query(variable="tas", other_terms={"something_we_never_record": ("x",)})

    assert catalogue.find(asked) == ()


OUR_OWN_TAG = "a-format-we-never-shipped"
"""A `raw_docs_format_tag` no flattener we ship knows"""


def flatten_our_own(raw):
    """
    Flatten a document of our own format, for the injection tests

    Deliberately keys-untouched, which is what makes it usable as both of the
    flatteners a reader takes: the two built-in mappings differ only for a format which
    prefixes its facet names, and this one does not.
    """
    return dict(raw)


@pytest.fixture
def stored_in_our_own_format(engine, get_dataset_kwargs):
    """
    Store one dataset whose only document carries a format tag we do not ship

    `project` is named, and has to be: a facet is only answerable as one we model when
    the row's project is one we have a query class for, so with the fixture default
    label the injected flattener would be exercised without `realm` ever reaching
    `project_facets`. See
    `test_a_project_we_have_no_query_class_for_models_nothing`.
    """
    with Session(engine) as session:
        dataset = Dataset(**get_dataset_kwargs("one", project="CMIP6", variable="tas"))
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
            raw_docs_format_tag=OUR_OWN_TAG,
        )
        session.add(doc)
        session.flush()
        session.add(RawDocVersionLink(raw_doc_id=doc.id, dataset_version_id=version.id))
        session.commit()

    return engine


def test_an_unknown_format_tag_is_refused(stored_in_our_own_format):
    """A stored document we have no flattener for raises rather than being skipped"""
    with pytest.raises(UnknownRawDocFormatTagError, match=OUR_OWN_TAG):
        DatabaseCatalogue(stored_in_our_own_format).find(Query(variable="tas"))


def test_injecting_a_flattener_for_our_own_format_makes_it_readable(
    stored_in_our_own_format,
):
    """
    Registering the tag in both mappings is what clears the error

    The pair is the point. A reader flattens each document twice -- once
    canonicalising its keys, once keeping them -- so a tag has to be known to both
    before anything can be read, and the same function serves both here because this
    format does not prefix its facet names.
    """
    catalogue = DatabaseCatalogue(
        stored_in_our_own_format,
        normalisers={**DEFAULT_NORMALISERS, OUR_OWN_TAG: flatten_our_own},
        verbatim_normalisers={**VERBATIM_NORMALISERS, OUR_OWN_TAG: flatten_our_own},
    )

    (entry,) = catalogue.find(Query(variable="tas"))

    # `realm` is modelled, so it lands under its canonical name and a query can use it.
    assert entry.project_facets["realm"] == "atmos"
    assert found(catalogue, Query(variable="tas", realm="atmos")) == ["one_ps"]


def test_registering_only_one_of_the_two_mappings_still_refuses(
    stored_in_our_own_format,
):
    """
    Half the registration is no registration, and the message has to say so
    """
    catalogue = DatabaseCatalogue(
        stored_in_our_own_format,
        normalisers={**DEFAULT_NORMALISERS, OUR_OWN_TAG: flatten_our_own},
    )

    with pytest.raises(UnknownRawDocFormatTagError) as excinfo:
        catalogue.find(Query(variable="tas"))

    assert "verbatim_normalisers" in str(excinfo.value)


# ------------------------------------------------------------------------- needs_facets


def test_naming_only_columns_leaves_the_documents_unread(stored_in_our_own_format):
    """
    A lookup which wants nothing but columns does not open a stored document
    """
    catalogue = DatabaseCatalogue(stored_in_our_own_format)

    (entry,) = catalogue.find(Query(variable="tas"), needs_facets=("model",))

    assert entry.id_project_specific == "one_ps"
    # The columns come whether or not they were named, because they are the row.
    assert entry.facets_loaded == frozenset(DATASET_FACET_COLUMNS)


def test_naming_a_facet_only_a_document_knows_reads_them(stored_in_our_own_format):
    """The same store, asked for `realm`, has to go to the documents"""
    with pytest.raises(UnknownRawDocFormatTagError, match=OUR_OWN_TAG):
        DatabaseCatalogue(stored_in_our_own_format).find(
            Query(variable="tas"), needs_facets=("model", "realm")
        )


def test_the_querys_own_facets_are_read_whatever_was_named(stored_in_our_own_format):
    """
    Matching comes first, so a facet the query names is read regardless
    """
    with pytest.raises(UnknownRawDocFormatTagError, match=OUR_OWN_TAG):
        DatabaseCatalogue(stored_in_our_own_format).find(
            Query(variable="tas", realm="atmos"), needs_facets=("model",)
        )


def test_a_lean_entry_refuses_a_facet_nobody_named(engine, store):
    """
    The two halves together: not read, and said so when asked

    The same dataset, read twice. What the full entry answers for `realm` is what the
    lean one refuses to guess at.
    """
    store("one", project="CMIP6", variable="tas", versions=live([{"realm": ["atmos"]}]))
    catalogue = DatabaseCatalogue(engine)

    (full,) = catalogue.find(Query(variable="tas"))
    (lean,) = catalogue.find(Query(variable="tas"), needs_facets=("model",))

    assert full.facet("realm") == "atmos"
    assert full.facets_loaded is None
    with pytest.raises(UnloadedFacetError, match="realm"):
        lean.facet("realm")


def test_skipping_the_documents_does_not_change_a_solve(
    engine, store, reads_everything
):
    """
    The same requirement gives the same answer whether or not the documents were read

    This is the test that stops the optimisation changing answers. The requirement is
    built to reach all three outcomes -- one group satisfied, one missing a dataset,
    one ambiguous -- because each is produced by a different part of the solver and
    each carries its own explanation.

    The ambiguity here is between two candidates which differ in `grid_label`, a
    column. Two which differ *only* in a facet no column holds are the one case where
    the explanation legitimately differs, and
    `test_an_ambiguity_among_lean_candidates_says_what_was_read` in
    `tests/unit/requirements/test_solve.py` is where that is pinned.
    """
    rows = [
        ("tas-a", {"model": "A", "variable": "tas", "grid_label": "gn"}),
        ("rlut-a", {"model": "A", "variable": "rlut", "grid_label": "gn"}),
        # B has both variables, but two grids for `tas` and nothing to choose between
        # them, so the group is ambiguous rather than satisfied.
        ("tas-b-gn", {"model": "B", "variable": "tas", "grid_label": "gn"}),
        ("tas-b-gr", {"model": "B", "variable": "tas", "grid_label": "gr"}),
        ("rlut-b", {"model": "B", "variable": "rlut", "grid_label": "gn"}),
        # C has no `tas` at all, so it is missing a dataset. Unsatisfied beats
        # ambiguous, which is why B needs its `rlut`.
        ("rlut-c", {"model": "C", "variable": "rlut", "grid_label": "gn"}),
    ]
    for label, facets in rows:
        store(label, project="CMIP6", versions=live([{"realm": ["atmos"]}]), **facets)

    req = requirement(
        name="needs-tas-and-rlut",
        tree=all_of(
            leaf(Query(variable="tas"), "tas"),
            leaf(Query(variable="rlut"), "rlut"),
        ),
        group_by=("model",),
        where=Query(project="CMIP6"),
    )

    lean = solve(req, DatabaseCatalogue(engine))
    full = solve(req, reads_everything(DatabaseCatalogue(engine)))

    # Every group reached, in the same bucket.
    assert set(lean.satisfied) == set(full.satisfied)
    assert set(lean.unsatisfied) == set(full.unsatisfied)
    assert set(lean.ambiguous) == set(full.ambiguous)
    # Not vacuous: all three buckets were exercised.
    assert len(lean.satisfied) == 1
    assert len(lean.unsatisfied) == 1
    assert len(lean.ambiguous) == 1
    # The same datasets chosen, named the way both solves agree on.
    assert {
        key: {
            role: tuple(entry.id for entry in entries)
            for role, entries in group.roles.items()
        }
        for key, group in lean.satisfied.items()
    } == {
        key: {
            role: tuple(entry.id for entry in entries)
            for role, entries in group.roles.items()
        }
        for key, group in full.satisfied.items()
    }
    # And the same explanation, word for word.
    assert lean.explain() == full.explain()
    # Not vacuous: the two solves really did read different amounts. Without this the
    # test would still pass if `solve` stopped naming what it reads.
    assert lean.satisfied[(("model", "A"),)].one("tas").facets_loaded is not None
    assert full.satisfied[(("model", "A"),)].one("tas").facets_loaded is None


# ------------------------------------------------------------- the protocol itself


def test_it_satisfies_the_catalogue_protocol(engine):
    """Structural conformance, which the type checkers also read off this line"""
    catalogue: Catalogue = DatabaseCatalogue(engine)

    # The signature, not just the call: `versions` is keyword-only with a default, so
    # an implementation which dropped it would still answer `find(query)` and only
    # fail once someone asked for a different filter. The in-memory catalogue is held
    # to the same comparison in `tests/unit/requirements/test_catalogue.py`.
    assert inspect.signature(type(catalogue).find) == inspect.signature(Catalogue.find)
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
    """
    store("one", variable="tas", versions=live())

    in_memory = InMemoryCatalogue(
        entries=(CatalogueEntry(id=1, **get_dataset_kwargs("one", variable="tas")),)
    )

    assert outcome(DatabaseCatalogue(engine), query) == outcome(in_memory, query)
    # Pinned, not just compared: two catalogues which both raised would also agree.
    assert outcome(DatabaseCatalogue(engine), query) == ()
