"""
Expressing, compiling and solving the data requirements of an analysis

[`esmporium.search`][] answers "what exists that matches these facets?".
An analysis needs something harder: "do I have everything I need, and if not,
what is missing?". That question has a shape a single query cannot express
— all of these and optionally those, or else that one — it reaches across
datasets (the control an experiment branched from, its sibling experiment, the
cell areas it is weighted by), and it involves judgements rather than matches
(the control has to actually cover the period being analysed).

This package is being built from the bottom up, so it starts with the nouns:
what a dataset looks like to selection, and what it means for one to match a
query. Where it is going is set out in the *Expressing analysis data
requirements* design note, under *Further background* in the documentation.

## Import boundary

This package imports from [`esmporium.query`][] and takes
[`DATASET_FACET_COLUMNS`][esmporium.db.schema.DATASET_FACET_COLUMNS] from
[`esmporium.db.schema`][], and nothing else from
[`esmporium.db`][] or [`esmporium.search`][]. There is a test which checks this.

The boundary is deliberate. Requirements describe what an analysis needs; they
do not search, and they do not write to the database. Keeping that true is what
lets a requirement be solved against any catalogue, an in-memory one included,
rather than only against a live database.
"""

from esmporium.requirements.catalogue import (
    Catalogue,
    ClashingFacetError,
    DatasetRecord,
    InMemoryCatalogue,
    UnsupportedFacetError,
    matches,
    set_facets,
)

__all__ = [
    "Catalogue",
    "ClashingFacetError",
    "DatasetRecord",
    "InMemoryCatalogue",
    "UnsupportedFacetError",
    "matches",
    "set_facets",
]
