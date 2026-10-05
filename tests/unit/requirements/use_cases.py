"""
The use cases from the requirements plan, written as requirements

One module per repository rather than one per test, because these are the
questions the whole package exists to answer: they are written once here and
every PR which widens the tree solves them again.

[USE_CASES][(m).USE_CASES] only ever grows. A use case lands here when the tree
can express it, which is why it holds one of the twenty today: everything else
needs a node type or a relation which does not exist yet (lineage, auxiliary
data, optional parts, alternatives, scopes). Each arrives with the PR which
brings what it needs.
"""

from __future__ import annotations

from esmporium.query import Query
from esmporium.requirements import Requirement, leaf, requirement

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
for it to relate to, which is what makes it the one the tree could express first.
"""

USE_CASES: dict[str, Requirement] = {
    "pattern-effect": PATTERN_EFFECT,
}
"""
The use cases the tree can express, by their number in the requirements plan

The numbers are the plan's, not a sequence, so gaps are expected: they say which
use case this is, and are what makes a test failure traceable to the thing it was
asked to do.
"""
