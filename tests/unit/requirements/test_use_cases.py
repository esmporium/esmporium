"""
Tests that every use case builds, stores and can be satisfied

The point of this file is that it is parametrised over `USE_CASES` rather than
naming any one of them: a use case added later is checked by these same four
assertions, without anyone writing a test for it.
"""

from __future__ import annotations

import pytest

from esmporium.query import Query
from esmporium.requirements import Requirement, leaf, requirement, solve
from esmporium.requirements.tree import role_paths

# ---------------------------------------------------------------- the use cases

PATTERN_EFFECT = requirement(
    name="pattern-effect",
    tree=leaf(
        Query(variable="tas", experiment="historical", reporting_interval="mon"),
        "tas",
    ),
    group_by=("model", "variant_label"),
)
"""
The simplest use case there is: one dataset, in one role

Historical near-surface air temperature, one model and variant at a time. There
is nothing optional about it, nothing to choose between, and no second dataset
for it to relate to.
"""

USE_CASES: dict[str, Requirement] = {
    "14-pattern-effect": PATTERN_EFFECT,
}
"""
The use cases the tree can express, by their number in the requirements plan
"""


# ----------------------------------------------------------------------- the test


@pytest.mark.parametrize("requirement", USE_CASES.values(), ids=list(USE_CASES))
def test_use_case(requirement, satisfying_catalogue):
    # Stored and reloaded first, because a use case which cannot survive a
    # round-trip cannot be kept in a database, whatever it solves to.
    loaded = Requirement.model_validate_json(requirement.model_dump_json())
    assert loaded == requirement
    assert loaded.requirement_hash() == requirement.requirement_hash()

    result = solve(requirement, satisfying_catalogue(requirement))

    # `explain()` as the message, so a failure here reads as the solver's own
    # account of what it could not do.
    assert len(result.resolved) == 1, result.explain()
    assert not result.unsatisfied, result.explain()
    assert not result.ambiguous, result.explain()

    # Every role is filled: a requirement can be satisfied as a whole and still
    # have left a role out if the roles and the merged result disagree.
    (group,) = result.resolved.values()
    assert set(group.roles) == set(role_paths(requirement.tree))
