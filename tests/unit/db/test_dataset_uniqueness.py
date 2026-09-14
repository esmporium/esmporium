"""
Tests for reporting the facets that distinguish datasets our model considers identical.
"""

from __future__ import annotations

import pytest

from esmporium.db import MISSING, facet_differences
from esmporium.search import (
    SOLR_FORMAT_TAG,
    STAC_FORMAT_TAG,
    normalise_stored_document,
)


def test_single_distinguishing_facet_two_datasets():
    """The classic CMIP5 case: two datasets differ only in `product`."""
    result = facet_differences(
        ((2015, {"product": "output1"}), (1031, {"product": "output2"})),
    )

    assert result == {"product": {2015: "output1", 1031: "output2"}}


def test_facets_all_agree_on_yields_nothing():
    """A facet every dataset shares is not a difference."""
    result = facet_differences(
        (
            (1, {"product": "output1", "model": "CMCC-CM"}),
            (2, {"product": "output2", "model": "CMCC-CM"}),
        ),
    )

    # `model` is shared, so only `product` is reported.
    assert result == {"product": {1: "output1", 2: "output2"}}


def test_no_differences_when_every_facet_agrees():
    """Datasets that agree on everything explain nothing about their id difference."""
    facets = {"product": "output1", "model": "CMCC-CM"}
    result = facet_differences(((1, facets), (2, dict(facets))))

    assert result == {}


def test_more_than_two_datasets_clash():
    """A clash can span more than two rows; each id keeps its own value."""
    result = facet_differences(
        (
            (10, {"activity_id": "CMIP", "source_id": "BCC-CSM2-MR"}),
            (11, {"activity_id": "CFMIP", "source_id": "BCC-CSM2-MR"}),
            (12, {"activity_id": "ScenarioMIP", "source_id": "BCC-CSM2-MR"}),
        ),
    )

    assert result == {
        "activity_id": {10: "CMIP", 11: "CFMIP", 12: "ScenarioMIP"},
    }


def test_facet_present_in_only_some_documents_is_marked_missing():
    result = facet_differences(
        (
            (1, {"product": "output1"}),
            (2, {"product": "output1", "replica": True}),
        ),
    )

    # `product` agrees; `replica` is present only for dataset 2.
    assert result == {"replica": {1: MISSING, 2: True}}


def test_facet_shared_as_missing_by_all_is_not_a_difference():
    result = facet_differences(
        (
            (1, {"product": "output1", "region": "glb"}),
            (2, {"product": "output2", "region": "glb"}),
        ),
    )

    assert result == {"product": {1: "output1", 2: "output2"}}


def test_repeated_dataset_id_is_rejected():
    with pytest.raises(ValueError, match="unique"):
        facet_differences(
            ((7, {"product": "output1"}), (7, {"product": "output2"})),
        )


def test_end_to_end_from_stored_solr_documents():
    """Two stored CMIP5 Solr documents flatten and compare to reveal `product`."""
    output1 = {"product": ["output1"], "model": ["CMCC-CM"]}
    output2 = {"product": ["output2"], "model": ["CMCC-CM"]}

    result = facet_differences(
        (
            (2015, normalise_stored_document(output1, SOLR_FORMAT_TAG)),
            (1031, normalise_stored_document(output2, SOLR_FORMAT_TAG)),
        ),
    )

    assert result == {"product": {2015: "output1", 1031: "output2"}}


def test_end_to_end_from_stored_stac_features():
    """Two stored STAC features flatten and compare to reveal `activity_id`."""
    cmip = {
        "properties": {"cmip7:activity_id": "CMIP", "cmip7:source_id": "BCC-CSM2-MR"}
    }
    scenario = {
        "properties": {
            "cmip7:activity_id": "ScenarioMIP",
            "cmip7:source_id": "BCC-CSM2-MR",
        }
    }

    result = facet_differences(
        (
            (10, normalise_stored_document(cmip, STAC_FORMAT_TAG)),
            (11, normalise_stored_document(scenario, STAC_FORMAT_TAG)),
        ),
    )

    assert result == {"activity_id": {10: "CMIP", 11: "ScenarioMIP"}}
