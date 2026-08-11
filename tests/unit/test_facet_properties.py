"""
Property-based tests of the translation *laws* (Hypothesis).

Where the golden tests check exact outputs for chosen inputs, these check the
algebraic laws the hub-and-spoke design promises, over many randomly generated
queries — without hand-writing an expected dict for each. They are the cheapest
way to gain confidence the architecture holds for combinations no one enumerated.

Facet values are opaque (we never rewrite them), so the meaningful axis is *which
facets are set*, not the values; the value alphabet is deliberately tiny, and
excludes commas because commas are our OR-separator in a rendered param.
"""

from __future__ import annotations

import string

import pytest
from hypothesis import given
from hypothesis import strategies as st

from esmporium.db.esgf import (
    CANONICAL_FACETS,
    ESGFQuery,
    ESGFQueryCMIP5,
    ESGFQueryCMIP6,
    ESGFQueryCMIP7,
    translate,
)
from esmporium.db.esgf.mip_translation import (
    CMIP5_PROFILE,
    CMIP6_PROFILE,
    CMIP7_PROFILE,
)

ERAS = ("CMIP5", "CMIP6", "CMIP7")

# A tiny, comma-free value alphabet. Values are opaque, so variety buys nothing;
# a comma would collide with the OR-separator in a rendered param.
TOKEN = st.text(
    alphabet=string.ascii_letters + string.digits + "-_.", min_size=1, max_size=6
)
VALUES = st.lists(TOKEN, min_size=1, max_size=3, unique=True).map(tuple)

# Facets present in *every* era (nothing here can trip the fail-loud rule).
_ABSENT_ANYWHERE = (
    CMIP5_PROFILE.absent_facets
    | CMIP6_PROFILE.absent_facets
    | CMIP7_PROFILE.absent_facets
)
COMMON_FACETS = sorted(CANONICAL_FACETS - _ABSENT_ANYWHERE)

ERA_SKINS = {
    "CMIP5": ESGFQueryCMIP5,
    "CMIP6": ESGFQueryCMIP6,
    "CMIP7": ESGFQueryCMIP7,
}
PROFILES = {
    "CMIP5": CMIP5_PROFILE,
    "CMIP6": CMIP6_PROFILE,
    "CMIP7": CMIP7_PROFILE,
}


@st.composite
def canonical_content(draw) -> dict[str, tuple[str, ...]]:
    """A non-empty assignment of values to a subset of the common canonical facets."""
    chosen = draw(st.lists(st.sampled_from(COMMON_FACETS), unique=True, min_size=1))
    return {facet: draw(VALUES) for facet in chosen}


@pytest.mark.parametrize("era", ERAS, ids=ERAS)
@given(data=st.data())
def test_round_trip_identity(era: str, data: st.DataObject):
    """
    Law 1: an era's own query, rendered back to that era, is unchanged.

    Building a skin in era X's native words and rendering to X reproduces exactly
    those words and values (plus the `project` selector). This is `to_canonical`
    then `render_X` composing to the identity on era X's vocabulary.
    """
    skin_cls = ERA_SKINS[era]
    fields = [n for n in skin_cls.model_fields if n not in ("project", "other_terms")]

    chosen = data.draw(st.lists(st.sampled_from(fields), unique=True, min_size=1))
    content = {field: data.draw(VALUES) for field in chosen}

    result = translate(skin_cls(**content))[era]

    expected = {field: ",".join(values) for field, values in content.items()}
    expected["project"] = era
    assert result == expected


@given(content=canonical_content())
def test_hub_law_result_independent_of_input_dialect(content):
    """
    Law 2 (the heart of the design): output depends only on canonical content and
    target era — never on which input dialect typed it.

    The same content, expressed via the unified skin, the CMIP5 skin and the CMIP6
    skin, must render identically to every era.
    """
    unified = ESGFQuery(**content)
    via_cmip5 = ESGFQueryCMIP5(
        **{CMIP5_PROFILE.native_facet(f): v for f, v in content.items()}
    )
    via_cmip6 = ESGFQueryCMIP6(
        **{CMIP6_PROFILE.native_facet(f): v for f, v in content.items()}
    )

    for era in ERAS:
        baseline = translate(unified, projects=[era])[era]
        assert translate(via_cmip5, projects=[era])[era] == baseline
        assert translate(via_cmip6, projects=[era])[era] == baseline


@given(value=TOKEN)
def test_values_are_never_rewritten(value: str):
    """
    Law 3: names are ours, values are yours.

    A facet value comes out byte-identical in every era; only the facet *name*
    changes (`experiment` -> `experiment_id`).
    """
    query = ESGFQuery(experiment=value)

    for era in ERAS:
        native = PROFILES[era].native_facet("experiment")
        assert translate(query, projects=[era])[era][native] == value
