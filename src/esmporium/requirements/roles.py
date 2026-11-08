"""
Role names: the rules every role has to keep to

A role is the named slot a chosen dataset is filed under, and roles nest: a leaf's role
sits under the role of the lineage which found its parents, giving a *role path* like
`control.tas`.

This module exists because two different things name roles -- a leaf
([esmporium.requirements.tree][]) and a relation
([esmporium.requirements.relations][]) -- and the rules have to agree between them. A
role the two disagreed about could be written on one side and refused on the other, or,
worse, accepted on both and then build a path which splits in the wrong place.
"""

from __future__ import annotations

ROLE_SEPARATOR = "."
"""
Separator between the parts of a role path, e.g. the `.` in `abrupt4x.control.tas`

A role may not contain it (see [check_role][(m).check_role]), *because* it is what joins
the parts of a path together. A role which contained one would silently split into two.
"""

RESERVED_ROLES: frozenset[str] = frozenset({"chain", "self"})
"""
Role names the machinery itself uses, which nobody else may take

A lineage addresses its own members by name --
[Lineage.index_of][esmporium.requirements.Lineage.index_of] is where those names are
read -- and these two are its words rather than anyone's choice. `self` is the dataset
the lineage hangs off, and `chain` is the stem of `chain.0`, `chain.1` and so on for the
parents in between.

A lineage's *own* role, the one it files its last member under, is not in here: that is
a name someone chose, so `"control"` is reserved only for the leaf which declared it and
free everywhere else. The leaf-level check for that is on
[Leaf][esmporium.requirements.Leaf].

`end` is deliberately absent too. It is an alias `index_of` accepts for the last member,
so a lineage whose role really is `end` means the same thing by it and there is nothing
to refuse.
"""


def check_role(value: str) -> str:
    """
    Check that a role name can be used, and give it back

    Shaped to be handed straight to pydantic's `field_validator`, which is how both of
    the things which name roles use it.

    Parameters
    ----------
    value
        The role name

    Returns
    -------
    :
        `value`, unchanged

        Capitals, spaces within, and length are all left alone: a role is a label
        someone chose, and this is not the place to have opinions about their naming.

    Raises
    ------
    ValueError
        `value` has no non-whitespace content, contains
        [ROLE_SEPARATOR][(m).ROLE_SEPARATOR], or is one of
        [RESERVED_ROLES][(m).RESERVED_ROLES]

    Examples
    --------
    A name which keeps the rules comes back as it was given, capitals and spaces and
    all. The three which do not are refused for three different reasons: two are the
    machinery's own words, and the third would split into two parts.

    >>> for name in ("the Control Run", "chain", "self", "control.tas"):
    ...     try:
    ...         print(repr(check_role(name)))
    ...     except ValueError:
    ...         print(f"{name!r} refused")
    'the Control Run'
    'chain' refused
    'self' refused
    'control.tas' refused
    """
    # `strip()` because a role of only whitespace is indistinguishable from an empty one
    # in any message which quotes it, and is never what was meant.
    if not value.strip() or ROLE_SEPARATOR in value:
        msg = (
            "Roles must have some non-whitespace content "
            f"and contain no {ROLE_SEPARATOR!r}, got {value!r}"
        )
        raise ValueError(msg)

    if value in RESERVED_ROLES:
        msg = (
            f"Role {value!r} is reserved: {', '.join(sorted(RESERVED_ROLES))} "
            "are how a lineage names its own members. Choose another name."
        )
        raise ValueError(msg)

    return value
