"""
Building the request which looks for a parent simulation

Every project on every API it has a facade for, because the whole reason this is a
method on the facade is that each pairing spells the same facets differently.
"""

from __future__ import annotations

import pytest

from esmporium.query import FacetNotExpressibleError, QueryCanonical
from esmporium.search import INBUILT_SEARCH_API_FACADE_STORE

SOLR_HOST = "esgf.nci.org.au"
BRIDGE_HOST = "esgf-node.ornl.gov"
NG_EAST = "search.east.esgf.io"
NG_WEST = "search.west.esgf.io"


def facade(project: str, host: str):
    """Get the real facade for one pairing."""
    return INBUILT_SEARCH_API_FACADE_STORE.get_api_facade_for_project_from_host(
        project, host
    )


def asked(request) -> dict:
    """
    Pull the facets out of a request, whichever shape it came in

    Values are normalised to lists: the ESGF-1.5 bridge renders a single-valued facet as
    a scalar where ESGF-1 renders a one-element list, and that difference is the API's
    business rather than something these tests are about.
    """
    if request.params is not None:
        return {
            key: value if isinstance(value, list) else [value]
            for key, value in request.params.items()
            if key not in ("format", "type", "limit", "distrib")
        }

    found = {}
    for clause in request.json_body["filter"]["args"]:
        prop, values = clause["args"]
        found[prop["property"]] = values

    return found


def a_parent(project: str, **extra) -> QueryCanonical:
    """A parent simulation, as `to_canonical_query` builds one."""
    return QueryCanonical(
        project=(project,),
        model=("UKESM1-0-LL",),
        experiment=("piControl",),
        variant_label=("r1i1p1f1",),
        variable=("tas",),
        **extra,
    )


@pytest.mark.parametrize(
    ("project", "host", "model_field", "variant_field", "variable_field"),
    (
        pytest.param(
            "CMIP5", SOLR_HOST, "model", "ensemble", "variable", id="cmip5-solr"
        ),
        pytest.param(
            "CMIP6",
            SOLR_HOST,
            "source_id",
            "variant_label",
            "variable_id",
            id="cmip6-solr",
        ),
        pytest.param(
            "CMIP6",
            BRIDGE_HOST,
            "source_id",
            "variant_label",
            "variable_id",
            id="cmip6-bridge",
        ),
        pytest.param(
            "CMIP7",
            SOLR_HOST,
            "source_id",
            "variant_label",
            "variable_id",
            id="cmip7-solr",
        ),
        pytest.param(
            "CMIP6",
            NG_EAST,
            "cmip6:source_id",
            "cmip6:variant_label",
            "cmip6:variable_id",
            id="cmip6-ng-east",
        ),
        pytest.param(
            "CMIP6",
            NG_WEST,
            "cmip6:source_id",
            "cmip6:variant_label",
            "cmip6:variable_id",
            id="cmip6-ng-west",
        ),
        pytest.param(
            "CMIP7",
            NG_EAST,
            "cmip7:source_id",
            "cmip7:variant_label",
            "cmip7:variable_id",
            id="cmip7-ng-east",
        ),
    ),
)
def test_every_pairing_sends_its_own_field_names(
    project, host, model_field, variant_field, variable_field
):
    """
    The one thing this method exists to get right

    CMIP5 says `model` and `ensemble` where CMIP6 says `source_id` and `variant_label`,
    and ESGF-NG prefixes everything with the collection.
    """
    request = facade(project, host).build_parent_dataset_search_request(
        a_parent(project), 100
    )
    found = asked(request)

    assert found[model_field] == ["UKESM1-0-LL"]
    assert found[variant_field] == ["r1i1p1f1"]
    assert found[variable_field] == ["tas"]


@pytest.mark.parametrize("host", (SOLR_HOST, NG_EAST))
def test_cmip7_processing_id_goes_out_as_variable_branding_suffix(host):
    """
    The bug this method fixes

    `QueryCMIP7` calls `processing_id` `branding_suffix`, but both CMIP7 APIs want
    `variable_branding_suffix`. A request rendered through the query class sends the
    wrong field name; going through the facade's parameters is the only correct path.
    """
    request = facade("CMIP7", host).build_parent_dataset_search_request(
        a_parent("CMIP7", processing_id=("tavg-h2m-hxy-u",)), 100
    )
    fields = set(asked(request))

    assert any(field.endswith("variable_branding_suffix") for field in fields)
    assert not any(
        field.endswith(":branding_suffix") or field == "branding_suffix"
        for field in fields
    )


def test_cmip5_drops_facets_it_cannot_express_rather_than_refusing():
    """
    CMIP5 has no grid, activity or resolution at all
    """
    canonical = a_parent("CMIP5", grid_label=("gn",), activity=("CMIP",))
    cmip5 = facade("CMIP5", SOLR_HOST)

    found = asked(cmip5.build_parent_dataset_search_request(canonical, 100))

    assert found["model"] == ["UKESM1-0-LL"]
    assert "grid_label" not in found
    assert "activity_id" not in found

    with pytest.raises(FacetNotExpressibleError):
        cmip5.build_search_request(canonical, 100)


def test_a_query_specific_facet_a_facade_cannot_express_is_dropped_too():
    """
    Dropping is not limited to the facets we model

    A query carries its project's own un-canonical facets under their published names
    (CMIP5's `product`), and one of those is as unaskable on another project's facade
    as a canonical facet would be.
    """
    canonical = a_parent("CMIP6", query_specific_facets={"product": ("output1",)})

    narrowed = facade("CMIP6", SOLR_HOST).drop_unaskable_facets(canonical)

    assert narrowed.query_specific_facets == {}
    assert narrowed.model == ("UKESM1-0-LL",)


def test_the_stac_facades_send_the_project_as_a_collection():
    """
    Project is not a facet for ESGF-NG, it is the collection

    So it must survive the facet-dropping, which is the one exception
    `drop_unaskable_facets` makes.
    """
    found = asked(
        facade("CMIP7", NG_EAST).build_parent_dataset_search_request(
            a_parent("CMIP7"), 100
        )
    )

    assert found["collection"] == ["CMIP7"]


def test_nothing_is_copied_when_there_is_nothing_to_drop():
    """The common case leaves the query alone rather than rebuilding it."""
    canonical = a_parent("CMIP6")

    assert facade("CMIP6", SOLR_HOST).drop_unaskable_facets(canonical) is canonical
