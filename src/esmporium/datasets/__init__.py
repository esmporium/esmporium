"""
The vocabulary of a dataset, as esmporium stores one

What a dataset *is* -- the facets which describe one -- rather than how one is asked
for ([esmporium.query][]), searched for ([esmporium.search][]), stored
([esmporium.db][]) or reasoned about ([esmporium.requirements][]). All four of those
need to agree on it, so it lives on its own here.

The one rule which makes that work: **this package imports nothing else from
esmporium.** Anything may read it, from anywhere, without closing an import cycle.
Nothing goes in here which could ever point back out again.
"""

from esmporium.datasets.facets import DATASET_FACET_COLUMNS

__all__ = ["DATASET_FACET_COLUMNS"]
