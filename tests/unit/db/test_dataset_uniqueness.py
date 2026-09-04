"""
Tests for reading the distinguishing facet out of raw search documents.

These use crafted raw documents shaped like the real ones (verified live this cycle):
CMIP5 Solr distinguishes on `product`, CMIP6 Solr on `activity_id`, and CMIP7 STAC
carries its facets in a `properties` dict under `cmipN:`-prefixed keys.
"""

from __future__ import annotations

from esmporium.db import facet_differences

# CMIP5: same dataset published under two products (the real CMCC-CM piControl case).
CMIP5_MASTER_A = "cmip5.output1.CMCC.CMCC-CM.piControl.mon.atmos.Amon.r1i1p1"
CMIP5_MASTER_B = "cmip5.output2.CMCC.CMCC-CM.piControl.mon.atmos.Amon.r1i1p1"


def _cmip5_doc(product: str, version: str) -> dict:
    """A CMIP5 Solr doc: facets are top-level, mostly single-element lists."""
    return {
        "master_id": f"cmip5.{product}.CMCC.CMCC-CM.piControl.mon.atmos.Amon.r1i1p1",
        "product": [product],
        "institute": ["CMCC"],
        "model": ["CMCC-CM"],
        "variable": ["tas", "rlut"],
        "version": version,  # differs, but is NOT in the master_id
        "data_node": ["esgf.ceda.ac.uk"],  # differs, but is NOT in the master_id
    }


def test_cmip5_product_is_the_only_difference():
    """The distinguishing facet is `product`; version/node noise is excluded."""
    result = facet_differences(
        _cmip5_doc("output1", "20121008"),
        _cmip5_doc("output2", "20170725"),
        CMIP5_MASTER_A,
        CMIP5_MASTER_B,
    )

    assert result == {"product": ("output1", "output2")}


def test_cmip6_activity_id_is_found():
    """CMIP6 clashes on `activity_id` (e.g. amip under both CMIP and CFMIP)."""
    master_a = "CMIP6.CMIP.BCC.BCC-CSM2-MR.amip.r1i1p1f1.Amon.tas.gn"
    master_b = "CMIP6.CFMIP.BCC.BCC-CSM2-MR.amip.r1i1p1f1.Amon.tas.gn"
    doc_a = {
        "master_id": master_a,
        "activity_id": ["CMIP"],
        "source_id": ["BCC-CSM2-MR"],
    }
    doc_b = {
        "master_id": master_b,
        "activity_id": ["CFMIP"],
        "source_id": ["BCC-CSM2-MR"],
    }

    result = facet_differences(doc_a, doc_b, master_a, master_b)

    assert result == {"activity_id": ("CMIP", "CFMIP")}


def test_stac_properties_are_read_and_prefixes_stripped():
    """CMIP7 STAC keeps facets in `properties` under `cmipN:` keys."""
    native_a = "MIP-DRS7.CMIP7.CMIP.INST.MODEL.exp.r1i1p1f1.glb.mon.tas.tavg.g110"
    native_b = (
        "MIP-DRS7.CMIP7.ScenarioMIP.INST.MODEL.exp.r1i1p1f1.glb.mon.tas.tavg.g110"
    )
    feature_a = {"properties": {"cmip7:activity_id": "CMIP", "cmip7:region": "glb"}}
    feature_b = {
        "properties": {"cmip7:activity_id": "ScenarioMIP", "cmip7:region": "glb"}
    }

    result = facet_differences(feature_a, feature_b, native_a, native_b)

    # The prefix is dropped, and `region` (identical) is not reported.
    assert result == {"activity_id": ("CMIP", "ScenarioMIP")}


def test_no_difference_when_documents_agree_on_identity_facets():
    """Docs differing only in non-identity fields (version) yield nothing."""
    result = facet_differences(
        _cmip5_doc("output1", "20121008"),
        _cmip5_doc("output1", "20200101"),  # only version differs
        CMIP5_MASTER_A,
        CMIP5_MASTER_A,
    )

    assert result == {}


# --- CMIP7 icesheet known issue ------------------------------------------------
#
# WCRP-CMIP/cmip7-cmor-tables#166: some branded variables appear in BOTH the atmos
# and landIce MIP tables (e.g. `orog_tavg-u-hxy-is`), as "a global version in landIce
# and a regional version in atmos", differing in `cell_measures`. Full affected list
# (v1.2.2.5): hfls, hfss, orog, prra, prsn, rlds, rlus, rsds, rsus, ts — all with the
# `tavg-u-hxy-is` branding.
#
# For our model this is a same-branded-variable clash: `variable_id` and the branding
# (our `processing_id`) are identical, and `realm` is not one of our columns. What
# distinguishes the two in the CMIP7 native id is `region` (`glb` vs a regional code):
# verified live, the STAC native id carries `region` but NOT `realm`.


def _cmip7_icesheet_feature(realm: str, region: str) -> dict:
    """A CMIP7 STAC feature for a `tavg-u-hxy-is` icesheet variable."""
    return {
        "properties": {
            "cmip7:variable_id": "orog",
            "cmip7:variable_branding_suffix": "tavg-u-hxy-is",
            "cmip7:realm": [realm],
            "cmip7:region": region,
            "cmip7:area_label": "is",
        }
    }


def _cmip7_icesheet_native_id(region: str) -> str:
    """The version/node-free native id; `region` sits between variant and frequency."""
    return (
        f"MIP-DRS7.CMIP7.CMIP.INST.MODEL.historical.r1i1p1f1"
        f".{region}.mon.orog.tavg-u-hxy-is.g110"
    )


def test_cmip7_icesheet_versions_are_distinguished_by_region():
    """The atmos-regional vs landIce-global pair is told apart by `region`.

    This is the real manifestation of the known issue: the two versions differ in
    `region` (`glb` global for landIce, a regional code for atmos), and `region` is in
    the native id, so our model keeps them as distinct datasets and reads out the
    difference.

    Note what is NOT reported: `realm` (`landIce` vs `atmos`) — which is the more
    meaningful difference, and the actual subject of the issue — because CMIP7 leaves
    `realm` out of the native id. Our disambiguation can only surface facets that are
    part of that id.
    """
    landice = _cmip7_icesheet_feature("landIce", "glb")
    atmos = _cmip7_icesheet_feature("atmos", "gris")  # illustrative regional code

    result = facet_differences(
        landice,
        atmos,
        _cmip7_icesheet_native_id("glb"),
        _cmip7_icesheet_native_id("gris"),
    )

    assert result == {"region": ("glb", "gris")}
    assert "realm" not in result  # realm is not in the native id, so it is invisible


def test_cmip7_realm_only_difference_is_invisible():
    """If the two versions shared a region, our model could not tell them apart.

    Here the only difference is `realm` (atmos vs landIce); everything in the native id
    is identical. `facet_differences` finds nothing, which is the warning sign: such a
    pair would collide in `Dataset` (an unresolvable clash), because neither `realm`
    nor the MIP table is part of our identity. A finding worth raising if the Data
    Request keeps same-region atmos/landIce duplicates.
    """
    native_id = _cmip7_icesheet_native_id("glb")

    result = facet_differences(
        _cmip7_icesheet_feature("landIce", "glb"),
        _cmip7_icesheet_feature("atmos", "glb"),
        native_id,
        native_id,
    )

    assert result == {}
