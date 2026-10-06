"""
Expressing, compiling and solving the data requirements of an analysis

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
# It is not a rule against sharing. When `search`, `db` and this package need the
# same thing, it goes in [esmporium.query][], which they all already depend on, and
# they all import it from there. Two worked examples:
# [`ClashingFacetsError`][esmporium.query.ClashingFacetsError], which started out
# defined twice, once here and once in `search`; and
# [`DATASET_FACET_COLUMNS`][esmporium.query.DATASET_FACET_COLUMNS], which describes a
# `db` table but cannot live beside it, because this package needs it and the import
# above is what reading it from `db` would cost. Do the same with the next one.
#
# The whole esmporium surface this package uses is therefore
# [esmporium.query][] and [esmporium.formatting][], and nothing else.
# `esmporium.formatting` is a safe third entry because it imports nothing but the
# standard library, so it cannot be half of a cycle. That is the test to apply to
# anything proposed for this list: not "is it useful here?" but "could importing it
# ever point back this way?".
#
# The database-backed catalogue is on the other side of this line, in
# [esmporium.db.catalogue][]: `db` may import this package freely, because
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
from esmporium.requirements.to_search_plan import (
    LeafSearch,
    SearchPlan,
    to_search_plan,
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
    leaf,
    requirement,
)

__all__ = [
    "AllOf",
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
    "LeafSearch",
    "Node",
    "NotANodeError",
    "Requirement",
    "SearchPlan",
    "Solution",
    "SolveResult",
    "UnrecordedFacetError",
    "UnsolvedGroup",
    "all_of",
    "describe_query",
    "leaf",
    "matches",
    "matches_facets",
    "requirement",
    "set_facets",
    "solve",
    "to_search_plan",
]
