"""
Property-based tests of the translation "laws" (using Hypothesis package).

(1) a project's own query, rendered back to that project is unchanged.
(2)output depends only on canonical content and target project — never
on which input dialect typed it.
(3) facet search values (e.g. 'tas' for variable) is never changed.

Hypothesis tests over many randomly generated queries — without hand-writing
an expected dict for each. They are the cheapest way to gain confidence the
architecture holds for combinations no one enumerated.
"""

from __future__ import annotations

import string

import pytest
from hypothesis import given
from hypothesis import strategies as st

from esmporium.esgf import (
    CANONICAL_FACETS,
    ESGFQuery,
    ESGFQueryCMIP5,
    ESGFQueryCMIP6,
    ESGFQueryCMIP7,
    translate,
)
from esmporium.esgf.project_translation_maps import (
    CMIP5_PROFILE,
    CMIP6_PROFILE,
    CMIP7_PROFILE,
)

PROJECTS = ("CMIP5", "CMIP6", "CMIP7")

# A tiny, comma-free value alphabet. Values are opaque, so variety buys nothing;
# a comma would collide with the OR-separator in a rendered param.
TOKEN = st.text(
    alphabet=string.ascii_letters + string.digits + "-_.", min_size=1, max_size=6
)
VALUES = st.lists(TOKEN, min_size=1, max_size=3, unique=True).map(tuple)

# Facets present in every project (nothing here can trip the fail-loud rule).
_ABSENT_ANYWHERE = (
    CMIP5_PROFILE.absent_facets
    | CMIP6_PROFILE.absent_facets
    | CMIP7_PROFILE.absent_facets
)
COMMON_FACETS = sorted(CANONICAL_FACETS - _ABSENT_ANYWHERE)

PROJECT_SKINS = {
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


@pytest.mark.parametrize("project", PROJECTS, ids=PROJECTS)
@given(data=st.data())
def test_round_trip_identity(project: str, data: st.DataObject):
    """
    Law 1: a project's own query, rendered back to that project, is unchanged.

    Building a skin in project X's native words and rendering to X reproduces
    exactly those words and values (plus the `project` selector). This is
    `to_canonical` then `render_X` composing to the identity on project X's
    vocabulary.
    """
    skin_cls = PROJECT_SKINS[project]
    fields = [n for n in skin_cls.model_fields if n not in ("project", "other_terms")]

    chosen = data.draw(st.lists(st.sampled_from(fields), unique=True, min_size=1))
    content = {field: data.draw(VALUES) for field in chosen}

    result = translate(skin_cls(**content))[project]

    expected = {field: ",".join(values) for field, values in content.items()}
    expected["project"] = project
    assert result == expected


@given(content=canonical_content())
def test_result_independent_of_input_dialect(content):
    """
    Law 2: output depends only on canonical content and
    target project — never on which input dialect typed it.

    The same content, expressed via the unified skin, the CMIP5 skin and the CMIP6
    skin, must render identically to every project.
    """
    unified = ESGFQuery(**content)
    via_cmip5 = ESGFQueryCMIP5(
        **{CMIP5_PROFILE.native_facet(f): v for f, v in content.items()}
    )
    via_cmip6 = ESGFQueryCMIP6(
        **{CMIP6_PROFILE.native_facet(f): v for f, v in content.items()}
    )

    for project in PROJECTS:
        baseline = translate(unified, projects=[project])[project]
        assert translate(via_cmip5, projects=[project])[project] == baseline
        assert translate(via_cmip6, projects=[project])[project] == baseline


@given(value=TOKEN)
def test_values_are_never_rewritten(value: str):
    """
    Law 3: names are ours, values are users.

    A facet value comes out byte-identical in every project; only the facet name
    changes (`experiment` -> `experiment_id`).
    """
    query = ESGFQuery(experiment=value)

    for project in PROJECTS:
        native = PROFILES[project].native_facet("experiment")
        assert translate(query, projects=[project])[project][native] == value
