"""
Test initialisation and the errors raised when initialising a query.

Facet values are normalised to a tuple *before* pydantic validates them
so there is a real risk of confusing messages
e.g. complaining about a tuple the user never typed.
These tests pin the message the user actually sees.
"""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from esmporium.query import Query, QueryCanonical, QueryCMIP6, to_canonical


def test_single_string_becomes_one_tuple():
    assert QueryCMIP6(source_id="ACCESS-CM2").source_id == ("ACCESS-CM2",)


@pytest.mark.parametrize(
    "query_cls, facet",
    [
        pytest.param(Query, "model", id="base"),
        pytest.param(QueryCMIP6, "source_id", id="cmip6"),
        pytest.param(QueryCanonical, "model", id="canonical"),
    ],
)
def test_error_names_the_value_the_user_passed(query_cls, facet: str):
    """
    A wrong-typed facet value is reported against the facet, and names what was typed.

    The normalisation wraps a lone value in a one-tuple. Reporting the failure
    against that tuple would point the user at an index (`model.0`) of an input
    they never indexed, so the failure is reported against the facet itself
    (`model`), with the value they actually passed (`1`, an `int`).
    """
    with pytest.raises(
        ValidationError, match=rf"{facet}\s*\n\s*Value error, Input should be a string"
    ) as excinfo:
        query_cls(**{facet: 1})

    (error,) = excinfo.value.errors()

    assert error["loc"] == (facet,)
    assert error["type"] == "value_error"
    assert error["input"] == 1


def test_error_names_the_offending_item_of_a_collection():
    """A wrong-typed item in a collection is reported at its own position."""
    with pytest.raises(ValidationError, match=r"model\.1") as excinfo:
        Query(model=["ACCESS-CM2", 2])

    (error,) = excinfo.value.errors()

    assert error["loc"] == ("model", 1)
    assert error["input"] == 2


def test_mapping_facet_value_raises():
    """
    A mapping is rejected, rather than silently mangled.

    A mapping is a `Collection`, so a plain `tuple()` would quietly keep only its
    keys, giving a query that searches for something the user never asked for.
    """
    value = {"model": "ACCESS-CM2"}

    with pytest.raises(
        ValidationError,
        match=r"model\s*\n\s*Value error, Input should be a string",
    ) as excinfo:
        Query(model=value)

    (error,) = excinfo.value.errors()

    assert error["loc"] == ("model",)
    assert error["type"] == "value_error"
    assert error["input"] == value


def test_bytes_facet_value_is_one_value():
    """
    Bytes are one value, not a collection of integers.

    Bytes are a `Collection`, so a plain `tuple()` would turn them into a tuple of
    integers. Handing them to pydantic whole gets them decoded instead.
    """
    assert Query(model=b"ACCESS-CM2").model == ("ACCESS-CM2",)


def test_unknown_facet_is_not_silently_accepted():
    """
    A facet a query does not have is a typo, so it must not be quietly ignored.

    `other_terms` is the deliberate escape hatch for facets we have not modelled.
    """
    with pytest.raises(ValidationError, match="ensemble"):
        QueryCMIP6(ensemble="r1i1p1f1")


def test_other_terms_none_is_no_other_terms():
    """`None` means "nothing extra", the same as leaving `other_terms` out."""
    assert Query(other_terms=None).other_terms == Query().other_terms == {}


@pytest.mark.parametrize(
    "mapping_attribute", ["language_specific_facets", "other_terms"]
)
def test_facet_extras_must_be_mappings(mapping_attribute: str):
    """
    Query specific facets and other terms must be mappings
    """
    with pytest.raises(
        ValidationError,
        match=rf"{mapping_attribute}.*\s*.*Input should be a valid dictionary",
    ) as excinfo:
        QueryCanonical(**{mapping_attribute: "made_up_facet"})

    (error,) = excinfo.value.errors()

    assert error["type"] == "dict_type"
    assert error["input"] == "made_up_facet"


@pytest.mark.parametrize(
    "mapping_attribute", ["language_specific_facets", "other_terms"]
)
def test_facet_bucket_values_are_reported_under_their_own_facet(
    mapping_attribute: str,
):
    """
    A bad value in a facet bucket is located by facet name, and no further.
    """
    with pytest.raises(
        ValidationError,
        match=rf"{mapping_attribute}\.made_up_facet\s*\n\s*Value error, "
        r"Input should be a string",
    ) as excinfo:
        QueryCanonical(**{mapping_attribute: {"made_up_facet": 1}})

    (error,) = excinfo.value.errors()

    assert error["loc"] == (mapping_attribute, "made_up_facet")
    assert error["type"] == "value_error"
    assert error["input"] == 1


def test_facet_bucket_values_keep_a_real_index():
    """
    A bad item in a collection in other_terms (or equivalent) is at its own position.
    """
    with pytest.raises(
        ValidationError, match=r"other_terms\.made_up_facet\.1"
    ) as excinfo:
        Query(other_terms={"made_up_facet": ["foo", 2]})

    (error,) = excinfo.value.errors()

    assert error["loc"] == ("other_terms", "made_up_facet", 1)
    assert error["input"] == 2


def test_source_query_is_not_validated():
    """
    `source_query` is annotated but deliberately not checked at runtime.

    It is typed as `QueryProtocol | None`, which a type checker holds you to, but
    a `Protocol` is not something pydantic can validate against. Rather than
    loosen the whole model with `arbitrary_types_allowed`, validation is switched
    off for this one field. It exists for debugging, so a wrong value here is a
    nuisance rather than a corrupt query.
    """
    assert QueryCanonical(source_query=42).source_query == 42


@pytest.mark.parametrize(
    "source_query",
    [
        pytest.param(None, id="none"),
        pytest.param(Query(model="ACCESS-CM2"), id="base"),
        pytest.param(QueryCMIP6(source_id="ACCESS-CM2"), id="cmip6"),
    ],
)
def test_source_query_accepts_a_query_or_nothing(source_query):
    assert QueryCanonical(source_query=source_query).source_query is source_query


def test_frozen_canonical_query_cannot_be_mutated():
    """The canonical form is immutable, so a translation cannot be edited underneath."""
    canonical = to_canonical(Query(model="ACCESS-CM2"))

    with pytest.raises(ValidationError, match=re.escape("frozen")):
        canonical.model = ("UKESM1-0-LL",)
