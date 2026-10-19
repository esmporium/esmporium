"""
Tests of storing requirements and of the naming policy

The naming policy itself (`resolve_name_clash`) is tested without a database at all:
it is a function of three values, and keeping it that way is what makes the behaviour
readable.
"""

from __future__ import annotations

import hashlib
import re

import pytest
from sqlmodel import Session

from esmporium.db import (
    OnNameClash,
    RequirementNameClashError,
    RequirementNameInUseError,
    RequirementNotFoundError,
    find_requirement,
    get_stored_requirements,
    load_requirement,
    resolve_name_clash,
    store_requirement,
    to_requirement,
)
from esmporium.query import Query, QueryCMIP6
from esmporium.requirements import Cardinality, all_of, leaf, requirement


def a_requirement(name="gregory-method", variable="tas", **overrides):
    """A requirement which differs from its siblings only where a test says so"""
    return requirement(
        name=name,
        tree=all_of(leaf(Query(variable=variable, project="CMIP6"), "field")),
        group_by=("model",),
        **overrides,
    )


# The policy, with no database in sight.


def test_a_name_we_have_not_seen_is_simply_used():
    assert (
        resolve_name_clash(stored_name=None, incoming_name="gregory-method")
        == "gregory-method"
    )


def test_the_same_name_is_not_a_clash():
    assert (
        resolve_name_clash(stored_name="gregory-method", incoming_name="gregory-method")
        == "gregory-method"
    )


def test_a_clash_raises_by_default():
    # The default matters more than the mechanism: nobody should lose the name they
    # have been finding their search by because they typed a different one today.
    with pytest.raises(RequirementNameClashError) as excinfo:
        resolve_name_clash(stored_name="gregory-method", incoming_name="ecs-method")

    assert str(excinfo.value) == (
        "This requirement is already stored as 'gregory-method', so storing it as "
        "'ecs-method' would give one search two names. Use the name "
        "'gregory-method', or pass on_name_clash=OnNameClash.OVERWRITE_NAME to "
        "rename it to 'ecs-method', or OnNameClash.KEEP_OLD_NAME to carry on under "
        "the stored name."
    )
    # Carried as well as said, so a caller can branch on them rather than read prose.
    assert excinfo.value.stored_name == "gregory-method"
    assert excinfo.value.incoming_name == "ecs-method"


@pytest.mark.parametrize(
    ("on_clash", "expected"),
    [
        pytest.param(OnNameClash.OVERWRITE_NAME, "ecs-method", id="overwrite"),
        pytest.param(OnNameClash.KEEP_OLD_NAME, "gregory-method", id="keep_old"),
    ],
)
def test_a_clash_resolves_as_asked(on_clash, expected):
    resolved = resolve_name_clash(
        stored_name="gregory-method",
        incoming_name="ecs-method",
        on_clash=on_clash,
    )

    assert resolved == expected


def test_the_options_are_plain_strings():
    # Like `Cardinality` and `VersionFilter`, so a caller can pass "raise".
    assert (
        resolve_name_clash(stored_name="a", incoming_name="b", on_clash="keep_old_name")
        == "a"
    )


# Storing and loading.


def test_a_requirement_round_trips(engine):
    stored_requirement = a_requirement()

    with Session(engine) as session:
        store_requirement(session, stored_requirement)
        session.commit()

    with Session(engine) as session:
        assert load_requirement(session, "gregory-method") == stored_requirement


def test_a_requirement_can_be_loaded_by_id(engine):
    # The id is the other thing a person has to hand, and the two cannot be confused.
    with Session(engine) as session:
        stored = store_requirement(session, a_requirement())
        session.commit()
        requirement_id = stored.id

    with Session(engine) as session:
        assert load_requirement(session, requirement_id) == a_requirement()


def test_everything_about_a_requirement_survives_the_round_trip(engine):
    # Not just the tree: the settings which decide what solving does are part of the
    # question, so a reloaded requirement has to be able to answer for them.
    full = requirement(
        name="full",
        tree=all_of(leaf(QueryCMIP6(variable_id="tas"), "field")),
        group_by=("model", "variant_label"),
        group_by_other_terms=("cordex-cmip6:driving_source_id",),
        prefer={"grid_label": ("gn", "gr")},
        prefer_other_terms={"product": ("output1",)},
        cardinality=Cardinality.ALL,
        where=Query(project="CMIP6"),
    )

    with Session(engine) as session:
        store_requirement(session, full)
        session.commit()

    with Session(engine) as session:
        loaded = load_requirement(session, "full")

    assert loaded == full
    assert loaded.requirement_hash() == full.requirement_hash()


def test_the_stored_json_is_what_the_hash_commits_to(engine):
    # Keeping the two beside each other is only worth anything if they agree.
    with Session(engine) as session:
        stored = store_requirement(session, a_requirement())
        session.commit()

        expected = hashlib.sha256(stored.identity_json.encode()).hexdigest()

        assert stored.requirement_hash == expected


def test_storing_the_same_requirement_twice_reuses_the_row(engine):
    with Session(engine) as session:
        first = store_requirement(session, a_requirement())
        session.commit()
        first_id = first.id

    with Session(engine) as session:
        second = store_requirement(session, a_requirement())
        session.commit()

        assert second.id == first_id

    assert len(get_stored_requirements(engine)) == 1


def test_a_renamed_requirement_is_recognised_as_the_one_we_have(engine):
    # The whole reason the fingerprint leaves the name out: this is one question, and
    # it has to be found as such whichever label arrives with it.
    with Session(engine) as session:
        store_requirement(session, a_requirement(name="gregory-method"))
        session.commit()

    with Session(engine) as session:
        with pytest.raises(
            RequirementNameClashError,
            match=re.escape("already stored as 'gregory-method'"),
        ):
            store_requirement(session, a_requirement(name="ecs-method"))


def test_a_rename_keeps_the_requirement_and_its_history(engine):
    with Session(engine) as session:
        stored = store_requirement(session, a_requirement(name="gregory-method"))
        session.commit()
        original_id = stored.id

    with Session(engine) as session:
        renamed = store_requirement(
            session,
            a_requirement(name="ecs-method"),
            on_name_clash=OnNameClash.OVERWRITE_NAME,
        )
        session.commit()

        # The same row, so every run already recorded against it still belongs to it.
        assert renamed.id == original_id
        assert renamed.name == "ecs-method"

    with Session(engine) as session:
        # And the requirement now comes back under the new name, carrying it.
        assert load_requirement(session, "ecs-method").name == "ecs-method"
        assert find_requirement(session, "gregory-method") is None


def test_keeping_the_old_name_leaves_the_row_alone(engine):
    with Session(engine) as session:
        store_requirement(session, a_requirement(name="gregory-method"))
        session.commit()

    with Session(engine) as session:
        kept = store_requirement(
            session,
            a_requirement(name="ecs-method"),
            on_name_clash=OnNameClash.KEEP_OLD_NAME,
        )
        session.commit()

        assert kept.name == "gregory-method"
        # The caller gets back the requirement as it is tracked, not as they wrote it.
        assert to_requirement(kept).name == "gregory-method"


def test_reusing_a_name_for_a_different_requirement_is_refused(engine):
    # The direction the naming options deliberately do not cover. One name has to mean
    # one search, or re-running by name is ambiguous and comparing runs over time
    # silently compares answers to different questions.
    with Session(engine) as session:
        store_requirement(session, a_requirement(variable="tas"))
        session.commit()

    stored_hash = a_requirement(variable="tas").requirement_hash()
    incoming_hash = a_requirement(variable="pr").requirement_hash()

    with Session(engine) as session:
        with pytest.raises(RequirementNameInUseError) as excinfo:
            store_requirement(session, a_requirement(variable="pr"))

    assert str(excinfo.value) == (
        f"The name 'gregory-method' is already used by a different requirement "
        f"({stored_hash[:12]}..., where this one is {incoming_hash[:12]}...). "
        "One name means one search, so that a search can be re-run by name and its "
        "runs compared with each other. Give this requirement a name of its own, or "
        "rename the stored one first if 'gregory-method' is meant to be this search "
        "now."
    )
    assert excinfo.value.name == "gregory-method"
    assert excinfo.value.stored_hash == stored_hash
    assert excinfo.value.incoming_hash == incoming_hash


@pytest.mark.parametrize(
    "on_clash",
    [OnNameClash.RAISE, OnNameClash.OVERWRITE_NAME, OnNameClash.KEEP_OLD_NAME],
)
def test_reusing_a_name_is_refused_whatever_the_option(engine, on_clash):
    # Not configurable, on purpose: none of the three options means anything here.
    with Session(engine) as session:
        store_requirement(session, a_requirement(variable="tas"))
        session.commit()

    with Session(engine) as session:
        with pytest.raises(
            RequirementNameInUseError,
            match=re.escape("is already used by a different requirement"),
        ):
            store_requirement(
                session, a_requirement(variable="pr"), on_name_clash=on_clash
            )


def test_two_different_requirements_can_both_be_tracked(engine):
    with Session(engine) as session:
        store_requirement(session, a_requirement(name="tas-watch", variable="tas"))
        store_requirement(session, a_requirement(name="pr-watch", variable="pr"))
        session.commit()

    assert [stored.name for stored in get_stored_requirements(engine)] == [
        "tas-watch",
        "pr-watch",
    ]


@pytest.mark.parametrize(
    ("name_or_id", "described_as"),
    [
        pytest.param("nothing", "name 'nothing'", id="name"),
        pytest.param(404, "id 404", id="id"),
    ],
)
def test_loading_something_we_have_not_got_says_so(engine, name_or_id, described_as):
    # The message calls what you passed either a name or an id, worked out from its
    # type. That choice is asserted, because a lookup by name reported as an id sends
    # someone looking in the wrong place, and inverting the test would be invisible.
    with Session(engine) as session:
        with pytest.raises(RequirementNotFoundError) as excinfo:
            load_requirement(session, name_or_id)

    assert str(excinfo.value) == (
        f"No requirement is stored with {described_as}. "
        "Store one by searching for it with esmporium.db.search_and_track."
    )
    assert excinfo.value.name_or_id == name_or_id
