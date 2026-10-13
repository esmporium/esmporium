"""
Tests of the catalogue: entries, the facets a query sets, and matching

The one worth more than the rest is the protocol-conformance test, because it is
what breaks quietly: nothing declares that `InMemoryCatalogue` implements
`Catalogue`, so the two can drift apart in silence.

`CatalogueEntry` is checked against `Dataset` in `tests/unit/test_schema.py`, beside
the same check for `DatasetFacets`, because that is the file someone opens when they
change `Dataset`.
"""

from __future__ import annotations

import inspect
import re

import pytest

from esmporium.datasets import DATASET_FACET_COLUMNS
from esmporium.query import (
    ClashingFacetsError,
    Query,
    QueryCanonical,
    QueryCMIP5,
    QueryCMIP6,
    QueryCMIP7,
)
from esmporium.requirements import (
    Catalogue,
    DuplicateEntryIDError,
    InMemoryCatalogue,
    asked_facets,
    matches,
    matches_facets,
    set_facets,
)

# ---------------------------------------------------------------- CatalogueEntry.facet


@pytest.mark.parametrize("column", ["variable", "grid_label"])
def test_facet_returns_a_column(column: str, make_entry):
    entry = make_entry(1)

    assert entry.facet(column) == getattr(entry, column)


def test_facet_returns_none_for_a_dataset_with_no_grid(make_entry):
    # CMIP5 has no concept of a grid, so this is a real value rather than a failure.
    assert make_entry(1, grid_label=None).facet("grid_label") is None


def test_facet_reads_both_facet_mappings(make_entry):
    assert make_entry(1).project_facets == {}
    assert make_entry(1).other_terms == {}

    entry = make_entry(
        1,
        project_facets={"product": "output1", "realm": None},
        other_terms={"cmip6:mip_era": "CMIP6"},
    )

    assert entry.facet("product") == "output1"
    assert entry.facet("cmip6:mip_era") == "CMIP6"
    # A facet recorded as `None` is known to have no value, which is a different
    # question from a facet the entry has never heard of -- and `known_facets` is
    # where that difference is visible, because `facet` answers `None` to both.
    assert entry.facet("realm") is None


def test_facet_answers_none_for_a_facet_it_does_not_know(make_entry):
    # Deliberately not an error. `matches_facets` is an `all`, so an entry which
    # raised here would make the result of a query depend on which row happened to be
    # asked first: see the note above `Catalogue`. A facet value is always a string,
    # so `None` cannot match anything a query asks for.
    assert make_entry(1).facet("nonsense") is None


@pytest.mark.parametrize("name", ["id", "id_project_specific"])
def test_facet_does_not_answer_for_identifiers(name: str, make_entry):
    # These say *which* dataset an entry is, not what it is like, so selecting on
    # them is not what `facet` is for. They are columns of `Dataset` but not of
    # `DATASET_FACET_COLUMNS`, so they read as unknown rather than being returned.
    assert make_entry(1).facet(name) is None


def test_known_facets_covers_the_columns_and_both_mappings(make_entry):
    # The seam which tells "no value" from "no such facet". `group_by` needs it:
    # grouping CMIP5 on `grid_label` is one group of datasets which genuinely have no
    # grid, whereas grouping on a facet nothing recorded is no grouping at all.
    entry = make_entry(
        1,
        grid_label=None,
        project_facets={"realm": None},
        other_terms={"cmip6:mip_era": "CMIP6"},
    )

    known = entry.known_facets()

    assert known == frozenset(DATASET_FACET_COLUMNS) | {"realm", "cmip6:mip_era"}
    # Both of these answer `None` from `facet`, and only one of them is known.
    assert "grid_label" in known
    assert "realm" in known
    assert "nonsense" not in known


# -------------------------------------------------------------------------- set_facets


def test_set_facets_keeps_canonical_names_and_drops_empties():
    assert set_facets(Query(variable="tas", experiment="historical")) == {
        "experiment": ("historical",),
        "variable": ("tas",),
    }


@pytest.mark.parametrize(
    "query",
    [
        pytest.param(
            Query(experiment="historical", reporting_interval="mon"), id="base"
        ),
        pytest.param(
            QueryCMIP5(experiment="historical", time_frequency="mon"), id="cmip5"
        ),
        pytest.param(
            QueryCMIP6(experiment_id="historical", frequency="mon"), id="cmip6"
        ),
        pytest.param(
            QueryCMIP7(experiment_id="historical", frequency="mon"), id="cmip7"
        ),
        # Already canonical, so it has to be recognised and used as it is:
        # `QueryCanonical`'s fields carry no `QueryFacet`, so translating it would
        # raise rather than come back with the same facets.
        pytest.param(
            QueryCanonical(experiment="historical", reporting_interval="mon"),
            id="already-canonical",
        ),
    ],
)
def test_set_facets_translates_any_query_style(query):
    facets = set_facets(query)

    # Whatever the style called them, they come back under the names our columns use.
    assert facets["experiment"] == ("historical",)
    assert facets["reporting_interval"] == ("mon",)


def test_set_facets_includes_query_specific_facets():
    facets = set_facets(QueryCMIP5(variable="tas", product="output1"))

    assert facets["product"] == ("output1",)


def test_set_facets_includes_other_terms_and_drops_empty_ones():
    facets = set_facets(
        Query(variable="tas", other_terms={"driving_model": ("X",), "nothing": ()})
    )

    assert facets == {"variable": ("tas",), "driving_model": ("X",)}


@pytest.mark.parametrize(
    "query, clashing",
    [
        pytest.param(
            Query(experiment="historical", other_terms={"experiment": ("ssp585",)}),
            ("experiment",),
            id="canonical-vs-other-terms",
        ),
        pytest.param(
            QueryCMIP5(product="output1", other_terms={"product": ("output2",)}),
            ("product",),
            id="query-specific-vs-other-terms",
        ),
    ],
)
def test_set_facets_refuses_a_facet_set_twice(query, clashing):
    # The message comes from `esmporium.query`, which `search` raises it from too, so
    # pinning it here also pins what a search user sees.
    expected = (
        f"`other_terms` facet {clashing[0]!r} clashes with the query's facet names. "
        "Set each facet either as a query facet or in `other_terms`, not both."
    )

    with pytest.raises(ClashingFacetsError, match=re.escape(expected)) as excinfo:
        set_facets(query)

    assert excinfo.value.clashing == clashing


# ----------------------------------------------------------------------------- matches


def test_matches_facets_checks_already_flattened_facets(make_entry):
    """
    The companion to `set_facets`: flatten once, then check many entries

    Public because both catalogues need it. `InMemoryCatalogue` flattens a query once
    and walks its own entries; the database-backed one flattens once and walks the rows
    its `select` returned. Going through `matches` instead would re-flatten the query
    per entry, and -- more to the point -- the two catalogues agreeing about a facet
    only an entry can answer for is exactly what sharing this function buys.
    """
    entry = make_entry(1, variable="tas", experiment="historical")
    asked = asked_facets(Query(variable="tas", experiment="historical"))

    assert matches_facets(asked, entry)
    assert not matches_facets(asked_facets(Query(variable="pr")), entry)
    # Nothing to check means nothing to fail, as with an empty query one level up.
    assert matches_facets(asked_facets(Query()), entry)


@pytest.mark.parametrize(
    ("variable", "expected"),
    [pytest.param("tas", True, id="matches"), pytest.param("pr", False, id="does-not")],
)
def test_matches_facets_and_matches_agree(make_entry, variable, expected):
    """Parametrised both ways, so this cannot pass by both answers being `False`"""
    entry = make_entry(1, variable="tas")
    query = Query(variable=variable, project=entry.project)

    assert matches(query, entry) is expected
    assert matches_facets(asked_facets(query), entry) is expected


def test_matches_on_one_facet(make_entry):
    entry = make_entry(1, variable="tas")

    assert matches(Query(variable="tas"), entry)
    assert not matches(Query(variable="pr"), entry)


def test_several_values_for_one_facet_are_an_or(make_entry):
    entry = make_entry(1, variable="tas")

    assert matches(Query(variable=("pr", "tas")), entry)
    assert not matches(Query(variable=("pr", "rsdt")), entry)


def test_an_empty_query_matches_everything(make_entry):
    # Deliberately no facets at all, `project` included: a query which asks for
    # nothing constrains nothing. Setting any facet here would test something else.
    assert matches(Query(), make_entry(1))


@pytest.mark.parametrize(
    "query",
    [
        pytest.param(Query(variable="tas", activity="CMIP"), id="declared"),
        pytest.param(
            Query(variable="tas", other_terms={"activity": ("CMIP",)}),
            id="other-terms",
        ),
    ],
)
@pytest.mark.parametrize("populated", [False, True], ids=["empty", "populated"])
def test_a_facet_nothing_records_finds_nothing_either_way(query, populated, make_entry):
    # The defect this replaced. `matches_facets` is an `all`, which stops at the first
    # facet that does not match, and `facet` used to raise for a facet an entry did not
    # know -- so the same query raised or quietly found nothing depending on where the
    # facet sorted and on whether any row got far enough to be asked.
    #
    # Parametrised over both homes and over an empty and a populated catalogue because
    # the point is that all four answers are the same one.
    entries = (make_entry(1, variable="tas"),) if populated else ()

    assert InMemoryCatalogue(entries=entries).find(query) == ()


def test_other_terms_match_the_document_spelling(make_entry):
    # `other_terms` is matched against `other_terms`, which is keyed as the search
    # document keyed it. Nothing is translated at either end, so the name the user
    # would have sent to the API is the name which matches it back.
    entries = (
        make_entry(1, other_terms={"cordex-cmip6:driving_source_id": "ACCESS-CM2"}),
        make_entry(2, other_terms={"cordex-cmip6:driving_source_id": "MIROC6"}),
    )
    query = Query(other_terms={"cordex-cmip6:driving_source_id": ("ACCESS-CM2",)})

    found = InMemoryCatalogue(entries=entries).find(query)

    assert [entry.id for entry in found] == [1]


def test_other_terms_do_not_read_the_facets_we_model(make_entry):
    # The two mappings are matched separately, and that is what keeps the escape hatch
    # an escape hatch: `realm` in `other_terms` asks for the document's own spelling of
    # `realm`, not for the canonical facet an entry answers under.
    entry = make_entry(1, project_facets={"realm": "atmos"})

    assert matches(Query(realm="atmos"), entry)
    assert not matches(Query(other_terms={"realm": ("atmos",)}), entry)


def test_a_modelled_facet_is_not_answered_from_other_terms(make_entry):
    """
    The two mappings do not stand in for each other, in either direction

    `other_terms` holds a document's own field names, and one of them can coincide with
    a facet we model without meaning it: a CMIP6 document's `product` is not CMIP5's
    `product`. So a facet which arrived as one we model is answered from the columns and
    `project_facets` only, and the escape hatch is answered from `other_terms` only.

    Pinned on the entry as well as through `matches`, because `facet` deliberately reads
    both -- `group_by_other_terms` needs it to -- so a mix-up is invisible there.
    """
    entry = make_entry(1, other_terms={"product": "model-output"})

    assert entry.facet("product") == "model-output"
    assert entry.modelled_facet("product") is None
    assert "product" not in entry.modelled_facets()
    assert "product" in entry.known_facets()

    # A query naming the facet we model finds nothing; the escape hatch finds it.
    assert not matches(QueryCMIP5(product="model-output"), entry)
    assert matches(Query(other_terms={"product": ("model-output",)}), entry)


def test_two_spellings_of_one_facet_match_nothing(make_entry):
    # Several values for one facet are an "or"; several facet names are an "and". So
    # naming one facet under both a STAC and a Solr spelling cannot match, because no
    # single document carries both. This is why `other_terms` is per-API, and why
    # covering both needs a solve per spelling.
    stac = make_entry(1, other_terms={"cmip6:source_id": "ACCESS-CM2"})
    solr = make_entry(2, other_terms={"source_id": "ACCESS-CM2"})
    query = Query(
        other_terms={
            "cmip6:source_id": ("ACCESS-CM2",),
            "source_id": ("ACCESS-CM2",),
        }
    )

    assert InMemoryCatalogue(entries=(stac, solr)).find(query) == ()
    # Either one on its own finds its own source.
    assert [
        entry.id
        for entry in InMemoryCatalogue(entries=(stac, solr)).find(
            Query(other_terms={"source_id": ("ACCESS-CM2",)})
        )
    ] == [2]


def test_a_dataset_with_no_grid_never_matches_a_grid_label(make_entry):
    # Facet values are strings, so there is nothing a query could say that means
    # "the grid label is absent".
    entry = make_entry(1, grid_label=None)

    assert not matches(Query(grid_label="gn"), entry)


# ------------------------------------------------------------------ InMemoryCatalogue


def test_find_returns_matches_in_the_order_given(make_entry):
    entries = tuple(make_entry(entry_id, variable="tas") for entry_id in (3, 1, 2))

    found = InMemoryCatalogue(entries=entries).find(Query(variable="tas"))

    assert [entry.id for entry in found] == [3, 1, 2]


def test_find_on_an_empty_catalogue_finds_nothing():
    assert InMemoryCatalogue(entries=()).find(Query(variable="tas")) == ()


def test_duplicate_ids_are_refused(make_entry):
    with pytest.raises(DuplicateEntryIDError) as excinfo:
        InMemoryCatalogue(
            entries=(
                make_entry(7, label="a"),
                make_entry(5, label="b"),
                make_entry(7, label="c"),
                make_entry(5, label="d"),
                make_entry(7, label="e"),
            )
        )

    # Every repeated ID is reported, sorted, with how many entries carry it.
    assert excinfo.value.collisions == {5: 2, 7: 3}
    assert list(excinfo.value.collisions) == [5, 7]
    assert "id 5 is used by 2 entries" in str(excinfo.value)


# -------------------------------------------------------------------------- structural


def test_in_memory_catalogue_satisfies_the_catalogue_protocol():
    """
    `InMemoryCatalogue` still fits `Catalogue`

    Nothing declares the relationship: a protocol is satisfied by shape, so the two
    can drift apart silently. The signature comparison is what actually catches
    that here, because `make checks` runs mypy over `src` only, so the annotation
    below is documentation rather than a check.
    """
    catalogue: Catalogue = InMemoryCatalogue(entries=())

    assert inspect.signature(type(catalogue).find) == inspect.signature(Catalogue.find)
    assert catalogue.find(Query()) == ()
