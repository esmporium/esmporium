"""
Tests for flattening a stored raw search document into `{facet_name: value}`.

`normalise_stored_document` dispatches on the `search_api_tag` recorded with each
document (no shape sniffing): the tag names the format, and a registry maps it to the
flattener. The default registry covers the search APIs we ship; it is injectable so a
user who bypasses our facade can supply the flattener for their own tag, and an unknown
tag raises rather than guessing. The difference-reporting these feed is tested in
`tests/unit/db/test_dataset_uniqueness.py`.
"""

from __future__ import annotations

import pytest

from esmporium.search import (
    DEFAULT_NORMALISERS,
    SOLR_FORMAT_TAG,
    STAC_FORMAT_TAG,
    UnknownRawDocFormatTagError,
    normalise_stored_document,
)
from esmporium.search.apis import (
    SearchAPIESGF1Solr,
    SearchAPIESGF15BridgeSolr,
    SearchAPIESGFNGSTAC,
)


def test_solr_tag_unwraps_top_level_single_element_lists():
    """The `solr` tag flattens a Solr doc: single-element lists become scalars."""
    doc = {
        "master_id": "cmip5.output1.CMCC.CMCC-CM.piControl.mon.atmos.Amon.r1i1p1",
        "product": ["output1"],
        "variable": ["tas", "rlut"],  # genuine multi-value: left as a list
        "version": "20121008",  # already scalar
    }

    result = normalise_stored_document(doc, SOLR_FORMAT_TAG)

    assert result == {
        "master_id": "cmip5.output1.CMCC.CMCC-CM.piControl.mon.atmos.Amon.r1i1p1",
        "product": "output1",
        "variable": ["tas", "rlut"],
        "version": "20121008",
    }


def test_stac_tag_reads_properties_and_strips_cmip_prefix():
    """The `stac` tag reads `properties` and drops the `cmipN:` prefix."""
    feature = {
        "id": "some-feature",  # top-level, not a facet: ignored
        "properties": {"cmip7:activity_id": "ScenarioMIP", "cmip7:region": ["glb"]},
    }

    result = normalise_stored_document(feature, STAC_FORMAT_TAG)

    assert result == {"activity_id": "ScenarioMIP", "region": "glb"}


def test_unknown_tag_raises_rather_than_guessing():
    """A tag with no registered flattener is an error, not a shape guess."""
    with pytest.raises(UnknownRawDocFormatTagError) as excinfo:
        normalise_stored_document({"properties": {"x": "y"}}, "mystery-format")

    # The error names the offending tag and lists the tags it does know.
    assert "mystery-format" in str(excinfo.value)
    assert SOLR_FORMAT_TAG in excinfo.value.known
    assert STAC_FORMAT_TAG in excinfo.value.known


def test_injected_normalisers_handle_a_custom_tag():
    """A user who bypasses our facade can inject a flattener for their own tag."""

    def flatten_pipe_delimited(raw: dict) -> dict:
        return dict(pair.split("=", 1) for pair in raw["blob"].split("|"))

    normalisers = {**DEFAULT_NORMALISERS, "pipe": flatten_pipe_delimited}

    result = normalise_stored_document(
        {"blob": "product=output1|model=CMCC-CM"}, "pipe", normalisers
    )

    assert result == {"product": "output1", "model": "CMCC-CM"}


def test_injected_mapping_that_drops_defaults_no_longer_knows_solr():
    """Passing a bare mapping replaces the defaults; merge to keep ours as well."""
    with pytest.raises(UnknownRawDocFormatTagError):
        normalise_stored_document({}, SOLR_FORMAT_TAG, {"only-mine": dict})


@pytest.mark.parametrize(
    ("search_api", "expected_tag"),
    [
        (SearchAPIESGF1Solr, SOLR_FORMAT_TAG),
        (SearchAPIESGF15BridgeSolr, SOLR_FORMAT_TAG),
        (SearchAPIESGFNGSTAC, STAC_FORMAT_TAG),
    ],
)
def test_each_inbuilt_api_declares_its_tag(search_api, expected_tag):
    """Each shipped search API declares the tag its stored docs are normalised by."""
    assert search_api.search_api_tag == expected_tag


@pytest.mark.parametrize(
    "search_api",
    [SearchAPIESGF1Solr, SearchAPIESGF15BridgeSolr, SearchAPIESGFNGSTAC],
)
def test_every_inbuilt_tag_has_a_default_normaliser(search_api):
    """Our own APIs can never trip the unknown-tag error: each tag is registered.

    This guards the pairing between the two edits a new inbuilt API needs -- declaring
    a `search_api_tag` and registering a flattener for it -- so one cannot ship without
    the other.
    """
    assert search_api.search_api_tag in DEFAULT_NORMALISERS
