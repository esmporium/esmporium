"""
Structural invariants of the facet vocabulary and the query classes.

These tests never translate a real query. They check that the configuration in
[`canonical`][esmporium.query.canonical],
[`languages`][esmporium.query.languages] and
[`translate`][esmporium.query.translate] is internally consistent, so that a
mistake in a query class's annotations is caught here — at the source of the
error — rather than surfacing later as a wrong translation.

Most of what this file used to check is now unwriteable rather than untested: a
query class declares how each of its facets translates, in the field's own
annotation, so there is no second declaration for it to disagree with. What is
left is the handful of invariants which annotations alone cannot enforce, plus
the errors we raise when a class gets them wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, ClassVar

import pytest

from esmporium.query import (
    CANONICAL_FACETS,
    PROJECT_QUERY_MAP_DEFAULT,
    DuplicateCanonicalFacetError,
    FacetValues,
    MultipleFacetAnnotationsError,
    NoFacetsDeclaredError,
    NotACanonicalFacetError,
    Query,
    QueryCanonical,
    QueryCMIP5,
    QueryCMIP6,
    QueryCMIP7,
    QueryFacet,
    SourceQuery,
    UnannotatedFacetError,
    facet_spec,
)

ALL_QUERIES = [Query, QueryCMIP5, QueryCMIP6, QueryCMIP7]

by_query = pytest.mark.parametrize(
    "query_class", ALL_QUERIES, ids=[cls.__name__ for cls in ALL_QUERIES]
)


def test_canonical_facets_match_query_fields():
    """
    Test consistency of canonical vocabulary and query fields

    These must be the same set - this checks that is the case.
    """
    facet_fields = set(QueryCanonical.model_fields) - {
        # fields that aren't facets
        "language_specific_facets",
        "other_terms",
        "source_query",
    }

    assert facet_fields == set(CANONICAL_FACETS)


def test_the_canonical_query_names_every_canonical_facet():
    spec = facet_spec(Query)

    assert set(spec.canonical_to_native) == set(CANONICAL_FACETS)
    assert not spec.absent_canonical_facets
    assert not spec.language_specific_facets


def test_the_canonical_query_maps_each_facet_to_itself():
    spec = facet_spec(Query)
    identity = {
        canonical
        for canonical, native in spec.canonical_to_native.items()
        if canonical == native
    }

    assert identity == set(spec.canonical_to_native)


@by_query
def test_absent_facets(query_class):
    spec = facet_spec(query_class)

    assert spec.absent_canonical_facets == CANONICAL_FACETS - set(
        spec.canonical_to_native
    )
    assert spec.absent_canonical_facets.isdisjoint(set(spec.canonical_to_native))


@by_query
def test_language_specific_facets_are_not_rename_targets(query_class):
    """
    A native name is a rename target or a language-specific facet, not both.
    """
    spec = facet_spec(query_class)

    assert spec.language_specific_facets.isdisjoint(
        set(spec.canonical_to_native.values())
    )


@by_query
def test_native_and_canonical_facet_are_inverses(query_class):
    spec = facet_spec(query_class)

    for canonical, native in spec.canonical_to_native.items():
        assert spec.native_to_canonical[native] == canonical


@by_query
def test_language_specific_facets_do_not_have_a_canonical_equivalent(query_class):
    spec = facet_spec(query_class)

    for facet in spec.language_specific_facets:
        assert facet not in spec.native_to_canonical


@by_query
def test_facet_names_are_exactly_the_declared_fields(query_class):
    spec = facet_spec(query_class)
    declared_by_class = set(query_class.model_fields) - {"other_terms", "source_query"}

    assert set(spec.facet_names) == declared_by_class


@by_query
def test_every_facet_is_named_once(query_class):
    spec = facet_spec(query_class)

    assert len(spec.facet_names) == len(set(spec.facet_names))
    assert len(spec.canonical_to_native) == len(set(spec.canonical_to_native.values()))


def test_every_project_has_a_query_class():
    for query_class in PROJECT_QUERY_MAP_DEFAULT.values():
        assert query_class in ALL_QUERIES


def test_facet_spec_is_cached():
    """
    The spec is worked out once per class, not once per translation
    """
    assert facet_spec(QueryCMIP5) is facet_spec(QueryCMIP5)


def test_a_typo_in_a_canonical_equivalent_raises_immediately():
    """
    A canonical facet we do not have is caught as the annotation is written
    """
    with pytest.raises(NotACanonicalFacetError, match="'mdoel' is not a canonical"):
        QueryFacet("mdoel")


def test_an_unannotated_field_raises():
    """
    A field that is not defined as expected is an error

    Otherwise, we would have to guess and this makes processing much harder.
    The error names every offending field, whether it is a would-be facet
    (`ensemble`) or something else entirely (`notes`),
    and says which fields are allowed to go unannotated.
    """

    @dataclass
    class QueryMissingAnnotation:
        model: Annotated[FacetValues, QueryFacet("model")] = ()
        ensemble: FacetValues = ()
        notes: str = ""

    with pytest.raises(
        UnannotatedFacetError,
        match=(
            r"QueryMissingAnnotation\.ensemble, QueryMissingAnnotation\.notes\. "
            r"Only other_terms, source_query may go unannotated"
        ),
    ) as excinfo:
        facet_spec(QueryMissingAnnotation)

    assert excinfo.value.fields == ("ensemble", "notes")


def test_the_non_facet_fields_may_go_unannotated():
    """`other_terms` and `source_query` are the deliberate exceptions."""

    @dataclass
    class QueryWithEscapeHatches:
        model: Annotated[FacetValues, QueryFacet("model")] = ()
        other_terms: dict[str, tuple[str, ...]] | None = None
        source_query: SourceQuery = None

    assert facet_spec(QueryWithEscapeHatches).facet_names == ("model",)


def test_a_class_var_is_not_a_facet():
    """
    A ClassVar is shared by every instance, so it cannot hold one query's values

    As a result, it doesn't have to be annotated with QueryFacet
    """

    @dataclass
    class QueryWithClassVar:
        registry: ClassVar[str] = "somewhere"

        model: Annotated[FacetValues, QueryFacet("model")] = ()

    assert facet_spec(QueryWithClassVar).facet_names == ("model",)


def test_a_class_declaring_no_facets_raises():
    @dataclass
    class QueryWithNoFacets:
        other_terms: dict[str, tuple[str, ...]] | None = None

    with pytest.raises(NoFacetsDeclaredError, match="declares no query facets"):
        facet_spec(QueryWithNoFacets)


def test_a_field_with_more_than_one_query_facet_raises():
    """
    A field must say how it translates exactly once

    `Annotated` metadata is normally read last-one-wins, which here would mean
    a facet translating in a way the class visibly claims it does not.
    """

    @dataclass
    class QueryDoublyAnnotated:
        model: Annotated[
            FacetValues, QueryFacet("model"), QueryFacet("experiment")
        ] = ()

    with pytest.raises(
        MultipleFacetAnnotationsError,
        match=(
            r"QueryDoublyAnnotated\.model carries 2 `QueryFacet` annotations, "
            r"claiming: 'model', 'experiment'"
        ),
    ) as excinfo:
        facet_spec(QueryDoublyAnnotated)

    assert excinfo.value.field == "model"
    assert excinfo.value.declared == (QueryFacet("model"), QueryFacet("experiment"))


def test_a_field_annotated_as_both_a_facet_and_language_specific_raises():
    """A `QueryFacet(None)` alongside a named one is just as ambiguous."""

    @dataclass
    class QueryFacetAndLanguageSpecific:
        model: Annotated[FacetValues, QueryFacet("model"), QueryFacet(None)] = ()

    with pytest.raises(MultipleFacetAnnotationsError, match=r"claiming: 'model', None"):
        facet_spec(QueryFacetAndLanguageSpecific)


def test_two_facets_claiming_one_canonical_facet_raises():
    @dataclass
    class QueryAmbiguous:
        model: Annotated[FacetValues, QueryFacet("model")] = ()
        source_id: Annotated[FacetValues, QueryFacet("model")] = ()

    with pytest.raises(
        DuplicateCanonicalFacetError, match=r"\(model, source_id\).*'model'"
    ) as excinfo:
        facet_spec(QueryAmbiguous)

    assert excinfo.value.canonical == "model"
