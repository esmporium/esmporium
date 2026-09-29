"""
Tests of the package as a whole, rather than of any one module

Only the import boundary lives here, and it is the thing in this package most likely
to break quietly: nothing fails at the point the wrong import is written, it fails
later and somewhere else, as a circular import which stops both packages loading.

Both tests walk every module the package has, discovered rather than listed, so a
module added later is checked from the day it lands.
"""

from __future__ import annotations

import pytest
from tests.unit.requirements.conftest import (
    esmporium_modules_imported_by,
    requirements_modules,
)

# --------------------------------------------------------------- the import boundary


@pytest.mark.parametrize(
    "module", requirements_modules(), ids=lambda module: module.__name__
)
def test_never_imports_search(module):
    """
    The hard half of the boundary: nothing from `esmporium.search`

    `search` will import `Requirement` objects, which fixes the direction of the
    dependency for good. An import the other way is therefore not a preference but a
    circular import, and both packages then fail to load at all.

    Only `esmporium.search` is refused.
    """
    offending = {
        name
        for name in esmporium_modules_imported_by(module)
        if name.split(".")[:2] == ["esmporium", "search"]
    }

    assert not offending, (
        f"{sorted(offending)} would make a circular import once `search` takes "
        "`Requirement` objects. Put anything shared in `esmporium.query` instead."
    )


def test_imports_only_the_esmporium_it_needs():
    """
    The soft half: exactly which esmporium this package leans on today

    Written out by hand, so that widening it is a decision rather than a drift. The
    test to apply to a candidate is not "is it useful here?" but "could importing it
    ever point back this way?" -- `esmporium.formatting` passes because it imports
    nothing but the standard library, so it cannot be half of a cycle.

    The `esmporium.db` entry is the one expected to grow. Widening it to
    `esmporium.search` is what the test above refuses.
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
        "esmporium.db.schema",
        "esmporium.formatting",
    }
