"""
The facets a stored dataset records
"""

DATASET_FACET_COLUMNS: tuple[str, ...] = (
    "project",
    "model",
    "institution",
    "experiment",
    "variant_label",
    "variable",
    "reporting_interval",
    "grid_label",
    "processing_id",
)
"""
The canonical facets that a stored dataset records

In other words, the columns of [Dataset][esmporium.db.schema.Dataset]
which describe the data itself: everything except
[Dataset.id][esmporium.db.schema.Dataset.id] and
[Dataset.id_project_specific][esmporium.db.schema.Dataset.id_project_specific],
which identify a row rather than describe it.
Two rows agreeing on all of these is allowed:
the same dataset can legitimately turn up under more than one project-specific ID.

This is a subset of [CANONICAL_FACETS][esmporium.query.CANONICAL_FACETS].
`activity`, `realm` and `resolution` are deliberately *not* here:
they are canonical facets, so a query may ask for them and the search APIs answer,
but [Dataset][esmporium.db.schema.Dataset] has no column for them,
so a stored row cannot answer for them on its own.
`test_dataset_facet_columns_are_canonical_facets` pins both halves of that
relationship.

It lives in [esmporium.datasets][] rather than beside the table it mirrors because it
is the shared vocabulary of three packages --
[esmporium.db][] writes these columns, [esmporium.requirements][] asks which facets a
stored dataset records, and [esmporium.search][] fills them --
and this package imports nothing else from esmporium, so any of them may read it
without risking an import cycle. See the developer note in
`esmporium/requirements/__init__.py` for what that cycle costs.

The list is written out rather than derived from the table,
both because not every future column will be a facet
and because deriving it would mean importing the table.
Adding a facet to [Dataset][esmporium.db.schema.Dataset] means adding it here too,
which `test_facet_columns_are_the_declared_facets` checks.
"""
