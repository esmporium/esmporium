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
the same dataset can legitimately turn up under more than one project-specific ID.
"""
