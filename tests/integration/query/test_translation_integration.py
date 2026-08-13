"""
Tests of query translation using hypothesis

Hypothesis tests over many randomly generated queries without hand-writing
an expected result for each.
Using hypothesis is the cheapest way to gain confidence the
architecture holds for combinations we don't explicitly enumerate.
"""

from __future__ import annotations

import string

import pytest
from hypothesis import given
from hypothesis import strategies as st

from esmporium.query import (
    CANONICAL_FACETS,
    PROJECT_QUERY_MAP_DEFAULT,
    Query,
    QueryCMIP5,
    QueryCMIP6,
    QueryCMIP7,
    facet_spec,
    translate_to_projects,
    translate_to_type,
)

PROJECTS = ("CMIP5", "CMIP6", "CMIP6Plus", "CMIP7")

# Create a tiny alphabet of values we can use.
# Values are opaque to translation, so variety buys nothing.
TOKEN = st.text(
    alphabet=string.ascii_letters + string.digits + "-_.", min_size=1, max_size=6
)
VALUES = st.lists(TOKEN, min_size=1, max_size=3, unique=True).map(tuple)

# Prefixed so a drawn name can never collide with a real facet name,
# which would make a pass-through failure look like a translation failure.
OTHER_TERMS = st.dictionaries(
    TOKEN.map(lambda name: f"other_{name}"), VALUES, max_size=3
)

# Specify the translations here
# so they are stable to changes.
PROJECT_QUERIES = {
    "CMIP5": QueryCMIP5,
    "CMIP6": QueryCMIP6,
    "CMIP6Plus": QueryCMIP6,
    "CMIP7": QueryCMIP7,
}

_ABSENT_ANYWHERE = frozenset().union(
    *(facet_spec(cls).absent_canonical_facets for cls in PROJECT_QUERIES.values())
)

# Facets present in every project
# (we don't want anything here to hit errors in translation).
# `project` is special because `translate_to_projects` sets it.
COMMON_FACETS = sorted(CANONICAL_FACETS - _ABSENT_ANYWHERE - {"project"})


def test_all_projects_are_covered():
    """
    Ensure that every query we support is exercised by these tests
    """
    assert set(PROJECT_QUERY_MAP_DEFAULT) == set(PROJECT_QUERIES)

    for project, query_class in PROJECT_QUERY_MAP_DEFAULT.items():
        assert PROJECT_QUERIES[project] is query_class


@st.composite
def canonical_content(draw) -> dict[str, tuple[str, ...]]:
    """A non-empty assignment of values to a subset of the common canonical facets."""
    chosen = draw(st.lists(st.sampled_from(COMMON_FACETS), unique=True, min_size=1))
    return {facet: draw(VALUES) for facet in chosen}


@pytest.mark.parametrize("project", PROJECTS, ids=PROJECTS)
@given(data=st.data())
def test_round_trip_identity(project: str, data: st.DataObject):
    """
    A query, rendered back itself, is unchanged.

    `other_terms` is drawn too: it is carried through untranslated, so it has to
    survive the round trip just as the declared facets do.
    """
    query_cls = PROJECT_QUERIES[project]

    fields = list(facet_spec(query_cls).facet_names)

    chosen = data.draw(st.lists(st.sampled_from(fields), unique=True, min_size=1))
    content = {field: data.draw(VALUES) for field in chosen}
    other_terms = data.draw(OTHER_TERMS)

    start = query_cls(**content, other_terms=other_terms)
    result = translate_to_type(start, to=type(start))

    assert result == query_cls(**content, other_terms=other_terms, source_query=start)


@given(content=canonical_content())
def test_result_independent_of_input_language(content):
    """
    No matter how we go from one project to another, the result is the same
    """
    if "project" not in content:
        # set project so that we don't get query defaults
        content["project"] = "common"

    canonical = Query(**content)
    via_cmip5 = QueryCMIP5(
        **{facet_spec(QueryCMIP5).canonical_to_native[f]: v for f, v in content.items()}
    )
    via_cmip6 = QueryCMIP6(
        **{facet_spec(QueryCMIP6).canonical_to_native[f]: v for f, v in content.items()}
    )

    baseline = {
        k: v.model_copy(update={"source_query": None})
        for k, v in translate_to_projects(canonical, projects=PROJECTS).items()
    }

    for start in (via_cmip5, via_cmip6):
        result = translate_to_projects(start, projects=PROJECTS)

        assert all(v.source_query is start for v in result.values())

        result_comparable = {
            k: v.model_copy(update={"source_query": None}) for k, v in result.items()
        }
        assert result_comparable == baseline


@given(value=TOKEN)
def test_values_are_never_rewritten(value: str):
    """
    Only the facet name ever changes.

    Values never change, they are the users to control
    """
    query = Query(experiment=value)

    for project in PROJECTS:
        native = facet_spec(PROJECT_QUERIES[project]).canonical_to_native["experiment"]
        translated = translate_to_type(query, to=PROJECT_QUERIES[project])

        assert getattr(translated, native) == (value,)
