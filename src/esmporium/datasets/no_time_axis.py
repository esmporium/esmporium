"""
Which of a project's datasets have no time axis at all

`fx` data -- cell areas, land fractions, orography -- is published once for a
simulation rather than over time, so coverage is not a question you can ask of it.
Which reporting intervals mean that is a project's own vocabulary, so this is a
registry: the public function takes the mapping as a defaulted argument, and a caller
can add a project, or override one of ours, without forking anything.

```python
has_no_time_axis(
    "MIP1", "fx", intervals={**NO_TIME_AXIS_INTERVALS_BY_PROJECT, "MIP1": frozenset()}
)
```

This lives in `esmporium.datasets` rather than beside the rest of enrichment because
[Covers][esmporium.requirements.Covers] refuses `fx` data both when a requirement is
built and when it is solved, and `esmporium.requirements` may import
`esmporium.datasets` and not `esmporium.enrich`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

NO_TIME_AXIS_INTERVALS_BY_PROJECT: Mapping[str, frozenset[str]] = {
    "CMIP5": frozenset({"fx"}),
    "CMIP6": frozenset({"fx"}),
    "CMIP6Plus": frozenset({"fx"}),
    "CMIP7": frozenset({"fx"}),
}
"""
Which reporting intervals mean "this dataset has no time axis at all", per project

What [Covers][esmporium.requirements.Covers] refuses against: coverage is not a
question you can ask of auxiliary variables, such as cell area.
"""


class UnknownProjectTimeAxisError(NotImplementedError):
    """
    Raised when we cannot say whether a project's dataset has a time axis
    """

    def __init__(self, project: str, intervals: Mapping[str, frozenset[str]]) -> None:
        """
        Initialise the error

        Parameters
        ----------
        project
            The project we know nothing about

        intervals
            The per-project vocabularies which were available
        """
        self.project = project
        self.intervals = intervals
        known = ", ".join(sorted(intervals)) or "none"
        super().__init__(
            f"Do not know which of project {project!r}'s reporting intervals mean a "
            f"dataset has no time axis. Known projects: {known}. Answering 'it has "
            f"one' would be a claim we have no grounds for, so pass your own "
            f"`intervals` mapping instead."
        )


def has_no_time_axis(
    project: str,
    reporting_interval: str,
    intervals: Mapping[str, frozenset[str]] = NO_TIME_AXIS_INTERVALS_BY_PROJECT,
) -> bool:
    """
    Determine whether a dataset has no time axis at all

    Parameters
    ----------
    project
        The project the dataset belongs to

    reporting_interval
        The dataset's reporting interval

    intervals
        Which intervals mean "no time axis", per project

    Returns
    -------
    :
        `True` if this dataset cannot have a time range, e.g. an `fx` dataset

    Raises
    ------
    UnknownProjectTimeAxisError
        We know nothing about that project, so we cannot say

    Notes
    -----
    Raising for an unknown project rather than answering `False` is deliberate: `False`
    would read as "this dataset does have a time axis", which we have no grounds to
    say, and the cost of being wrong is a header fetched for a file that has no time
    in it at all.

    Examples
    --------
    >>> has_no_time_axis("CMIP6", "fx"), has_no_time_axis("CMIP6", "mon")
    (True, False)
    """
    if project not in intervals:
        raise UnknownProjectTimeAxisError(project, intervals)

    return reporting_interval in intervals[project]


def projects_with_a_known_time_axis_vocabulary(
    intervals: Mapping[str, frozenset[str]] = NO_TIME_AXIS_INTERVALS_BY_PROJECT,
) -> frozenset[str]:
    """
    Get the projects we can answer [has_no_time_axis][(m).has_no_time_axis] for

    What a check which has no project in hand has to fall back on: a query which names
    a reporting interval and no project selects data from every project, so refusing it
    means the interval has no time axis in all of them.

    Parameters
    ----------
    intervals
        Which intervals mean "no time axis", per project

    Returns
    -------
    :
        The projects named in `intervals`

    Examples
    --------
    >>> "CMIP6" in projects_with_a_known_time_axis_vocabulary()
    True
    """
    return frozenset(intervals)
