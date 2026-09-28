"""
Tests of the catalogue: entries, the facets a query sets, and matching

Two of these are worth more than the rest, because they are what breaks quietly:
the import-boundary test, which fails when this package reaches into
`esmporium.search`, and the protocol-conformance test,
which fails when `InMemoryCatalogue` stops fitting `Catalogue`.

`CatalogueEntry` is checked against `Dataset` in `tests/unit/test_schema.py`, beside
the same check for `DatasetFacets`, because that is the file someone opens when they
change `Dataset`.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
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
    set_facets,
)
from esmporium.requirements import catalogue as catalogue_module

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
    # The columns are interpolated rather than spelled out, so adding a facet to
    # `Dataset` does not mean editing this string.
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
    #
    # Correct at this level, and a sharp edge one level up: see the `TODO(R2)` note
    # in `catalogue.py`. An empty query on a requirement's leaf would mean "any
    # dataset in the catalogue fills this role", which is much more likely to be a
    # half-written requirement than an intention, and which surfaces late — as an
    # ambiguous group with thousands of candidates — rather than where the mistake
    # was made. If `Leaf` grows that check at R2, this test stays as it is: the
    # catalogue is still right to answer the question as asked.
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


def esmporium_modules_imported_by(module) -> set[str]:
    """
    Get the esmporium modules a module imports, read off its source

    Read off the source rather than `sys.modules`, because `esmporium.db.schema`
    itself imports `esmporium.search.health`: anything which followed imports
    transitively would report `esmporium.search` for a reason that has nothing to do
    with this package.
    """
    tree = ast.parse(pathlib.Path(module.__file__).read_text())

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    return {name for name in imported if name.split(".")[0] == "esmporium"}


def test_catalogue_never_imports_search():
    """
    The hard half of the import boundary: nothing from `esmporium.search`

    Only `esmporium.search` is refused. What this package takes from `esmporium.db`
    is expected to grow, because the database-backed catalogue will need a session
    and the `Dataset` table.
    """
    offending = {
        name
        for name in esmporium_modules_imported_by(catalogue_module)
        if name.split(".")[:2] == ["esmporium", "search"]
    }

    assert not offending, (
        f"{sorted(offending)} would make a circular import once `search` takes "
        "`Requirement` objects. Put anything shared in `esmporium.query` instead."
    )
