"""
Tests of the catalogue: records, the facets a query sets, and matching

Three things here are worth more than the rest, because they are what breaks
quietly:

- the import-boundary test, which fails when this package reaches into
  `esmporium.search` or the rest of `esmporium.db`;
- the flatten-once test, which fails when `find` goes back to re-reading the query
  for every record it checks.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from typing import Annotated

import pytest
from pydantic import BaseModel

from esmporium import requirements
from esmporium.db.schema import DATASET_FACET_COLUMNS
from esmporium.query import (
    FacetValues,
    FacetValuesByName,
    Query,
    QueryCanonical,
    QueryCMIP5,
    QueryCMIP6,
    QueryCMIP7,
    QueryFacet,
    SourceQuery,
)
from esmporium.requirements import (
    Catalogue,
    ClashingFacetError,
    DatasetRecord,
    DuplicateRecordIDError,
    InMemoryCatalogue,
    UnsupportedFacetError,
    matches,
    set_facets,
)
from esmporium.requirements import catalogue as catalogue_module


class QueryWithNativeRealm(BaseModel):
    """
    A query which can set `realm` twice over

    Synthetic, and deliberately so. `realm_field` translates to the canonical
    `realm`, while the field actually called `realm` has no canonical equivalent and
    so travels under its own name. That is the only way one query object can fill
    both the canonical bucket and the query-specific bucket for one name, which is a
    branch of `set_facets`'s clash check that the shipped query classes cannot reach.
    """

    realm_field: Annotated[FacetValues, QueryFacet("realm")] = ()
    realm: Annotated[FacetValues, QueryFacet(None)] = ()
    other_terms: FacetValuesByName = {}
    source_query: SourceQuery = None


# ---------------------------------------------------------------- DatasetRecord.facet


@pytest.mark.parametrize("column", DATASET_FACET_COLUMNS)
def test_facet_returns_every_column(column: str, make_record):
    record = make_record(1)

    assert record.facet(column) == getattr(record, column)


def test_facet_returns_none_for_a_dataset_with_no_grid():
    # CMIP5 has no concept of a grid, so this is a real value rather than a failure.
    record = DatasetRecord(
        id=1,
        id_project_specific="cmip5.output1.X.historical.mon.atmos.Amon.r1i1p1.v1",
        project="CMIP5",
        model="ACCESS1-0",
        institution="CSIRO-BOM",
        experiment="historical",
        variant_label="r1i1p1",
        variable="tas",
        reporting_interval="mon",
        grid_label=None,
        processing_id="Amon",
    )

    assert record.facet("grid_label") is None


def test_facet_reads_extra(make_record):
    record = make_record(1, extra={"product": "output1", "realm": None})

    assert record.facet("product") == "output1"
    # A facet `extra` records as `None` is known to have no value, which is not the
    # same as a facet `extra` has never heard of (below).
    assert record.facet("realm") is None


def test_facet_defaults_to_no_extra(make_record):
    record = make_record(1)

    assert record.extra == {}
    with pytest.raises(UnsupportedFacetError):
        record.facet("product")


def test_facet_error_names_the_columns_and_extra(make_record):
    record = make_record(7)

    with pytest.raises(UnsupportedFacetError) as excinfo:
        record.facet("nonsense")

    error = excinfo.value
    assert error.facets == ("nonsense",)
    assert error.record_id == 7
    message = str(error)
    assert "asked of record 7" in message
    for column in DATASET_FACET_COLUMNS:
        assert column in message
    assert "`extra`" in message


@pytest.mark.parametrize("name", ["id", "id_project_specific"])
def test_facet_refuses_identifiers(name: str, make_record):
    # These say *which* dataset a record is, not what it is like, so selecting on
    # them is not what `facet` is for.
    with pytest.raises(UnsupportedFacetError):
        make_record(1).facet(name)


def test_facet_error_explains_an_api_parameter_name(make_record):
    with pytest.raises(UnsupportedFacetError) as excinfo:
        make_record(1).facet("cmip6:experiment_id")

    assert "looks like a search API parameter name" in str(excinfo.value)


def test_facet_error_stays_quiet_about_api_names_otherwise(make_record):
    with pytest.raises(UnsupportedFacetError) as excinfo:
        make_record(1).facet("experiment_id")

    assert "looks like a search API parameter name" not in str(excinfo.value)


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
    ],
)
def test_set_facets_translates_any_query_style(query):
    facets = set_facets(query)

    # Whatever the style called them, they come back under the names our columns use.
    assert facets["experiment"] == ("historical",)
    assert facets["reporting_interval"] == ("mon",)


def test_set_facets_accepts_an_already_canonical_query():
    # `QueryCanonical`'s fields carry no `QueryFacet`, so translating it would fail;
    # it has to be recognised and used as it is.
    assert set_facets(QueryCanonical(variable="tas")) == {"variable": ("tas",)}


def test_set_facets_includes_query_specific_facets():
    facets = set_facets(QueryCMIP5(variable="tas", product="output1"))

    assert facets["product"] == ("output1",)


def test_set_facets_includes_other_terms_and_drops_empty_ones():
    facets = set_facets(
        Query(variable="tas", other_terms={"driving_model": ("X",), "nothing": ()})
    )

    assert facets == {"variable": ("tas",), "driving_model": ("X",)}


def test_set_facets_orders_canonical_facets_first_and_alphabetically():
    facets = set_facets(
        QueryCMIP5(variable="tas", product="output1", other_terms={"zzz": ("1",)})
    )

    assert list(facets) == ["project", "variable", "product", "zzz"]


@pytest.mark.parametrize(
    "query, expected",
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
        pytest.param(
            QueryWithNativeRealm(realm_field="atmos", realm="ocean"),
            ("realm",),
            id="canonical-vs-query-specific",
        ),
    ],
)
def test_set_facets_refuses_a_facet_set_twice(query, expected):
    with pytest.raises(ClashingFacetError) as excinfo:
        set_facets(query)

    assert excinfo.value.facets == expected


# ----------------------------------------------------------------------------- matches


def test_matches_on_one_facet(make_record):
    record = make_record(1, variable="tas")

    assert matches(Query(variable="tas"), record)
    assert not matches(Query(variable="pr"), record)


def test_several_values_for_one_facet_are_an_or(make_record):
    record = make_record(1, variable="tas")

    assert matches(Query(variable=("pr", "tas")), record)
    assert not matches(Query(variable=("pr", "rsdt")), record)


def test_an_empty_query_matches_everything(make_record):
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
    assert matches(Query(), make_record(1))


def test_matching_an_unrecorded_facet_raises(make_record):
    # The failure has to survive the trip out through `matches`, not just be raised
    # inside `facet`: swallowing it here would quietly turn "we cannot answer that"
    # into "nothing matched", which is the one answer we must never invent.
    with pytest.raises(UnsupportedFacetError):
        matches(Query(activity="CMIP"), make_record(1))


def test_finding_an_unrecorded_facet_raises(make_record):
    catalogue = InMemoryCatalogue(records=(make_record(1), make_record(2)))

    with pytest.raises(UnsupportedFacetError):
        catalogue.find(Query(activity="CMIP"))


def test_other_terms_are_facets_too(make_record):
    # `other_terms` names a facet no query class models, so a record can only answer
    # it from `extra` -- but once it does, it narrows a search like any other facet.
    records = (
        make_record(1, extra={"driving_model": "ACCESS-CM2"}),
        make_record(2, extra={"driving_model": "MIROC6"}),
    )
    query = Query(other_terms={"driving_model": ("ACCESS-CM2",)})

    found = InMemoryCatalogue(records=records).find(query)

    assert [record.id for record in found] == [1]


def test_a_dataset_with_no_grid_never_matches_a_grid_label(make_record):
    # Facet values are strings, so there is nothing a query could say that means
    # "the grid label is absent".
    record = make_record(1, grid_label=None)

    assert not matches(Query(grid_label="gn"), record)


def test_matches_agrees_with_find(make_record):
    records = (
        make_record(1, variable="tas"),
        make_record(2, variable="pr"),
        make_record(3, variable="tas"),
    )
    query = Query(variable="tas")

    found = InMemoryCatalogue(records=records).find(query)

    assert found == tuple(record for record in records if matches(query, record))


# ------------------------------------------------------------------ InMemoryCatalogue


def test_find_returns_matches_in_the_order_given(make_record):
    records = tuple(make_record(record_id, variable="tas") for record_id in (3, 1, 2))

    found = InMemoryCatalogue(records=records).find(Query(variable="tas"))

    assert [record.id for record in found] == [3, 1, 2]


def test_find_on_an_empty_catalogue_finds_nothing():
    assert InMemoryCatalogue(records=()).find(Query(variable="tas")) == ()


def test_duplicate_ids_are_refused(make_record):
    with pytest.raises(DuplicateRecordIDError) as excinfo:
        InMemoryCatalogue(
            records=(
                make_record(5, label="first"),
                make_record(5, label="second"),
                make_record(6, label="third"),
            )
        )

    assert excinfo.value.collisions == {5: 2}
    assert "id 5 is used by 2 records" in str(excinfo.value)


def test_every_duplicated_id_is_reported_in_order(make_record):
    with pytest.raises(DuplicateRecordIDError) as excinfo:
        InMemoryCatalogue(
            records=(
                make_record(7, label="a"),
                make_record(5, label="b"),
                make_record(7, label="c"),
                make_record(5, label="d"),
                make_record(7, label="e"),
            )
        )

    assert excinfo.value.collisions == {5: 2, 7: 3}
    assert list(excinfo.value.collisions) == [5, 7]


def test_find_reads_the_query_once_not_once_per_record(monkeypatch, make_record):
    # `find` is the hot loop: re-flattening the query for every record would turn one
    # cheap translation into one per dataset.
    #
    # `monkeypatch` swaps the module's `set_facets` for a wrapper which records each
    # call and then delegates, and puts the real one back when the test ends. `find`
    # looks `set_facets` up in its module's globals every time it runs, which is what
    # makes the swap visible from inside it. Five records, one call.
    real_set_facets = catalogue_module.set_facets
    calls = []

    def spy(query):
        calls.append(query)

        return real_set_facets(query)

    monkeypatch.setattr(catalogue_module, "set_facets", spy)
    catalogue = InMemoryCatalogue(
        records=tuple(make_record(record_id) for record_id in range(1, 6))
    )

    found = catalogue.find(Query(variable="variable-value"))

    assert len(found) == 5
    assert len(calls) == 1


# -------------------------------------------------------------------------- structural


def test_in_memory_catalogue_satisfies_the_catalogue_protocol():
    """
    `InMemoryCatalogue` still fits `Catalogue`

    Nothing declares the relationship: a protocol is satisfied by shape, so the two
    can drift apart silently. The signature comparison is what actually catches
    that here, because `make checks` runs mypy over `src` only, so the annotation
    below is documentation rather than a check.
    """
    catalogue: Catalogue = InMemoryCatalogue(records=())

    assert inspect.signature(type(catalogue).find) == inspect.signature(Catalogue.find)
    assert catalogue.find(Query()) == ()


def test_catalogue_imports_nothing_else_from_esmporium():
    """
    The package's import boundary holds

    Read off the source rather than `sys.modules`: `esmporium.db.schema` itself
    imports `esmporium.search.health`, so anything which followed imports
    transitively would fail for a reason that has nothing to do with this package.
    """
    tree = ast.parse(pathlib.Path(catalogue_module.__file__).read_text())

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    from_esmporium = {
        module for module in imported if module.split(".")[0] == "esmporium"
    }

    assert from_esmporium == {"esmporium.query", "esmporium.db.schema"}


def test_everything_public_is_exported():
    defined_here = {
        name
        for name, value in vars(catalogue_module).items()
        if not name.startswith("_")
        and getattr(value, "__module__", None) == catalogue_module.__name__
    }

    assert defined_here <= set(requirements.__all__)
    assert requirements.__all__ == sorted(requirements.__all__)
    for name in requirements.__all__:
        assert hasattr(requirements, name)
