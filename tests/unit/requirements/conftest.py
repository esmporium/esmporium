"""
Pieces shared by the requirements tests
"""

from __future__ import annotations

import pytest

from esmporium.requirements import CatalogueEntry


@pytest.fixture
def make_entry(get_dataset_kwargs):
    """
    Get a factory for a [`CatalogueEntry`][esmporium.requirements.CatalogueEntry]

    Builds on `get_dataset_kwargs` (see the root `conftest.py`), so the facet values
    come from the column names and types rather than being written out here.
    Adding a facet to the model therefore does not mean editing these tests.

    The factory takes the entry's `id` and, optionally, `extra` and any facet to
    override. `id` also seeds `id_project_specific`, so distinct IDs give distinct
    datasets unless a test deliberately says otherwise.
    """

    def factory(entry_id, *, extra=None, label=None, **facets):
        return CatalogueEntry(
            id=entry_id,
            extra={} if extra is None else extra,
            **get_dataset_kwargs(
                f"entry-{entry_id}" if label is None else label, **facets
            ),
        )

    return factory
