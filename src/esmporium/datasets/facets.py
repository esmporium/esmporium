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
Two rows agreeing on all of these dataset facets is allowed:
it shows that there may be something missing in our data model
(we think the datasets are the same,
but the project-specific ID tells us that they're not)
or that we have deliberately decided not to support this difference
via the 'standard path'
(e.g. we know we can handle CMIP5's 'product' facet, but it's not via these columns,
it's via project-specific facet handling instead.)
[@Claude can you suggest a good cross-reference for this,
or make a note in FUTURE-DOCS.md]
"""
