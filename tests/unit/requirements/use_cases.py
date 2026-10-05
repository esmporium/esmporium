"""
The use cases from the requirements plan, written as requirements
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
for it to relate to.
"""

USE_CASES: dict[str, Requirement] = {
    "14-pattern-effect": PATTERN_EFFECT,
}
"""
The use cases the tree can express, by their number in the requirements plan
"""
