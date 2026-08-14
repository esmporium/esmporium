"""Unit tests for the search-API generation config and index-node registry."""

import pytest
from pydantic import ValidationError

from esmporium.esgf.canonical import CANONICAL_FACETS
from esmporium.esgf.search import (
    GENERATION_CONFIGS,
    KNOWN_NODES,
    IndexNode,
    SearchAPIGeneration,
    get_generation_config,
)


def test_every_generation_has_a_config():
    assert set(GENERATION_CONFIGS) == set(SearchAPIGeneration)


@pytest.mark.parametrize("generation", list(SearchAPIGeneration))
def test_get_generation_config_returns_matching_config(generation):
    config = get_generation_config(generation)
    assert config.generation is generation


@pytest.mark.parametrize("generation", list(SearchAPIGeneration))
def test_facet_name_maps_use_only_canonical_facets(generation):
    # The keys of every per-project name map must be real canonical facets, so a
    # typo cannot silently invent a facet the rest of the system does not know.
    config = get_generation_config(generation)
    for per_project in config.facet_names.values():
        assert set(per_project).issubset(CANONICAL_FACETS)


def test_cql2_flag_matches_generation_family():
    assert get_generation_config(SearchAPIGeneration.ESGF1).builds_cql2 is False
    assert get_generation_config(SearchAPIGeneration.ESGF_NG_EAST).builds_cql2 is True
    assert get_generation_config(SearchAPIGeneration.ESGF_NG_WEST).builds_cql2 is True


def test_east_and_west_share_stac_data():
    east = get_generation_config(SearchAPIGeneration.ESGF_NG_EAST)
    west = get_generation_config(SearchAPIGeneration.ESGF_NG_WEST)
    assert east.facet_names == west.facet_names
    assert east.collection_ids == west.collection_ids


def test_stac_property_names_are_prefixed():
    stac = get_generation_config(SearchAPIGeneration.ESGF_NG_EAST)
    assert stac.facet_names["CMIP6"]["model"] == "cmip6:source_id"
    assert stac.facet_names["CMIP7"]["model"] == "cmip7:source_id"
    assert all(name.startswith("cmip6:") for name in stac.facet_names["CMIP6"].values())
    assert all(name.startswith("cmip7:") for name in stac.facet_names["CMIP7"].values())


def test_stac_cmip7_processing_id_is_variable_branding_suffix():
    # The live-verified special case: mechanically prefixing the project-native
    # `branding_suffix` would be wrong; the STAC property is variable_branding_suffix.
    stac = get_generation_config(SearchAPIGeneration.ESGF_NG_EAST)
    assert (
        stac.facet_names["CMIP7"]["processing_id"] == "cmip7:variable_branding_suffix"
    )


def test_stac_collection_ids_are_uppercase():
    stac = get_generation_config(SearchAPIGeneration.ESGF_NG_WEST)
    assert stac.collection_ids == {"CMIP6": "CMIP6", "CMIP7": "CMIP7"}


def test_solr_names_are_project_native():
    solr = get_generation_config(SearchAPIGeneration.ESGF1)
    assert solr.facet_names["CMIP6"]["model"] == "source_id"
    assert solr.facet_names["CMIP5"]["model"] == "model"
    assert solr.facet_names["CMIP5"]["variant_label"] == "ensemble"
    assert solr.facet_names["CMIP5"]["reporting_interval"] == "time_frequency"
    assert solr.facet_names["CMIP5"]["processing_id"] == "cmor_table"
    assert solr.collection_ids == {}


def test_search_url_construction():
    stac = get_generation_config(SearchAPIGeneration.ESGF_NG_WEST)
    solr = get_generation_config(SearchAPIGeneration.ESGF1)
    assert stac.search_url("discovery.west.esgf.io") == (
        "https://discovery.west.esgf.io/search"
    )
    assert solr.search_url("esgf.ceda.ac.uk") == (
        "https://esgf.ceda.ac.uk/esg-search/search"
    )


def test_index_node_is_immutable():
    node = IndexNode(
        host="discovery.west.esgf.io", generation=SearchAPIGeneration.ESGF_NG_WEST
    )
    with pytest.raises(ValidationError):
        node.host = "somewhere.else"


def test_known_nodes_cover_both_ng_deployments():
    by_generation = {node.generation: node.host for node in KNOWN_NODES}
    assert by_generation[SearchAPIGeneration.ESGF_NG_EAST] == "api.stac.esgf.ceda.ac.uk"
    assert by_generation[SearchAPIGeneration.ESGF_NG_WEST] == "discovery.west.esgf.io"


def test_every_known_node_has_a_known_generation():
    for node in KNOWN_NODES:
        assert node.generation in GENERATION_CONFIGS
