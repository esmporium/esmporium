"""
Pieces shared by the requirements tests
"""

from __future__ import annotations

import pytest

from esmporium.datasets import DATASET_FACET_COLUMNS
from esmporium.requirements import CatalogueEntry, InMemoryCatalogue, set_facets
from esmporium.requirements.tree import effective_query, walk_leaves


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


@pytest.fixture
def satisfying_catalogue(make_entry):
    """
    Get a factory for a catalogue in which every leaf of a requirement is satisfied

    One entry per leaf, built from the first value of every facet the leaf's
    effective query sets. Facets which are columns are set on the entry itself;
    the rest (CMIP5's `product`, anything in `other_terms`) go into its `extra`,
    which is the only way an entry can answer for them.

    Facets no leaf names keep the values `get_dataset_kwargs` builds, so every
    entry agrees on `model` and `variant_label` and there is exactly one group.

    The known limit, and the part which grows as the tree does: a requirement
    which groups or prefers on a facet no leaf's query names has nothing here to
    answer for it, so solving it raises
    [`UnrecordedFacetError`][esmporium.requirements.UnrecordedFacetError].
    """

    def factory(requirement):
        entries = {}
        for _, each_leaf in walk_leaves(requirement.tree):
            asked = set_facets(effective_query(each_leaf, requirement.where))
            facets = {facet: values[0] for facet, values in asked.items()}
            columns = {
                facet: value
                for facet, value in facets.items()
                if facet in DATASET_FACET_COLUMNS
            }
            extra = {
                facet: value
                for facet, value in facets.items()
                if facet not in DATASET_FACET_COLUMNS
            }

            # Keyed by what was asked for, so two leaves which ask the same thing
            # share one entry rather than colliding on `id`.
            key = tuple(sorted(facets.items()))
            if key not in entries:
                entries[key] = make_entry(len(entries) + 1, extra=extra, **columns)

        return InMemoryCatalogue(entries=tuple(entries.values()))

    return factory
