"""Unit tests for building generation-specific requests from a canonical query."""

import pytest

from esmporium.esgf.canonical import CanonicalQuery
from esmporium.esgf.search import (
    DEFAULT_LIMIT,
    SearchAPIGeneration,
    UnrepresentableFacetError,
    build_request,
    get_generation_config,
)

SOLR = get_generation_config(SearchAPIGeneration.ESGF1)
STAC = get_generation_config(SearchAPIGeneration.ESGF_NG_EAST)


# --------------------------------------------------------------------------- Solr


def test_solr_cmip6_maps_names_and_comma_joins_values():
    canonical = CanonicalQuery(
        model=("ACCESS-CM2",),
        experiment=("historical",),
        variable=("tas", "pr"),
    )
    request = build_request(canonical, "CMIP6", SOLR)
    assert request.method == "GET"
    assert request.json_body is None
    assert request.params["source_id"] == "ACCESS-CM2"
    assert request.params["experiment_id"] == "historical"
    # OR within a facet -> comma-joined.
    assert request.params["variable_id"] == "tas,pr"
    assert request.params["project"] == "CMIP6"
    assert request.params["type"] == "Dataset"
    assert request.params["format"] == "application/solr+json"
    assert request.params["limit"] == str(DEFAULT_LIMIT)


def test_solr_cmip5_uses_project_native_names():
    canonical = CanonicalQuery(
        model=("ACCESS1-0",),
        variant_label=("r1i1p1",),
        reporting_interval=("mon",),
    )
    request = build_request(canonical, "CMIP5", SOLR)
    assert request.params["model"] == "ACCESS1-0"
    assert request.params["ensemble"] == "r1i1p1"
    assert request.params["time_frequency"] == "mon"


def test_solr_always_sends_a_retracted_control():
    # ESGF1 excludes retracted by default, so a retracted param must always be
    # present to include them (per the design; value unverified as endpoints 501).
    request = build_request(CanonicalQuery(model=("X",)), "CMIP6", SOLR)
    assert "retracted" in request.params


def test_solr_passes_through_extra_facets_as_is():
    canonical = CanonicalQuery(extra_facets={"product": ("output1",)})
    request = build_request(canonical, "CMIP5", SOLR)
    assert request.params["product"] == "output1"


# --------------------------------------------------------------------------- STAC


def _properties(clause: dict) -> dict[str, list]:
    """Flatten a CQL2 filter (single clause or an ``and``) to {property: values}."""
    clauses = clause["args"] if clause.get("op") == "and" else [clause]
    out = {}
    for c in clauses:
        assert c["op"] == "in"
        prop = c["args"][0]["property"]
        out[prop] = c["args"][1]
    return out


def test_stac_builds_cql2_post_with_prefixed_names():
    canonical = CanonicalQuery(model=("ACCESS-CM2",), experiment=("historical",))
    request = build_request(canonical, "CMIP6", STAC)
    assert request.method == "POST"
    assert request.params == {}
    body = request.json_body
    assert body["collections"] == ["CMIP6"]
    assert body["filter-lang"] == "cql2-json"
    assert body["limit"] == DEFAULT_LIMIT
    props = _properties(body["filter"])
    assert props["cmip6:source_id"] == ["ACCESS-CM2"]
    assert props["cmip6:experiment_id"] == ["historical"]


def test_stac_single_facet_is_not_wrapped_in_and():
    request = build_request(CanonicalQuery(model=("ACCESS-CM2",)), "CMIP6", STAC)
    assert request.json_body["filter"]["op"] == "in"


def test_stac_or_within_a_facet_uses_in_list():
    request = build_request(CanonicalQuery(variable=("tas", "pr")), "CMIP6", STAC)
    props = _properties(request.json_body["filter"])
    assert props["cmip6:variable_id"] == ["tas", "pr"]


def test_stac_no_facets_sends_collection_only():
    request = build_request(CanonicalQuery(), "CMIP6", STAC)
    body = request.json_body
    assert body["collections"] == ["CMIP6"]
    assert body["limit"] == DEFAULT_LIMIT
    assert "filter" not in body


def test_stac_cmip7_processing_id_uses_variable_branding_suffix():
    request = build_request(CanonicalQuery(processing_id=("ap5",)), "CMIP7", STAC)
    props = _properties(request.json_body["filter"])
    assert "cmip7:variable_branding_suffix" in props


def test_stac_never_adds_a_retracted_filter():
    # STAC includes retracted by default; adding a filter would restrict (east) or
    # break (west). So the body must mention retracted nowhere.
    request = build_request(CanonicalQuery(model=("X",)), "CMIP6", STAC)
    assert "retracted" not in str(request.json_body)


def test_stac_limit_is_at_least_one():
    # STAC rejects limit=0 with a 400, so we must never emit it.
    request = build_request(CanonicalQuery(), "CMIP6", STAC)
    assert request.json_body["limit"] >= 1


# ---------------------------------------------------------- STAC extra_facets


def test_stac_prefixes_an_unprefixed_extra_facet():
    # A passthrough facet (project-native name) is namespaced with the project
    # prefix and emitted as an `in` clause, like a canonical one.
    canonical = CanonicalQuery(extra_facets={"sub_experiment_id": ("s1990",)})
    request = build_request(canonical, "CMIP6", STAC)
    props = _properties(request.json_body["filter"])
    assert props == {"cmip6:sub_experiment_id": ["s1990"]}


def test_stac_leaves_an_already_prefixed_extra_facet_untouched():
    # If the caller already namespaced the key, we must not double-prefix it.
    canonical = CanonicalQuery(extra_facets={"cmip6:sub_experiment_id": ("s1990",)})
    request = build_request(canonical, "CMIP6", STAC)
    props = _properties(request.json_body["filter"])
    assert "cmip6:sub_experiment_id" in props
    assert "cmip6:cmip6:sub_experiment_id" not in props


def test_stac_extra_facet_uses_the_target_projects_prefix():
    # The prefix follows the project being searched, not the query's dialect.
    canonical = CanonicalQuery(extra_facets={"region": ("global",)})
    request = build_request(canonical, "CMIP7", STAC)
    props = _properties(request.json_body["filter"])
    assert props == {"cmip7:region": ["global"]}


def test_stac_extra_facets_are_anded_with_canonical_facets():
    canonical = CanonicalQuery(
        model=("UKESM1-0-LL",), extra_facets={"sub_experiment_id": ("s1990",)}
    )
    request = build_request(canonical, "CMIP6", STAC)
    assert request.json_body["filter"]["op"] == "and"
    props = _properties(request.json_body["filter"])
    assert props["cmip6:source_id"] == ["UKESM1-0-LL"]
    assert props["cmip6:sub_experiment_id"] == ["s1990"]


# ------------------------------------------------------------- unrepresentable


def test_unrepresentable_facet_on_solr_raises():
    # CMIP5 has no grid concept, so grid_label cannot be expressed for it.
    with pytest.raises(UnrepresentableFacetError):
        build_request(CanonicalQuery(grid_label=("gn",)), "CMIP5", SOLR)


def test_facet_on_project_absent_from_stac_raises():
    # CMIP5 is not a STAC collection with a name map, so any facet is unrepresentable.
    with pytest.raises(UnrepresentableFacetError):
        build_request(CanonicalQuery(model=("ACCESS1-0",)), "CMIP5", STAC)
