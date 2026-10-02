"""
Pieces shared by the requirements tests
"""

from __future__ import annotations

import ast
import importlib
import pathlib
import pkgutil

import pytest

import esmporium.requirements
from esmporium.requirements import CatalogueEntry


def esmporium_modules_imported_by(module) -> set[str]:
    """
    Get the esmporium modules a module imports, read off its source

    Read off the source rather than `sys.modules`, because `esmporium.db.schema`
    itself imports `esmporium.search.health`: anything which followed imports
    transitively would report `esmporium.search` for a reason that has nothing to do
    with this package.
    """
    tree = ast.parse(pathlib.Path(module.__file__).read_text())

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    return {name for name in imported if name.split(".")[0] == "esmporium"}


def requirements_modules() -> tuple[object, ...]:
    """
    Get every module of `esmporium.requirements`, the package itself included

    Discovered rather than listed, so that a module added later is checked without
    anyone having to remember to add it here.
    """
    package = esmporium.requirements
    found = [package]
    for info in pkgutil.iter_modules(package.__path__):
        found.append(importlib.import_module(f"{package.__name__}.{info.name}"))

    return tuple(found)


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
