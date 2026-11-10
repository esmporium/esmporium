"""
Relations: how the datasets around a chosen dataset are found

A leaf's query says which datasets could fill its role, and
[Requirement.group_by][esmporium.requirements.Requirement.group_by] splits those into
groups. Everything else an analysis needs hangs off one of the datasets so chosen: the
control it branched from, the sibling experiment it is compared against, the cell areas
it is weighted by. A *relation* is how one of those is found.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from esmporium.query import QueryCanonical
from esmporium.requirements.catalogue import (
    CatalogueEntry,
    accept_any_query_style,
    set_facets,
)
from esmporium.requirements.roles import check_role

RELATION_MODEL_CONFIG = ConfigDict(frozen=True, extra="forbid")
"""
Model config shared by the relations

*Frozen*, for the reason
[NODE_MODEL_CONFIG][esmporium.requirements.tree.NODE_MODEL_CONFIG] is: a relation is
part of a requirement, a requirement is fingerprinted by
[requirement_hash][esmporium.requirements.Requirement.requirement_hash], and anything
which could be edited afterwards would leave that fingerprint describing something
which no longer exists.
"""

LINEAGE_END = "end"
"""
Name a lineage accepts for its last member, whatever that member's role is called
"""


class EmptyUntilError(ValueError):
    """
    Raised when a lineage is told to walk back to a query which sets no facets
    """

    def __init__(self, role: str) -> None:
        """
        Initialise the error

        Parameters
        ----------
        role
            Role the lineage files its last member under
        """
        self.role = role
        super().__init__(
            f"The `until` of the lineage filed under {role!r} sets no facets, "
            "so it matches the first parent found and the walk stops after one hop. "
            "Name the experiment to stop at, e.g. "
            '`until=Query(experiment=("piControl", "esm-piControl"))`.'
        )


class Ancestors(BaseModel):
    """
    Follow parent links back until a dataset matching `until` is reached

    Parent links are read off the published data rather than guessed at, so this is
    only as good as what the archive recorded. A dataset for which nothing has read a
    parent yet is reported as such rather than as a dataset with no parent --
    see [ParentStatus][esmporium.requirements.ParentStatus].
    """

    model_config = RELATION_MODEL_CONFIG

    kind: Literal["ancestors"] = "ancestors"
    """
    Which relation this is
    """

    until: QueryCanonical
    """
    Where to stop, e.g. `Query(experiment=("piControl", "esm-piControl"))`

    Written and stored as [Leaf.query][esmporium.requirements.Leaf.query] is: any query
    style is accepted and translated on the way in.

    Every facet it sets has to match, so in practice it names the experiment and
    nothing else -- that is what "walk back to the control" means. Naming more is
    allowed and sometimes right (a particular variant of `historical`, say), but note
    that a query style written in a project's own names sets `project` for you, which
    then has to match as well.
    """

    role: str
    """
    Role the dataset which matched `until` is filed under

    Yours to choose, subject to the same rules as a leaf's role (see
    [check_role][esmporium.requirements.roles.check_role]), and it may not be the role
    of the leaf it is attached to.
    """

    max_hops: int = Field(default=16, ge=1)
    """
    How many parent links to follow before giving up

    A guard, not the stopping rule -- `until` is. It is insurance against a chain which
    never ends, and the default is set well above any real lineage, so reaching it
    means something is wrong rather than that a chain was simply long.
    """

    _accept_any_style = field_validator("until", mode="before")(accept_any_query_style)

    @field_validator("role")
    @classmethod
    def _valid_role(cls, value: str) -> str:
        return check_role(value)

    @model_validator(mode="after")
    def _until_sets_a_facet(self) -> Ancestors:
        # After the field validators, so `until` is already canonical and `role` is
        # available to name in the error.
        if not set_facets(self.until):
            raise EmptyUntilError(self.role)

        return self


Relation = Annotated[Union[Ancestors], Field(discriminator="kind")]
"""
How a leaf's related datasets -- its control, its sibling -- are found
"""


@dataclass(frozen=True)
class Lineage:
    """
    The datasets a leaf's lineage resolved to, and what they are called
    """

    members: tuple[CatalogueEntry, ...]
    """
    The chain, as `(anchor, *parents_in_between, end)`

    The *anchor* is the dataset which filled the leaf's role, and `end` is the one the
    relation was looking for. At least two long: a relation which resolved to nothing
    is not a lineage, it is an unsatisfied leaf.
    """

    end_role: str
    """
    Role the last member is filed under, i.e. the relation's own `role`
    """

    def index_of(self, role: str) -> int:
        """
        Get the position in this lineage a name refers to

        Parameters
        ----------
        role
            `"self"` for the anchor, `"chain.<i>"` for a parent in between (`chain.0`
            is the anchor's own parent), or [end_role][(m).Lineage.end_role] -- or
            [LINEAGE_END][(m).LINEAGE_END] -- for the last member

        Returns
        -------
        :
            Position in [members][(m).Lineage.members]

        Raises
        ------
        ValueError
            `role` is not a member of this lineage, which includes a `chain.<i>`
            beyond the end of a chain shorter than the check expected

        Examples
        --------
        >>> lineage = Lineage(members=(1, 2, 3, 4), end_role="control")
        >>> [
        ...     lineage.index_of(name)
        ...     for name in ("self", "chain.0", "chain.1", "control", "end")
        ... ]
        [0, 1, 2, 3, 3]

        A chain with nothing in between has no `chain.<i>` at all, and says so rather
        than answering with one of its two ends:

        >>> short = Lineage(members=(1, 2), end_role="control")
        >>> short.index_of("self"), short.index_of("control")
        (0, 1)
        >>> try:
        ...     short.index_of("chain.0")
        ... except ValueError as exc:
        ...     print(str(exc).split(". ")[0])
        'chain.0' is not in this lineage: use 'self', 'control' or 'end'
        """
        if role == "self":
            return 0

        if role in (self.end_role, LINEAGE_END):
            return len(self.members) - 1

        head, _, index = role.partition(".")
        if head == "chain" and index.isdigit():
            # `+ 1` because `chain.0` is the anchor's own parent, and the anchor is
            # member 0. The upper bound excludes the last member, which is `end_role`'s
            # and is not reachable under a `chain` name -- two names for one dataset
            # would make a check's subject depend on how long the chain turned out to
            # be.
            position = int(index) + 1
            if position < len(self.members) - 1:
                return position

        raise ValueError(self._not_a_member(role))

    def _not_a_member(self, role: str) -> str:
        """
        Explain that a name is not one of this lineage's members

        Parameters
        ----------
        role
            The name which was asked for

        Returns
        -------
        :
            The message
        """
        names = f"use 'self', {self.end_role!r} or {LINEAGE_END!r}"
        in_between = len(self.members) - 2
        if in_between < 1:
            return (
                f"{role!r} is not in this lineage: {names}. This lineage has no "
                "parents in between, so there is no 'chain.<i>' to ask for."
            )

        return (
            f"{role!r} is not in this lineage: {names}, or 'chain.<i>' with "
            f"i < {in_between}."
        )
