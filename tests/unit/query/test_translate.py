"""
Translation tests
"""

from __future__ import annotations

import pytest

from esmporium.query import (
    Query,
    QueryCanonical,
    QueryCMIP5,
    QueryCMIP6,
    from_canonical,
    to_canonical,
)


def test_project_facet_translates_like_any_other_facet():
    canonical = to_canonical(QueryCMIP5(model="ACCESS-CM2"))

    assert canonical.project == ("CMIP5",)


@pytest.mark.parametrize(
    "query",
    [
        pytest.param(QueryCMIP5(model="ACCESS-CM2"), id="translatable-facets-only"),
        pytest.param(
            QueryCMIP5(model="ACCESS-CM2", product="output1"),
            id="with-a-query-specific-facet",
        ),
    ],
)
def test_source_query_is_kept(query):
    assert to_canonical(query).source_query is query


def test_a_facet_is_renamed_values_are_unchanged():
    canonical = to_canonical(QueryCMIP5(ensemble=("r1i1p1f1", "r2i1p1f1")))

    assert canonical.variant_label == ("r1i1p1f1", "r2i1p1f1")


def test_a_query_specific_facet_stays_put():
    """
    A facet with no canonical equivalent is held under its native name.

    There is nothing to translate it to, so it is kept as the user wrote it.
    """
    canonical = to_canonical(QueryCMIP5(model="ACCESS-CM2", product="output1"))

    assert canonical.language_specific_facets == {"product": ("output1",)}


def test_other_terms_are_carried_across_untouched():
    """`other_terms` is the escape hatch, so it is passed through as given."""
    canonical = to_canonical(
        Query(model="ACCESS-CM2", other_terms={"made_up_facet": "foo"})
    )

    assert canonical.other_terms == {"made_up_facet": ("foo",)}


def test_empty_query_specific_facet_does_not_trip_fail_loud():
    """
    A query-specific facet with no values asks for nothing, so don't fail
    """
    # If the user builds QueryCanonical by hand
    # and includes query-specific values that aren't used, don't raise.
    canonical = QueryCanonical(
        model="ACCESS-CM2", language_specific_facets={"product": ()}
    )

    assert from_canonical(canonical=canonical, to=QueryCMIP6) == QueryCMIP6(
        source_id="ACCESS-CM2"
    )


def test_an_empty_canonical_facet_leaves_the_targets_default_alone():
    """
    A facet nobody asked for is not written, so the target's own default stands.

    `QueryCMIP6` defaults `project` to `("CMIP6",)`.
    An empty canonical project means "not asked for",
    so it must not overwrite that default with "asked for nothing".
    In practice, people shouldn't be creating `QueryCanonical` instances
    so this slightly confusing aspect shouldn't be an issue.
    If it is, we need to improve our docs, not change behaviour.
    """
    result = from_canonical(canonical=QueryCanonical(model="ACCESS-CM2"), to=QueryCMIP6)

    assert result.facet_values() == {
        "project": ("CMIP6",),
        "source_id": ("ACCESS-CM2",),
    }
