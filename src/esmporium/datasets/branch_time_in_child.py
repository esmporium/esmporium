"""
How each project's runs say where they branched, on their own clock

A branch point is one instant written on two clocks: `branch_time_in_parent` on the
parent's time axis and `branch_time_in_child` on the child's. Mapping a child's data
onto its parent's axis needs both. CMIP6 and CMIP7 publish both in the child's file
header. CMIP5 publishes neither as such, only a bare `branch_time` with no units, and
nothing at all on the child's clock.

So where `branch_time_in_child` is not published, **we decide** what stands in for it,
and the decision is recorded here per project rather than left implicit wherever a
branch point is read. This is a registry: the public function takes the mapping as a
defaulted argument, and a caller can add a project, or override one of ours, without
forking anything.

```python
branch_time_in_child_convention(
    "MIP1",
    conventions={
        **BRANCH_TIME_IN_CHILD_BY_PROJECT,
        "MIP1": BranchTimeInChild.PUBLISHED,
    },
)
```

This lives in `esmporium.datasets` because
[Covers][esmporium.requirements.Covers] applies it at solve time and the enricher
applies it when it derives a branch point, and `esmporium.requirements` may import
`esmporium.datasets` and not `esmporium.enrich`.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


class BranchTimeInChild(str, Enum):
    """
    Where a project's `branch_time_in_child` comes from
    """

    PUBLISHED = "published"
    """
    The child's file header publishes it

    A branch point without one has simply not been recorded yet, so it is work to do
    rather than something to stand in for.
    """

    ASSUMED_FIRST_TIME_BOUND = "assumed_first_time_bound"
    """
    Not published, and assumed to be the child's own first time bound

    The CMIP5 convention: a run starts at its branch. It is **an assumption**, and is
    named in every answer which rests on it. It fails exactly when a run's leading files
    are missing, because then its first time bound is later than its branch. A fix which
    states `branch_time_in_child` replaces it.
    """


BRANCH_TIME_IN_CHILD_BY_PROJECT: Mapping[str, BranchTimeInChild] = {
    "CMIP5": BranchTimeInChild.ASSUMED_FIRST_TIME_BOUND,
    "CMIP6": BranchTimeInChild.PUBLISHED,
    "CMIP6Plus": BranchTimeInChild.PUBLISHED,
    "CMIP7": BranchTimeInChild.PUBLISHED,
}
"""
Where `branch_time_in_child` comes from, per project

Keyed by the *child's* project, because `branch_time_in_child` is on the child's clock
and is published, or not, by the child's header.
"""


class UnknownProjectBranchTimeInChildError(NotImplementedError):
    """
    Raised when we cannot say where a project's `branch_time_in_child` comes from
    """

    def __init__(
        self, project: str, conventions: Mapping[str, BranchTimeInChild]
    ) -> None:
        """
        Initialise the error

        Parameters
        ----------
        project
            The project we know nothing about

        conventions
            The per-project conventions which were available
        """
        self.project = project
        self.conventions = conventions
        known = ", ".join(sorted(conventions)) or "none"
        super().__init__(
            f"Do not know whether project {project!r} publishes "
            f"`branch_time_in_child`, or what to assume where it does not. Known "
            f"projects: {known}. Assuming either would be a claim we have no grounds "
            f"for, so pass your own `conventions` mapping instead."
        )


def branch_time_in_child_convention(
    project: str,
    conventions: Mapping[str, BranchTimeInChild] = BRANCH_TIME_IN_CHILD_BY_PROJECT,
) -> BranchTimeInChild:
    """
    Get where a project's `branch_time_in_child` comes from

    Only asked when a branch point has no `in_child`. One which has one uses it,
    whether the child's header published it or a fix set it.

    Parameters
    ----------
    project
        The child's project

    conventions
        Where `branch_time_in_child` comes from, per project

    Returns
    -------
    :
        The convention for `project`

    Raises
    ------
    UnknownProjectBranchTimeInChildError
        We know nothing about that project, so we cannot say

    Examples
    --------
    >>> branch_time_in_child_convention("CMIP6")
    <BranchTimeInChild.PUBLISHED: 'published'>

    >>> branch_time_in_child_convention("CMIP5")
    <BranchTimeInChild.ASSUMED_FIRST_TIME_BOUND: 'assumed_first_time_bound'>
    """
    if project not in conventions:
        raise UnknownProjectBranchTimeInChildError(project, conventions)

    return conventions[project]
