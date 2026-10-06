"""
Tests of the package as a whole, rather than of any one module

Only the import boundary lives here, and it is the thing in this package most likely
to break quietly: nothing fails at the point the wrong import is written, it fails
later and somewhere else, as a circular import which stops both packages loading.

Both tests walk every module the package has, discovered rather than listed, so a
module added later is checked from the day it lands.
"""

from __future__ import annotations

import ast
import importlib
import pathlib
import pkgutil
import subprocess
import sys

import pytest

import esmporium.requirements


def esmporium_modules_imported_by(module) -> set[str]:
    """
    Get the esmporium modules a module imports, read off its source

    Read off the source rather than `sys.modules`, so that the answer is about this
    module and nothing else: following imports transitively would report whatever a
    dependency happens to drag in, which is a fact about the dependency.

    The flip side is that this cannot see a transitive edge at all, and a transitive
    edge is exactly how this package's import rule gets broken in practice. That is
    what `test_importing_requirements_imports_nothing_else` is for; the two tests are
    complements, not duplicates.
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


# --------------------------------------------------------------- the import boundary


@pytest.mark.parametrize(
    "module", requirements_modules(), ids=lambda module: module.__name__
)
def test_never_imports_search(module):
    """
    The hard half of the boundary: nothing from `esmporium.search`

    `search` imports `Requirement` objects, which fixes the direction of the
    dependency for good. An import the other way is therefore not a preference but a
    circular import, and both packages then fail to load at all.

    Only `esmporium.search` is refused here. `esmporium.db` is refused too, but by
    the test below, because the reason is different: a db import is not itself a
    cycle, it just reaches `search` anyway.
    """
    offending = {
        name
        for name in esmporium_modules_imported_by(module)
        if name.split(".")[:2] == ["esmporium", "search"]
    }

    assert not offending, (
        f"{sorted(offending)} is a circular import: `search` takes `Requirement` "
        "objects. Put anything shared in `esmporium.query` instead."
    )


def test_imports_only_the_esmporium_it_needs():
    """
    The soft half: exactly which esmporium this package leans on

    Written out by hand, so that widening it is a decision rather than a drift. The
    test to apply to a candidate is not "is it useful here?" but "could importing it
    ever point back this way?" -- `esmporium.formatting` passes because it imports
    nothing but the standard library, so it cannot be half of a cycle.

    There was an `esmporium.db.schema` entry here, for `DATASET_FACET_COLUMNS`. It did
    not grow, it went away: the constant moved to `esmporium.query`, because reading
    it from `db` meant importing `db`, and importing `db` reaches `esmporium.search`.
    See `test_importing_requirements_imports_nothing_else` for what that costs.
    """
    imported = set()
    for module in requirements_modules():
        imported |= esmporium_modules_imported_by(module)

    # Within the package is not a boundary crossing, so it says nothing either way.
    outside = {
        name for name in imported if not name.startswith("esmporium.requirements")
    }

    assert outside == {
        "esmporium.query",
        "esmporium.formatting",
    }


def test_importing_requirements_imports_nothing_else():
    """
    What importing this package actually loads, transitively

    The two tests above read each module's own source, so neither can see a
    *transitive* edge -- and a transitive edge is how this package's import rule gets
    broken in practice. Before `DATASET_FACET_COLUMNS` moved to `esmporium.query`,
    this package imported `esmporium.db.schema`, which looks harmless and is not:

        requirements -> db -> search -> requirements

    because `esmporium.db.__init__` imports `results_to_database`, which imports
    `esmporium.search.result_normalisation`, which initialises the whole of
    `esmporium.search`. Importing this package pulled in twenty search modules, and
    `esmporium.search.search.run` could not then import `Requirement` from a package
    still half-way through its own `__init__`.

    So this test is the one that would have caught it, and it has to run in a
    subprocess: inside pytest the whole of esmporium is imported already, so
    `sys.modules` says nothing.
    """
    script = """
import sys
import esmporium.requirements

for name in sys.modules:
    if name.startswith("esmporium"):
        print(name)
"""
    completed = subprocess.run(  # noqa: S603 - our own interpreter, our own script
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )

    loaded = set(completed.stdout.split())
    outside = {
        name
        for name in loaded
        if not name.startswith("esmporium.requirements") and name != "esmporium"
    }

    assert outside == {
        "esmporium.formatting",
        "esmporium.query",
        "esmporium.query.canonical_query",
        "esmporium.query.known_queries",
        "esmporium.query.protocol",
        "esmporium.query.translate",
    }
