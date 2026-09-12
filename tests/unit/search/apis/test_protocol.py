"""
Test the shared machinery for reporting a response we cannot read

The point of [`read_response_path`][esmporium.search.apis.read_response_path] is the
message it produces, not the lookup: a bare `KeyError` already does the lookup. So
these pin the message, path by path, because that message is the whole feature.

The errors built on top of it (`NoSearchResultDocumentsError` and friends) are
tested where they are raised, in `test_esgf1.py`, `test_esgfng.py` and
`test_result_parsers.py`.
"""

from __future__ import annotations

import re

import pytest

from esmporium.search.apis import (
    NoFacetValuesReturnedError,
    SearchAPIESGFNGSTAC,
    UnreadableResponseError,
    describe_search_api,
    read_response_path,
)
from esmporium.search.retry import build_transient_retrying

FEATURE = {
    "id": "an-id",
    "properties": {"version": "20200623", "latest": True},
}
"""A response shaped enough like a STAC feature to read paths out of"""


def test_reads_a_top_level_value():
    """The easy case: the key is there, so its value comes back"""
    assert read_response_path(FEATURE, "id", what="this record's id") == "an-id"


def test_reads_a_nested_value():
    """A dotted path walks down a level at a time"""
    assert (
        read_response_path(FEATURE, "properties.version", what="the version")
        == "20200623"
    )


def test_returns_the_value_whatever_its_type():
    """Reading is not checking: what type a value should be is the caller's business"""
    assert read_response_path(FEATURE, "properties.latest", what="latest") is True


def test_a_missing_top_level_key_names_the_keys_which_are_there():
    """Knowing what the response *did* carry is most of the diagnosis"""
    with pytest.raises(
        UnreadableResponseError,
        match=re.escape(
            "This response does not carry the project this record belongs to. "
            "We expected to read the project this record belongs to from "
            "'collection', but 'collection' is not in the response's top level, "
            "there is only: 'id', 'properties'."
        ),
    ):
        read_response_path(
            FEATURE, "collection", what="the project this record belongs to"
        )


def test_a_missing_nested_key_is_reported_at_the_level_it_ran_out():
    """The message blames `title` inside `properties`, not the whole path"""
    with pytest.raises(
        UnreadableResponseError,
        match=re.escape(
            "We expected to read the bundle id from 'properties.title', but "
            "'title' is not in 'properties', there is only: 'latest', 'version'."
        ),
    ):
        read_response_path(FEATURE, "properties.title", what="the bundle id")


def test_a_path_which_runs_into_something_that_is_not_a_mapping_says_so():
    """`id` is a string, so there is nothing under it to look in"""
    with pytest.raises(
        UnreadableResponseError,
        match=re.escape(
            "'title' is not in 'id', we found 'an-id' rather than keys at this path"
        ),
    ):
        read_response_path(FEATURE, "id.title", what="the bundle id")


def test_an_empty_response_says_it_is_empty():
    """There is nothing to enumerate, so the message does not try"""
    with pytest.raises(
        UnreadableResponseError, match=re.escape("but the response is empty.")
    ):
        read_response_path({}, "features", what="the documents")


def test_every_message_says_how_to_report_it():
    """A response we cannot read is a bug report we want, so we ask for it"""
    with pytest.raises(
        UnreadableResponseError,
        match=re.escape(
            "this is a bug in esmporium: please raise an issue at "
            "https://github.com/esmporium/esmporium/issues"
        ),
    ):
        read_response_path(FEATURE, "collection", what="the project")


def test_context_is_included_when_it_is_given():
    """Context is what makes a report reproducible, so it goes in the message"""
    api = SearchAPIESGFNGSTAC("search.example.io", build_transient_retrying(1))

    with pytest.raises(
        UnreadableResponseError,
        match=re.escape(
            "This response came from SearchAPIESGFNGSTAC at https://search.example.io."
        ),
    ):
        read_response_path(
            FEATURE, "collection", what="the project", context=describe_search_api(api)
        )


def test_the_lead_sentence_can_be_replaced():
    """Not everything we read is a live response, so the opening is overridable"""
    with pytest.raises(
        UnreadableResponseError,
        match=re.escape("This stored raw document is not shaped the way we expect."),
    ):
        read_response_path(
            FEATURE,
            "collection",
            what="the project",
            lead="This stored raw document is not shaped the way we expect.",
        )


def test_the_error_keeps_what_it_was_built_from():
    """Callers which want to handle this, rather than show it, need the pieces"""
    exc = UnreadableResponseError(
        FEATURE, ("a", "b"), what="the project", context="Somewhere."
    )

    assert exc.raw == FEATURE
    assert exc.expected_at == ("a", "b")
    assert exc.what == "the project"
    assert exc.context == "Somewhere."


def test_a_single_path_is_still_kept_as_a_tuple():
    """One place we looked or several, `expected_at` reads the same way"""
    assert UnreadableResponseError(FEATURE, "a", what="the project").expected_at == (
        "a",
    )


def test_the_named_subclasses_are_all_catchable_as_one():
    """`except UnreadableResponseError` is meant to catch the whole family"""
    assert issubclass(NoFacetValuesReturnedError, UnreadableResponseError)
