"""
Pieces shared by the requirements tests
"""

from __future__ import annotations

import pytest

from esmporium.requirements import DatasetRecord


@pytest.fixture
def make_record(get_dataset_kwargs):
    """
    Get a factory for a [`DatasetRecord`][esmporium.requirements.DatasetRecord]

    Builds on `get_dataset_kwargs` (see the root `conftest.py`), so the facet values
    come from the column names and types rather than being written out here.
    Adding a facet to the model therefore does not mean editing these tests.

    The factory takes the record's `id` and, optionally, `extra` and any facet to
    override. `id` also seeds `id_project_specific`, so distinct IDs give distinct
    datasets unless a test deliberately says otherwise.
    """

    def factory(record_id, *, extra=None, label=None, **facets):
        return DatasetRecord(
            id=record_id,
            extra={} if extra is None else extra,
            **get_dataset_kwargs(
                f"record-{record_id}" if label is None else label, **facets
            ),
        )

    return factory
