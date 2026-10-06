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

from esmporium.db.schema import DATASET_FACET_COLUMNS
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
    UnrecordedFacetError,
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


def test_facet_reads_extra(make_entry):
    assert make_entry(1).extra == {}

    entry = make_entry(1, extra={"product": "output1", "realm": None})

    assert entry.facet("product") == "output1"
    # A facet `extra` records as `None` is known to have no value, which is not the
    # same as a facet `extra` has never heard of.
    assert entry.facet("realm") is None


def test_facet_error_names_the_columns_and_extra(make_entry):
    entry = make_entry(7)

    with pytest.raises(UnrecordedFacetError) as excinfo:
        entry.facet("nonsense")

    error = excinfo.value
    assert error.facets == ("nonsense",)
    assert error.entry_id == 7

    # The whole message, pinned once, here. `match=` is `re.search`, so it cannot
    # notice text added to either end of a message; this can. The other tests use
    # `match=` on a fragment and lean on this one for the wording, so there is one
    # place to edit when the wording changes rather than four.
    #
    # The columns are interpolated rather than spelled out, so adding a facet to
    # `Dataset` does not mean editing this. Their *order* is pinned by
    # `test_catalogue_entry_mirrors_dataset_columns`, not here.
    assert str(error) == (
        "Cannot select datasets on nonsense (asked of entry 7): "
        f"every dataset records {', '.join(DATASET_FACET_COLUMNS)}. "
        "For project-specific facets (e.g. CMIP5 `product`), "
        "or facets we can search but do not store "
        "(e.g. `activity`, `realm` and `resolution`), "
        "the catalogue has to put it in each entry's `extra`."
    )


@pytest.mark.parametrize("name", ["id", "id_project_specific"])
def test_facet_refuses_identifiers(name: str, make_entry):
    # These say *which* dataset an entry is, not what it is like, so selecting on
    # them is not what `facet` is for.
    #
    # The trailing space in the match matters: without it, `id` would also match the
    # message about `id_project_specific`.
    with pytest.raises(
        UnrecordedFacetError, match=re.escape(f"Cannot select datasets on {name} ")
    ):
        make_entry(1).facet(name)


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
    facets = set_facets(Query(variable="tas", experiment="historical"))

    assert matches_facets(facets, entry)
    assert not matches_facets(set_facets(Query(variable="pr")), entry)
    # Nothing to check means nothing to fail, as with an empty query one level up.
    assert matches_facets({}, entry)


@pytest.mark.parametrize(
    ("variable", "expected"),
    [pytest.param("tas", True, id="matches"), pytest.param("pr", False, id="does-not")],
)
def test_matches_facets_and_matches_agree(make_entry, variable, expected):
    """Parametrised both ways, so this cannot pass by both answers being `False`"""
    entry = make_entry(1, variable="tas")
    query = Query(variable=variable, project=entry.project)

    assert matches(query, entry) is expected
    assert matches_facets(set_facets(query), entry) is expected


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


def test_finding_an_unrecorded_facet_raises(make_entry):
    # The failure has to survive the trip out through `find`, not just be raised
    # inside `facet`: swallowing it on the way would quietly turn "we cannot answer
    # that" into "nothing matched", which is the one answer we must never invent.
    catalogue = InMemoryCatalogue(entries=(make_entry(1), make_entry(2)))

    # Only enough of the message to show it is about the facet we asked for; the
    # whole message is pinned in `test_facet_error_names_the_columns_and_extra`.
    with pytest.raises(UnrecordedFacetError, match="activity"):
        catalogue.find(Query(activity="CMIP"))


def test_other_terms_are_facets_too(make_entry):
    # `other_terms` names a facet no query class models, so an entry can only answer
    # it from `extra` -- but once it does, it narrows a search like any other facet.
    entries = (
        make_entry(1, extra={"driving_model": "ACCESS-CM2"}),
        make_entry(2, extra={"driving_model": "MIROC6"}),
    )
    query = Query(other_terms={"driving_model": ("ACCESS-CM2",)})

    found = InMemoryCatalogue(entries=entries).find(query)

    assert [entry.id for entry in found] == [1]


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
