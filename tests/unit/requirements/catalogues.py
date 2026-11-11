"""
Catalogues the requirements tests need beyond what esmporium ships
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from esmporium.requirements import CatalogueEntry, InMemoryCatalogue


@dataclass(frozen=True)
class InMemoryCatalogueWithTimeInfo(InMemoryCatalogue):
    """
    An in-memory catalogue which also knows time ranges and branch times

    Satisfies [CatalogueWithTimeInfo][esmporium.requirements.CatalogueWithTimeInfo],
    so it can be used to solve requirements which use
    [Covers][esmporium.requirements.Covers].
    """

    time_ranges: Mapping[int, tuple[float, float]] = field(default_factory=dict)
    """Entry ID -> `(start, end)` of its data, in fractional years"""

    branch_times_in_parent: Mapping[int, float] = field(default_factory=dict)
    """Entry ID -> where it branched from its parent, in fractional years"""

    def get_time_range(self, entry: CatalogueEntry) -> tuple[float, float]:
        """Get the span of a dataset's data"""
        return self.time_ranges[entry.id]

    def get_branch_time_in_parent(self, entry: CatalogueEntry) -> float:
        """Get where a dataset branched from its parent"""
        return self.branch_times_in_parent[entry.id]
