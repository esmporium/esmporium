"""
Pieces shared by the requirements tests
"""

from __future__ import annotations

import pytest

from esmporium.datasets import DATASET_FACET_COLUMNS
from esmporium.requirements import CatalogueEntry, InMemoryCatalogue, asked_facets
from esmporium.requirements.tree import effective_query, walk_leaves


@pytest.fixture
def make_entry(get_dataset_kwargs):
    """
    Get a factory for a [CatalogueEntry][esmporium.requirements.CatalogueEntry]

    Builds on `get_dataset_kwargs` (see the root `conftest.py`), so the facet values
    come from the column names and types rather than being written out here.
    Adding a facet to the model therefore does not mean editing these tests.

    The factory takes the entry's `id` and, optionally, `project_facets`,
    `other_terms`, `facets_loaded` and any facet to override. `id` also seeds
    `id_project_specific`, so distinct IDs give distinct datasets unless a test
    deliberately says otherwise.

    `facets_loaded` stands for an entry a lookup built for only some facets, and is
    given exactly as the field holds it: `None`, the default, is an entry which loaded
    everything.
    """

    def factory(
        entry_id,
        *,
        project_facets=None,
        other_terms=None,
        facets_loaded=None,
        label=None,
        **facets,
    ):
        return CatalogueEntry(
            id=entry_id,
            project_facets={} if project_facets is None else project_facets,
            other_terms={} if other_terms is None else other_terms,
            facets_loaded=(None if facets_loaded is None else frozenset(facets_loaded)),
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
    effective query sets. Facets which are columns are set on the entry itself; the
    rest go into the mapping which answers for them, and *which* mapping is decided by
    the home the leaf's query kept the facet in -- a facet we model (CMIP5's
    `product`) into `project_facets`, anything from `other_terms` into `other_terms`.
    Putting one in the wrong mapping would make these tests disagree with
    [esmporium.db.DatabaseCatalogue][], which is the thing they stand in for.

    Facets no leaf names keep the values `get_dataset_kwargs` builds, so every
    entry agrees on `model` and `variant_label` and there is exactly one group.

    A leaf with a lineage gets one further entry, which is its own entry with the
    lineage's `until` facets written over the top, recorded as its parent. So the chain
    is always **one hop** here, deliberately: `role_paths` cannot predict the
    `chain.<i>` roles a longer chain would fill (how many there are depends on the
    data), so a longer chain would put role paths in a solved group which the
    requirement never named, and `test_use_cases.py` compares the two. Longer chains
    are tested where they belong, in `test_solve.py`.

    That parent is recorded as a root, so a requirement which traced its lineage one
    hop further would be `unsatisfied` here rather than running off the end.

    The known limit, and the part which grows as the tree does: a requirement
    which groups on a facet no leaf's query names has nothing here to answer for it,
    so solving it raises
    [UngroupableFacetError][esmporium.requirements.UngroupableFacetError].
    """

    def factory(requirement):
        entries = {}

        def entry_for(modelled, literal):
            """Get, or build, the one entry which answers these facet values"""
            columns = {
                facet: value
                for facet, value in modelled.items()
                if facet in DATASET_FACET_COLUMNS
            }
            project_facets = {
                facet: value
                for facet, value in modelled.items()
                if facet not in DATASET_FACET_COLUMNS
            }

            # Keyed by what was asked for, so two leaves which ask the same thing
            # share one entry rather than colliding on `id`.
            key = tuple(sorted({**modelled, **literal}.items()))
            if key not in entries:
                entries[key] = make_entry(
                    len(entries) + 1,
                    project_facets=project_facets,
                    other_terms=literal,
                    **columns,
                )

            return entries[key]

        parents = {}
        for _, each_leaf in walk_leaves(requirement.tree):
            asked = asked_facets(effective_query(each_leaf, requirement.where))
            modelled = {facet: values[0] for facet, values in asked.modelled.items()}
            literal = {facet: values[0] for facet, values in asked.literal.items()}
            entry = entry_for(modelled, literal)

            if each_leaf.lineage is None:
                continue

            until = asked_facets(each_leaf.lineage.until)
            parent = entry_for(
                {
                    **modelled,
                    **{facet: values[0] for facet, values in until.modelled.items()},
                },
                {
                    **literal,
                    **{facet: values[0] for facet, values in until.literal.items()},
                },
            )
            parents[entry.id] = parent.id
            parents.setdefault(parent.id, None)

        return InMemoryCatalogue(entries=tuple(entries.values()), parents=parents)

    return factory
