"""
Expressing, compiling and solving the data requirements of an analysis

[`esmporium.search`][] answers "what exists that matches these facets?".
An analysis, for example, is "everything I need to calculate the ECS". This
needs something more sophisticated than a single search query:
"do I have everything I need, and if not, what is missing?", i.e.
all of these and optionally those, or else that one.
It reaches across datasets (the control an experiment branched from,
its sibling experiment, the
cell areas it is weighted by), and it involves judgements rather than matches
(the control has to actually cover the period being analysed).
"""

# A note for developers:
# This package must never import from [`esmporium.search`][]. That is a hard rule,
# and the reason is circular imports: `search` will take `Requirement` objects. Add an
# import the other way and both packages stop importing at all, with
# "cannot import name ... from partially initialized module".
#
# It is not a rule against sharing. When `search` and this package need the same
# thing, it goes in [`esmporium.query`][], which both already depend on, and both
# import it from there. That is how
# [`ClashingFacetsError`][esmporium.query.ClashingFacetsError] is shared: it started
# out defined twice, once here and once in `search`, and moved to `query` rather than
# one side importing the other. Do the same with the next one.
#
# Today the whole esmporium surface this package uses is [`esmporium.query`][] plus
# [`DATASET_FACET_COLUMNS`][esmporium.db.schema.DATASET_FACET_COLUMNS] from
# [`esmporium.db.schema`][].
from esmporium.requirements.catalogue import (
    Catalogue,
    CatalogueEntry,
    DuplicateEntryIDError,
    InMemoryCatalogue,
    UnrecordedFacetError,
    matches,
    set_facets,
)
from esmporium.requirements.tree import (
    AllOf,
    ConflictingFacetsError,
    DuplicateRoleError,
    EmptyLeafQueryError,
    Leaf,
    Node,
    NotANodeError,
    Requirement,
    all_of,
)

__all__ = [
    "AllOf",
    "Catalogue",
    "CatalogueEntry",
    "ConflictingFacetsError",
    "DuplicateEntryIDError",
    "DuplicateRoleError",
    "EmptyLeafQueryError",
    "InMemoryCatalogue",
    "Leaf",
    "Node",
    "NotANodeError",
    "Requirement",
    "UnrecordedFacetError",
    "all_of",
    "matches",
    "set_facets",
]
