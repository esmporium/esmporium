"""
Tests that every use case builds, stores and can be satisfied

The point of this file is that it is parametrised over
[`USE_CASES`][tests.unit.requirements.use_cases.USE_CASES] rather than naming
any one of them: a use case added later is checked by these same four
assertions, without anyone writing a test for it.

What it does not check is anything about a *particular* use case. The solver's
own behaviour -- what it does when a dataset is missing, or when two fit one
role -- is in `test_solve.py`.
"""

from __future__ import annotations

import pytest
from tests.unit.requirements.use_cases import USE_CASES

from esmporium.requirements import Requirement, solve
from esmporium.requirements.tree import role_paths


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
