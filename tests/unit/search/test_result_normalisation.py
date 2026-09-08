"""
Tests for flattening a stored raw search document into `{facet_name: value}`.

`normalise_stored_document` picks its flattener from the document's shape (no
generation flag): a STAC feature nests facets under `properties` with `cmipN:` keys,
while a Solr document puts them top-level as single-element lists. The Solr and STAC
handlers are deliberately uncoupled; the difference-reporting they feed is tested in
`tests/unit/db/test_dataset_uniqueness.py`.
"""

from __future__ import annotations

from esmporium.search import normalise_stored_document


def test_solr_top_level_single_element_lists_are_unwrapped():
    """A Solr doc's single-element list facets become scalars."""
    doc = {
        "master_id": "cmip5.output1.CMCC.CMCC-CM.piControl.mon.atmos.Amon.r1i1p1",
        "product": ["output1"],
        "institute": ["CMCC"],
        "version": "20121008",  # already scalar
    }

    result = normalise_stored_document(doc)

    assert result == {
        "master_id": "cmip5.output1.CMCC.CMCC-CM.piControl.mon.atmos.Amon.r1i1p1",
        "product": "output1",
        "institute": "CMCC",
        "version": "20121008",
    }


def test_solr_multi_element_list_is_left_as_a_list():
    """A genuine multi-value facet (e.g. CMIP5 variables) is not collapsed."""
    doc = {"variable": ["tas", "rlut"], "product": ["output1"]}

    result = normalise_stored_document(doc)

    assert result == {"variable": ["tas", "rlut"], "product": "output1"}


def test_stac_properties_are_read_and_cmip_prefix_is_stripped():
    """A STAC feature's facets live under `properties` with `cmipN:` prefixes."""
    feature = {
        "properties": {
            "cmip7:activity_id": "ScenarioMIP",
            "cmip7:region": "glb",
        },
    }

    result = normalise_stored_document(feature)

    assert result == {"activity_id": "ScenarioMIP", "region": "glb"}


def test_stac_single_element_lists_are_unwrapped_too():
    """STAC values given as single-element lists are unwrapped like Solr's."""
    feature = {"properties": {"cmip7:realm": ["landIce"], "cmip7:region": "glb"}}

    result = normalise_stored_document(feature)

    assert result == {"realm": "landIce", "region": "glb"}


def test_shape_router_prefers_stac_when_properties_present():
    """The `properties` key marks a STAC feature, so top-level keys are ignored."""
    feature = {
        "id": "some-feature",  # top-level, but this is a STAC feature
        "properties": {"cmip7:activity_id": "CMIP"},
    }

    result = normalise_stored_document(feature)

    # Only the properties facets are read; the top-level `id` is not treated as a facet.
    assert result == {"activity_id": "CMIP"}
