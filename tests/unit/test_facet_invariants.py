"""
Structural invariants of the facet vocabulary and the era profiles.

These tests never translate a real query. They check that the *configuration* in
[`canonical`][esmporium.esgf.canonical] and
[`project_translation_maps`][esmporium.esgf.project_translation_maps] is internally
consistent, so that a mistake in a `field_map` or an `absent_facets` set is caught
here — at the source of the error — rather than surfacing later as a baffling wrong
translation.

They are the facet-translation analogue of `test_facet_columns_are_the_declared_facets`
in `test_schema.py`: the thing that screams when someone adds a project, or a facet,
and forgets to keep the pieces in step.
"""

from __future__ import annotations

import pytest

from esmporium.esgf.canonical import CANONICAL_FACETS, CanonicalQuery
from esmporium.esgf.project_translation_maps import (
    _PROFILE_REGISTRY,
    CMIP5_PROFILE,
    CMIP6_PROFILE,
    CMIP7_PROFILE,
    ProjectProfile,
    known_project_specific_facets,
)

ALL_PROFILES = [CMIP5_PROFILE, CMIP6_PROFILE, CMIP7_PROFILE]

# Parametrise by profile, labelling each case with its project so a failure names it.
by_profile = pytest.mark.parametrize(
    "profile", ALL_PROFILES, ids=[p.project for p in ALL_PROFILES]
)


def test_canonical_facets_match_query_fields():
    """
    The canonical vocabulary and the IR's fields must be exactly the same set.

    `CANONICAL_FACETS` and the fields on `CanonicalQuery` are written out
    separately (pydantic needs literal field names, and the `field_validator`
    lists them a third time). This test is what stops those lists drifting: add a
    canonical facet to one place and forget the other, and this fails.
    """
    facet_fields = set(CanonicalQuery.model_fields) - {"extra_facets", "source_spec"}

    assert facet_fields == set(CANONICAL_FACETS)


@by_profile
def test_field_map_keys_are_canonical(profile: ProjectProfile):
    """
    Every `field_map` key names a canonical facet.

    A `field_map` maps *canonical* names to native ones, so a key that is not a
    canonical facet is a typo that would silently never match anything.
    """
    assert set(profile.field_map) <= set(CANONICAL_FACETS)


@by_profile
def test_absent_facets_are_canonical(profile: ProjectProfile):
    """
    Every `absent_facets` entry names a canonical facet.

    `absent_facets` says "this canonical facet does not exist in this project", so an
    entry that is not a canonical name declares the absence of nothing.
    """
    assert profile.absent_facets <= CANONICAL_FACETS


@by_profile
def test_mapped_and_absent_facets_are_disjoint(profile: ProjectProfile):
    """
    A canonical facet is either renamed, absent, or identity — never two of these.

    `field_map` keys (renamed) and `absent_facets` (missing) must not overlap;
    what remains is the identity set (same name, e.g. `realm`). Together, they
    partition the canonical vocabulary, which is what lets `to_native_params`
    reason about every facet unambiguously.
    """
    assert set(profile.field_map).isdisjoint(profile.absent_facets)


@by_profile
def test_field_map_is_injective(profile: ProjectProfile):
    """
    No two canonical facets map to the same native name.

    If they did, lowering (native -> canonical) would be ambiguous: one native
    name could not decide which canonical facet it came from.
    """
    natives = list(profile.field_map.values())

    assert len(natives) == len(set(natives))


@by_profile
def test_field_map_targets_are_not_canonical_names(profile: ProjectProfile):
    """
    A renamed native name never collides with a canonical facet name.

    `canonical_facet` treats a native name that *is* a canonical name (and is not
    renamed) as an identity facet. If a rename target were also a canonical name,
    that identity shortcut would misfire, so we forbid the collision.
    """
    assert set(profile.field_map.values()).isdisjoint(CANONICAL_FACETS)


@by_profile
def test_project_specific_facets_are_not_canonical(profile: ProjectProfile):
    """
    Project-specific facets are, by definition, outside the canonical vocabulary.

    A project-specific facet that were also canonical would be handled by two code
    paths at once (renamed *and* passed through), which is incoherent.
    """
    assert profile.project_specific_facets.isdisjoint(CANONICAL_FACETS)


@by_profile
def test_project_specific_facets_are_not_rename_targets(profile: ProjectProfile):
    """
    A native name is a rename target or a project-specific facet, not both.
    """
    assert profile.project_specific_facets.isdisjoint(set(profile.field_map.values()))


@by_profile
def test_native_and_canonical_facet_are_inverses(profile: ProjectProfile):
    """
    Rendering out then lowering back in recovers the canonical name.

    For every canonical facet the project actually has (i.e. not absent),
    `canonical_facet(native_facet(c)) == c`. This is the round-trip property the
    whole hub-and-spoke design relies on, checked here at the level of a single
    project's rename helpers.
    """
    for canonical in CANONICAL_FACETS - profile.absent_facets:
        native = profile.native_facet(canonical)

        assert profile.canonical_facet(native) == canonical


@by_profile
def test_registry_key_matches_project(profile: ProjectProfile):
    """
    The registry is keyed by the project a profile actually declares.

    `get_profile(project)` trusts this: a key that disagreed with `project` would
    hand back a profile that renders the wrong `project`.
    """
    assert _PROFILE_REGISTRY[profile.project] is profile


def test_known_project_specific_is_the_union():
    """
    `known_project_specific_facets` is exactly the facets some project owns.

    This is the set the fail-loud rule uses to tell a known-but-wrong-project facet
    (raise) from an unmodelled `other_terms` facet (pass through), so it must be
    the union of every profile's `project_specific_facets` and nothing more.
    """
    expected: set[str] = set()
    for profile in ALL_PROFILES:
        expected |= profile.project_specific_facets

    assert known_project_specific_facets() == expected
