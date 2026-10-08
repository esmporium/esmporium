"""
Expressing and solving the data requirements of an analysis

[esmporium.search][] answers "what exists that matches these facets?".
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
# This package must never import from [esmporium.search][]. That is a hard rule,
# and the reason is circular imports: `search` takes `Requirement` objects. Add an
# import the other way and both packages stop importing at all, with
# "cannot import name ... from partially initialized module".
#
# Nor may it import [esmporium.db][], for the same reason one step removed.
# `esmporium.db.__init__` imports `results_to_database`, which imports
# [esmporium.search.result_normalisation][], which initialises the whole of
# `esmporium.search`. So a db import from here reaches `search` anyway:
#
#     requirements -> db -> search -> requirements
#                                     ^ still half-built, so this raises
#
# That chain is why the rule bites at *import* time rather than only in principle.
# `tests/unit/requirements/test_package.py` checks it two ways, because the obvious
# check is not enough: one test walks each module's AST (and so cannot see a
# transitive edge like the one above), and the other imports this package in a
# subprocess and looks at what landed in `sys.modules` (and so can).
#
# It is not a rule against sharing, and there are two places to share from.
# Something about *queries* which `search`, `db` and this package all need goes in
# [esmporium.query][], which they all already depend on:
# [ClashingFacetsError][esmporium.query.ClashingFacetsError] is the worked example,
# having started out defined twice, once here and once in `search`.
# Something about *datasets* goes in [esmporium.datasets][], which imports nothing
# else from esmporium and so can never point back this way:
# [DATASET_FACET_COLUMNS][esmporium.datasets.DATASET_FACET_COLUMNS] is the worked
# example there, describing a `db` table it cannot live beside, because this package
# needs it and the import above is what reading it from `db` would cost.
# Do the same with the next one.
#
# The whole esmporium surface this package uses is therefore
# [esmporium.query][], [esmporium.datasets][] and [esmporium.formatting][], and
# nothing else. The last two are safe because they import nothing but the standard
# library, so neither can be half of a cycle. That is the test to apply to anything
# proposed for this list: not "is it useful here?" but "could importing it ever
# point back this way?".
#
# The database-backed catalogue is on the other side of this line, in
# [esmporium.db.database_catalogue][]: `db` may import this package freely, because
# db -> requirements -> query has no cycle in it.
from esmporium.requirements.catalogue import (
    Catalogue,
    CatalogueEntry,
    DuplicateEntryIDError,
    InMemoryCatalogue,
    UnrecordedFacetError,
    matches,
    matches_facets,
    set_facets,
)
from esmporium.requirements.solve import (
    Explanation,
    ExplanationStatus,
    ExplanationStatusNotOk,
    ExplanationStatusOk,
    Solution,
    SolveResult,
    UnsolvedGroup,
    describe_query,
    solve,
)
from esmporium.requirements.tree import (
    AllOf,
    Cardinality,
    ConflictingFacetsError,
    DuplicateRoleError,
    EmptyLeafQueryError,
    Leaf,
    Node,
    NotANodeError,
    Requirement,
    all_of,
    effective_query,
    leaf,
    requirement,
    walk_leaves,
)

__all__ = [
    "AllOf",
    "Cardinality",
    "Catalogue",
    "CatalogueEntry",
    "ConflictingFacetsError",
    "DuplicateEntryIDError",
    "DuplicateRoleError",
    "EmptyLeafQueryError",
    "Explanation",
    "ExplanationStatus",
    "ExplanationStatusNotOk",
    "ExplanationStatusOk",
    "InMemoryCatalogue",
    "Leaf",
    "Node",
    "NotANodeError",
    "Requirement",
    "Solution",
    "SolveResult",
    "UnrecordedFacetError",
    "UnsolvedGroup",
    "all_of",
    "describe_query",
    "effective_query",
    "leaf",
    "matches",
    "matches_facets",
    "requirement",
    "set_facets",
    "solve",
    "walk_leaves",
]
