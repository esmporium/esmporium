"""
Solving a requirement: filling roles with datasets, group by group

The requirements tree says what is needed,
the catalogue says what exists,
and the solver works out whether the one can be satisfied from the other.

We attempt to solve the same tree multiple times, one attempt per group.
A group is a set of datasets from which we try to meet the requirement.
The grouping is set by
[Requirement.group_by][esmporium.requirements.Requirement.group_by].
Each attempt either leads to a solution ([Solution][(m).Solution])
or not ([UnsolvedGroup][(m).UnsolvedGroup]).

Within an attempt, each node of the tree is also given a status
(see [ExplanationStatus][(m).ExplanationStatus]).
The group's outcome is the outcome of the root of the tree,
with whether that status is [ExplanationStatusOk][(m).ExplanationStatusOk]
or [ExplanationStatusNotOk][(m).ExplanationStatusNotOk]
defining whether we found a solution or not.
"""

# A note for whoever adds the next node type: [_eval][(m)._eval] is the fourth of the
# dispatch functions named in the note at the top of
# [esmporium.requirements.tree][]. Like the other three it ends in a `TypeError`
# rather than a silent fallthrough, so a forgotten branch fails loudly.

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum

from esmporium.datasets import DATASET_FACET_COLUMNS
from esmporium.formatting import readable_list
from esmporium.query import QueryProtocol, project_specific_facet_names
from esmporium.requirements.catalogue import (
    Catalogue,
    CatalogueEntry,
    VersionFilter,
    asked_facets,
    set_facets,
)
from esmporium.requirements.tree import (
    AllOf,
    Cardinality,
    Leaf,
    Node,
    NotANodeError,
    Requirement,
    effective_query,
    walk_leaves,
)

GroupKey = tuple[tuple[str, str | None], ...]
"""
`(facet, value)` pairs identifying a group

A value can be `None` because a facet can be: CMIP5 has no concept of a grid, so
[CatalogueEntry.grid_label][esmporium.requirements.CatalogueEntry.grid_label] is
`None` for every CMIP5 dataset.
"""


# TODO future:
# Two more join this later: `"degraded"` when a check passes with a complaint, and
# `"absent"` when an optional part is dropped. Nothing here can produce either yet,
# so they are deliberately absent rather than defined and unreachable.
class ExplanationStatusOk(str, Enum):
    """
    Ways in which (part of) a requirement can be met
    """

    SATISFIED = "satisfied"
    """Everything asked for was found"""


# TODO future:
# A third, `"undetermined"`, arrives with constraints: a check which needs metadata the
# catalogue cannot supply can neither pass nor fail, and answering "no" on its behalf
# would be a lie. Nothing here can produce that yet, because no check exists to need it,
# so it is deliberately absent rather than defined and unreachable.
class ExplanationStatusNotOk(str, Enum):
    """
    Ways in which (part of) a requirement can fail to be met

    An enum of its own,
    so that a field which can only have failed says so in its type.
    """

    UNSATISFIED = "unsatisfied"
    """There is no dataset"""

    AMBIGUOUS = "ambiguous"
    """There are too many datasets and nothing to choose between them"""


ExplanationStatus = ExplanationStatusOk | ExplanationStatusNotOk
"""
Statuses an [Explanation][(m).Explanation] can carry

A union so that every status has to decide whether it is ok or not
and there is no ambiguity about which is which.
"""

_STATUS_PRIORITY: dict[ExplanationStatusNotOk, int] = {
    ExplanationStatusNotOk.UNSATISFIED: 0,
    ExplanationStatusNotOk.AMBIGUOUS: 1,
}
"""
Which status a group takes when its parts fail in different ways, lowest first

`"unsatisfied"` wins over `"ambiguous"`: a group missing a dataset cannot be satisfied
however the ambiguity is settled, so the missing dataset is the honest headline. The
explanation still carries both, because nothing short-circuits -- see
[_eval_all_of][(m)._eval_all_of].
"""


# A note for developers: these two raise rather than being reported as an unsolved
# group, and the dividing line is worth stating because it is the same line
# `solve` draws everywhere. An unsatisfied or ambiguous group is a fact about the
# *data*: more searching could change it, so it is a result. These are facts about the
# *requirement*: no amount of new data makes a grouping work when nothing records the
# facet it groups on, and reporting it group by group would make one mistake look like
# missing data everywhere.
#
# They are also the second half of a pair. A name we do not model is caught when the
# requirement is built, by
# [UnmodelledFacetError][esmporium.requirements.UnmodelledFacetError], because that
# needs no data. These are the half which does: a name we *do* model, which nothing in
# this catalogue happens to record.
class UngroupableFacetError(ValueError):
    """
    Raised when nothing in the catalogue records a facet the requirement groups on

    Only for [Requirement.group_by][esmporium.requirements.Requirement.group_by].
    `group_by_other_terms` is the escape hatch and is never checked, so a name there
    which nothing records simply puts every dataset in one group.
    """

    def __init__(self, facets: Iterable[str], candidates: int) -> None:
        """
        Initialise the error

        Parameters
        ----------
        facets
            The facets no candidate can answer for

        candidates
            How many datasets were looked at, which is what makes the message
            actionable: it says the search found data and this facet was missing from
            it, rather than that nothing was found at all
        """
        self.facets = tuple(sorted(facets))
        self.candidates = candidates
        them = "it" if len(self.facets) == 1 else "them"
        super().__init__(
            f"Cannot group on {readable_list(self.facets)}: "
            f"none of the {candidates} datasets found records {them}. "
            "A group needs a value for every facet it is keyed on, so no group can "
            "be formed and the requirement cannot be solved as written. "
            "These are facets we can search but store only in the raw search "
            f"documents, so re-searching may populate {them}; "
            "otherwise group on a facet the datasets do record. "
            "Note that a facet whose value is a list of several (a multi-realm "
            "dataset, say) counts as unrecorded, because a group key needs one value. "
            "For a facet only a search document knows, use `group_by_other_terms`, "
            "which is never checked."
        )


class UnrankableFacetError(ValueError):
    """
    Raised when nothing in a tie records a facet the requirement prefers on

    Only for [Requirement.prefer][esmporium.requirements.Requirement.prefer].
    `prefer_other_terms` is the escape hatch: a name there which nothing records
    ranks every candidate equally, so it narrows nothing and the tie is reported as
    [ExplanationStatusNotOk.AMBIGUOUS][(m).ExplanationStatusNotOk] instead.
    """

    def __init__(self, facets: Iterable[str], subject: str, candidates: int) -> None:
        """
        Initialise the error

        Parameters
        ----------
        facets
            The facets no remaining candidate can answer for

        subject
            What was being filled, e.g. a role path

        candidates
            How many candidates were tied
        """
        self.facets = tuple(sorted(facets))
        self.subject = subject
        self.candidates = candidates
        super().__init__(
            f"Cannot rank the {candidates} candidates for {subject} "
            f"on {readable_list(self.facets)}: none of them records "
            f"{'it' if len(self.facets) == 1 else 'them'}, so the preference cannot "
            "break the tie. "
            "We can search these facets but store them only in the raw search "
            "documents, so re-searching may populate them; otherwise prefer on a "
            "facet the datasets do record, narrow the query, or use "
            "`Cardinality.ALL` if every candidate is wanted. "
            "For a facet only a search document knows, use `prefer_other_terms`, "
            "which is never checked and ranks unanswerable candidates equally "
            "rather than raising."
        )


# A note for developers: a `NotImplementedError` rather than a `ValueError`, because
# the requirement is a fair thing to want and the answer is "not yet" rather than "no".
# Grouping CMIP5 by `product` is right, and grouping CMIP5 *and* CMIP6 by it has an
# answer we have not decided on: every CMIP6 dataset would land in one group keyed
# `product=None`, which is either exactly what you meant or a silent accident, and we
# are not guessing which.
#
# Only project-specific facets, and only when *some* candidates answer. The three cases
# at the one site are worth seeing together:
#
#     none of the candidates records it  -> UngroupableFacetError
#     some do, some do not               -> this
#     all of them do                     -> proceed
#
# The last case is the one to be careful not to break: two projects can share a query
# class, and then they declare the same project-specific facets. A caller who maps
# CMIP6Plus to `QueryCMIP6` (which is what searching it takes -- see
# `tests/integration/unmodelled_project/test_cmip6plus_registration.py`) has a
# catalogue where every candidate answers for `sub_experiment_id`, whichever of the
# two projects it came from, so it can be grouped on throughout. "Spans more than one
# project" would refuse that, which is why the test is what the candidates can answer
# rather than what projects they come from.
#
# Canonical facets with no column (`realm`, `activity`, `resolution`) are deliberately
# left out: partial coverage there is ordinary -- two mirrors of one project return
# different fields, and a multi-valued facet is recorded as nothing at all -- and the
# one-`None`-group answer is the honest one. It is only a facet which belongs to some
# projects and not others where the `None` group means "wrong project" rather than
# "not recorded".
class MixedProjectFacetError(NotImplementedError):
    """
    Raised when a project-specific facet is used across datasets of several projects

    Naming `project` in
    [group_by][esmporium.requirements.Requirement.group_by] is the way through: each
    group then holds one project, so the question does not arise.
    """

    def __init__(
        self, facets: Iterable[str], field_name: str, knowing: int, total: int
    ) -> None:
        """
        Initialise the error

        Parameters
        ----------
        facets
            The project-specific facets only some candidates can answer for

        field_name
            Field they were set on, e.g. `"group_by"`

        knowing
            How many candidates can answer for them

        total
            How many candidates there were
        """
        self.facets = tuple(sorted(facets))
        self.field_name = field_name
        self.knowing = knowing
        self.total = total
        super().__init__(
            f"{field_name} names {readable_list(self.facets)}, which "
            f"{'is' if len(self.facets) == 1 else 'are'} specific to some projects, "
            f"but only {knowing} of the {total} datasets found "
            f"{'records' if knowing == 1 else 'record'} "
            f"{'it' if len(self.facets) == 1 else 'them'} -- so the datasets span "
            "more than one project and the rest have no value for it. "
            "We have not decided what that should mean, so this stops rather than "
            "quietly putting every dataset of the other projects into one group. "
            "Either restrict the requirement to one project "
            "(`where=Query(project=...)`), or add `project` to `group_by` so that "
            "each group holds a single project and the facet is answerable within it."
        )


@dataclass(frozen=True)
class Explanation:
    """
    Why (part of) a requirement was or was not met

    Output rather than diagnostics: [SolveResult.explain][(m).SolveResult.explain]
    renders these for a person to read. Most groups in a real archive are not satisfied,
    and `"unsatisfied"` on its own does not say whether to widen a query, set `prefer`,
    or accept the loss.
    """

    subject: str
    """What this explanation is about, e.g. a role path or a group"""

    status: ExplanationStatus
    """Status"""

    message: str = ""
    """Details, if there are any to add"""

    parts: tuple[Explanation, ...] = ()
    """Explanations of the parts, in the order they were evaluated"""

    def render(self, indent: int = 0) -> str:
        """
        Render as indented text, one line per explanation

        Parameters
        ----------
        indent
            Indentation level to start at

        Returns
        -------
        :
            The rendered explanation

        Examples
        --------
        >>> satisfied = ExplanationStatusOk.SATISFIED
        >>> unsatisfied = ExplanationStatusNotOk.UNSATISFIED
        >>> print(
        ...     Explanation(
        ...         "model=MIROC6",
        ...         unsatisfied,
        ...         parts=(
        ...             Explanation("tas", satisfied, "#1 ('CMIP6.a.tas')"),
        ...             Explanation("rlut", unsatisfied, "no dataset matches ..."),
        ...         ),
        ...     ).render()
        ... )
        [unsatisfied] model=MIROC6
          [satisfied] tas: #1 ('CMIP6.a.tas')
          [unsatisfied] rlut: no dataset matches ...
        """
        line = f"{'  ' * indent}[{self.status.value}] {self.subject}"
        if self.message:
            line = f"{line}: {self.message}"

        return "\n".join([line, *(part.render(indent + 1) for part in self.parts)])


# TODO future as tree grows:
# Three more fields join `roles` as the tree grows, each carrying what its own node
# type produces: `lineages` (the chain of parents behind each dataset), `choices`
# (which alternative an `any_of` used) and `notes` (an optional part dropped, a check
# which passed with a complaint). None of them are here yet, because nothing can put
# anything in them yet.
@dataclass(frozen=True)
class NodeResultOk:
    """
    A node of the tree which is satisfied, within one group
    """

    roles: Mapping[str, tuple[CatalogueEntry, ...]]
    """Role path -> the datasets filling it"""

    explanation: Explanation
    """How this node was satisfied"""


@dataclass(frozen=True)
class NodeResultNotOk:
    """
    Why one node of the tree was not satisfied, within one group
    """

    status: ExplanationStatusNotOk
    """Status"""

    explanation: Explanation
    """Explanation"""


NodeResult = NodeResultOk | NodeResultNotOk
"""The result of evaluating one node of the tree, within one group"""


@dataclass(frozen=True)
class Solution:
    """
    The datasets which satisfy the requirement for one group
    """

    key: GroupKey
    """Values of the `group_by` facets"""

    roles: Mapping[str, tuple[CatalogueEntry, ...]]
    """
    Role path -> the datasets filling it

    Always a tuple, whatever the
    [cardinality][esmporium.requirements.Requirement.cardinality], so that everything
    reading this has one shape to handle. [one][(m).Solution.one] is the way to
    ask for the single dataset in a role.
    """

    explanation: Explanation
    """How the group was satisfied"""

    def one(self, role: str) -> CatalogueEntry:
        """
        Get the single dataset in a role

        Parameters
        ----------
        role
            Role path

        Returns
        -------
        :
            The dataset filling `role`

        Raises
        ------
        KeyError
            `role` is not a role path of the requirement

        ValueError
            `role` holds more than one dataset, i.e. the requirement was probably
            solved with `cardinality=Cardinality.ALL`
        """
        entries = self.roles[role]
        if len(entries) != 1:
            msg = (
                f"{role!r} holds {len(entries)} datasets, so there is no single one to "
                "return. Read `roles` directly (solving with "
                "`cardinality=Cardinality.ONE` may also fix this)."
            )
            raise ValueError(msg)

        return entries[0]


@dataclass(frozen=True)
class UnsolvedGroup:
    """
    A group for which no solution was found
    """

    key: GroupKey
    """Values of the `group_by` facets"""

    status: ExplanationStatusNotOk
    """Status"""

    explanation: Explanation
    """Explanation"""


@dataclass(frozen=True)
class SolveResult:
    """
    The result of solving a requirement: every group, sorted into what became of it

    Every group which was discovered appears in exactly one of the three mappings,
    one per status.
    """

    requirement: Requirement
    """
    The requirement which was solved

    Kept so that a result can answer for itself, which is what makes "has this become
    satisfiable?" a comparison of two results:
    [requirement_hash][esmporium.requirements.Requirement.requirement_hash] tells you
    whether both solves asked the same question, so that a group which moved from
    `unsatisfied` to `satisfied` means new data arrived rather than someone having
    quietly edited what was asked for.
    """

    satisfied: Mapping[GroupKey, Solution] = field(default_factory=dict)
    """Solutions, i.e. groups for which the requirement is satisfied"""

    unsatisfied: Mapping[GroupKey, UnsolvedGroup] = field(default_factory=dict)
    """Groups missing a dataset"""

    ambiguous: Mapping[GroupKey, UnsolvedGroup] = field(default_factory=dict)
    """Groups where a role had several candidates and nothing to choose between them"""

    def explain(self) -> str:
        """
        Explain every group

        Returns
        -------
        :
            A title naming the requirement, then one block per group, in a
            stable order.

            The title is there because the blocks name only their group: two
            requirements solved against the same catalogue are otherwise
            indistinguishable once the output is pasted somewhere else.

            When no group was discovered at all there is nothing to render, so the
            answer says why instead: an empty string would read as a bug.
        """
        groups: list[Solution | UnsolvedGroup] = [
            *self.satisfied.values(),
            *self.unsatisfied.values(),
            *self.ambiguous.values(),
        ]
        if not groups:
            # `_no_groups` names the requirement itself, so it needs no title.
            return self._no_groups()

        title = (
            f"Requirement {self.requirement.name!r}, grouped by "
            f"{readable_list(group_facets(self.requirement))}:"
        )

        return "\n\n".join(
            [
                title,
                *(
                    group.explanation.render()
                    for group in sorted(groups, key=lambda group: _sortable(group.key))
                ),
            ]
        )

    def _no_groups(self) -> str:
        where = self.requirement.where
        asked = "; ".join(
            f"{leaf.role} ({describe_query(effective_query(leaf, where))})"
            for _, leaf in walk_leaves(self.requirement.tree)
        )

        return (
            "No groups were discovered, so nothing was solved: no dataset in the "
            f"catalogue matched any leaf of requirement {self.requirement.name!r}. "
            "A group is discovered from the candidates the leaves find, grouped by "
            f"{readable_list(group_facets(self.requirement))}, so no candidates means "
            f"no groups. The leaves asked for: {asked}."
        )


@dataclass(frozen=True)
class _Context:
    """What stays the same while one group is evaluated"""

    requirement: Requirement
    found: Mapping[str, tuple[CatalogueEntry, ...]]
    """
    Role path -> its leaf's candidates, before grouping

    Built once by [_candidates_by_role][(m)._candidates_by_role], because a leaf's
    candidates are the same in every group.
    """
    group: dict[str, str | None]


def _sortable(key: GroupKey) -> tuple[str, ...]:
    return tuple("" if value is None else value for _, value in key)


def _describe_group(group: Mapping[str, str | None]) -> str:
    """
    Describe a group, for a message a person is going to read

    One spelling, used by the group's own explanation and by the messages of the
    leaves inside it. Two spellings would be free to drift, and a leaf which named
    the group differently to the block it sits under would read as a second group.

    Parameters
    ----------
    group
        Facet -> value, as [_Context.group][(m)._Context] holds it

    Returns
    -------
    :
        The group, e.g. `model=ACCESS-CM2, variant_label=r1i1p1f1`
    """
    return ", ".join(f"{facet}={value}" for facet, value in group.items())


def describe_query(query: QueryProtocol) -> str:
    """
    Describe a query briefly, for a message a person is going to read

    Every facet the query sets, whichever of the three homes it sits in, since
    [set_facets][esmporium.requirements.set_facets] flattens all three.

    Parameters
    ----------
    query
        Query to describe

    Returns
    -------
    :
        The query's facets, e.g. `experiment=abrupt-4xCO2|abrupt4xCO2, variable=tas`,
        with `|` between the values of one facet because several values are an "or"

    Examples
    --------
    >>> from esmporium.query import Query
    >>> describe_query(
    ...     Query(variable="tas", experiment=("abrupt-4xCO2", "abrupt4xCO2"))
    ... )
    'experiment=abrupt-4xCO2|abrupt4xCO2, variable=tas'
    """
    return ", ".join(
        f"{facet}={'|'.join(values)}" for facet, values in set_facets(query).items()
    )


def _describe_entry(entry: CatalogueEntry) -> str:
    # Both ID and id_project_specific. The integer is the key to the
    # row, for anyone going to look it up or link to it; the project-specific ID is the
    # dataset in the project's own language, which is what a user can search for.
    return f"#{entry.id} ({entry.id_project_specific!r})"


def _differing_facets(candidates: Sequence[CatalogueEntry]) -> tuple[str, ...]:
    """
    Get the facets on which candidates disagree

    What is worth printing when a role is ambiguous. Every candidate matched the same
    query and the same group, so the facets they have in common say nothing about why
    there is more than one; the facets they differ on are exactly what `prefer` would
    have to name.

    Parameters
    ----------
    candidates
        The candidates, at least one

    Returns
    -------
    :
        The facets which take more than one value across `candidates`
    """
    # Only facets every candidate can answer for. A facet one of them does not know
    # reads as `None`, so including it would report a difference between "gn" and
    # "nothing recorded", which is not a difference the user can act on with `prefer`.
    shared = frozenset.intersection(
        *(candidate.known_facets() for candidate in candidates)
    ) - frozenset(DATASET_FACET_COLUMNS)

    return tuple(
        facet
        for facet in (*DATASET_FACET_COLUMNS, *sorted(shared))
        if len({candidate.facet(facet) for candidate in candidates}) > 1
    )


def _ambiguous_message(candidates: Sequence[CatalogueEntry], ctx: _Context) -> str:
    """
    Explain an ambiguity in terms of what the user can do about it

    Parameters
    ----------
    candidates
        The candidates left over, more than one

    ctx
        What is being solved

    Returns
    -------
    :
        The message
    """
    differing = _differing_facets(candidates)
    if not differing:
        listed = ", ".join(_describe_entry(candidate) for candidate in candidates)

        # Whether anything was left unread decides which of these two is true, and
        # they are not variations on one message. A solve which read everything has
        # found a hole in our data model; a solve which was told to read only some
        # facets has simply not looked, and pointing that at the issue tracker would
        # send us bug reports about an optimisation working as intended.
        if any(candidate.facets_loaded is not None for candidate in candidates):
            named = readable_list(sorted(facets_read(ctx.requirement)))

            return (
                f"{len(candidates)} candidates which agree on every facet this solve "
                f"read, so nothing in hand can choose between them: {listed}. "
                f"This requirement names {named}, so nothing else was read: a facet "
                "which tells these apart may exist and simply not have been looked "
                "at. Name it in the requirement to have it read -- in "
                "`group_by_other_terms` or `prefer_other_terms` for a facet only a "
                "search document knows -- or use `cardinality=Cardinality.ALL` to "
                "keep them all."
            )

        # The issue tracker rather than advice, because a catalogue backed by our
        # database cannot get here: ingestion refuses two datasets which agree on
        # every column, `id_project_specific` included (see
        # [UnhandledDatasetClashError][esmporium.db.UnhandledDatasetClashError]),
        # so these two differ in a facet which exists but reached neither a column
        # nor `project_facets` nor `other_terms`. That is ours to fix, not the
        # user's to work around, and
        # "narrow the query" is not offered because there is nothing visible to
        # narrow on.
        return (
            f"{len(candidates)} candidates which agree on every facet, so no facet can "
            f"choose between them: {listed}. They differ only in their "
            "project-specific ID, which cannot be used to break a tie."
            "This should not happen: datasets agreeing on every column are stored "
            "separately only when their project-specific IDs differ, so the facet "
            "which tells these apart exists but has not reached the solver. Please "
            "raise an issue at https://github.com/esmporium/esmporium/issues quoting "
            "the message above. `cardinality=Cardinality.ALL` keeps them all in "
            "the meantime."
        )

    listed = ", ".join(
        f"{_describe_entry(candidate)} with "
        + ", ".join(f"{facet}={candidate.facet(facet)!r}" for facet in differing)
        for candidate in candidates
    )
    lead = (
        f"{len(candidates)} candidates, differing in {readable_list(differing)}: "
        f"{listed}"
    )
    # Both preference fields, because either could be the one that was tried and came
    # up short, and a message which named only `prefer` would read as though
    # `prefer_other_terms` had been ignored.
    all_preferences = {
        **ctx.requirement.prefer,
        **ctx.requirement.prefer_other_terms,
    }
    if not all_preferences:
        example = differing[0]

        return (
            f"{lead}. Nothing was given to choose between them: set `prefer` on the "
            f"requirement (e.g. `prefer={{{example!r}: (...)}}`), narrow the query, or "
            "use `cardinality=Cardinality.ALL` to keep them all."
        )

    preferences = "; ".join(
        f"{facet} in the order {readable_list(order)}"
        for facet, order in all_preferences.items()
    )

    return (
        f"{lead}. Preferring {preferences} did not narrow this to one, since the "
        "candidates left over are equally preferred: add the facet they differ on to "
        "`prefer`, or narrow the query."
    )


def _choose(
    candidates: Sequence[CatalogueEntry],
    ctx: _Context,
    subject: str,
    cardinality: Cardinality,
) -> tuple[CatalogueEntry, ...] | NodeResultNotOk:
    """
    Narrow candidates to the datasets which fill a role

    Parameters
    ----------
    candidates
        The candidates, at least one

    ctx
        What is being solved

    subject
        What is being filled, used in the explanation

    cardinality
        How many datasets may fill the role

    Returns
    -------
    :
        The datasets, or why they could not be narrowed down
    """
    remaining = list(candidates)

    # Nothing below is reached unless there is a choice, which is the whole reason
    # `prefer` fails later than `group_by`: a preference which was never needed is not
    # a mistake worth stopping for. One consequence to know about: this makes an
    # unanswerable `prefer` facet depend on the data, so the same requirement can raise
    # against one catalogue and not another.
    #
    # Only the modelled field is checked. `prefer_other_terms` is unchecked by
    # construction, and an unanswerable facet there ranks every candidate equally (all
    # `None`, so all unlisted), which narrows nothing and leaves the tie to be reported
    # as ambiguous.
    if len(remaining) > 1 and ctx.requirement.prefer:
        unrankable = _unanswerable_facets(ctx.requirement.prefer, remaining)
        if unrankable:
            raise UnrankableFacetError(unrankable, subject, len(remaining))

        # The mixed-project case matters more here than it does for grouping, and is
        # the reason this check is not grouping's alone. An unlisted value ranks behind
        # every listed one, so a project-specific facet whose value is `None` for the
        # projects which do not have it ranks every one of their datasets last --
        # `prefer={"product": ("output2",)}` quietly becomes "prefer CMIP5 over CMIP6".
        # Grouping at least leaves a visible `product=None` in the key; this leaves
        # nothing at all, just a different dataset chosen.
        #
        # No `project not in group_by` carve-out is needed: if `project` is a group
        # facet then every candidate in a group shares a project, so either all of them
        # know the facet or none do and this cannot fire.
        _check_project_facets_are_shared(ctx.requirement.prefer, remaining, "prefer")

    # This way of doing preferences is tightly coupled to using a list of values.
    # If we ever open this up to allowing more custom injections,
    # this will need to be extracted out and re-written.
    #
    # The modelled preferences are applied before the escape-hatch ones, so a facet we
    # model always out-ranks one we do not rather than the two interleaving
    # alphabetically. Within each, the order is alphabetical, which means facet
    # priority is not the user's to choose -- a known limit, and the thing to fix if
    # preferences ever become pluggable (see `Requirement.prefer`).
    for preferences in (ctx.requirement.prefer, ctx.requirement.prefer_other_terms):
        for facet in sorted(preferences):
            order = preferences[facet]
            ranks = [
                order.index(value)
                if (value := candidate.facet(facet)) in order
                # Unlisted values rank behind every listed one, rather than being
                # dropped: `prefer` says which is better, not which is allowed.
                else len(order)
                for candidate in remaining
            ]
            best = min(ranks)
            remaining = [
                candidate
                for candidate, rank in zip(remaining, ranks, strict=True)
                if rank == best
            ]

    if cardinality is Cardinality.ONE and len(remaining) > 1:
        return NodeResultNotOk(
            ExplanationStatusNotOk.AMBIGUOUS,
            Explanation(
                subject,
                ExplanationStatusNotOk.AMBIGUOUS,
                _ambiguous_message(remaining, ctx),
            ),
        )

    return tuple(remaining)


def _in_group(entry: CatalogueEntry, ctx: _Context) -> bool:
    return all(entry.facet(facet) == value for facet, value in ctx.group.items())


def _eval_leaf(leaf: Leaf, ctx: _Context, role_prefix: str) -> NodeResult:
    path = f"{role_prefix}{leaf.role}"
    query = effective_query(leaf, ctx.requirement.where)

    # The candidates were found once, before any group was evaluated; a group is a
    # filter applied to them here rather than part of the question asked for them.
    candidates = [entry for entry in ctx.found[path] if _in_group(entry, ctx)]
    if not candidates:
        return NodeResultNotOk(
            ExplanationStatusNotOk.UNSATISFIED,
            Explanation(
                path,
                ExplanationStatusNotOk.UNSATISFIED,
                f"no dataset matches {describe_query(query)} "
                f"for {_describe_group(ctx.group)}",
            ),
        )

    chosen = _choose(candidates, ctx, path, ctx.requirement.cardinality)
    if isinstance(chosen, NodeResultNotOk):
        return chosen

    return NodeResultOk(
        {path: chosen},
        Explanation(
            path,
            ExplanationStatusOk.SATISFIED,
            ", ".join(_describe_entry(entry) for entry in chosen),
        ),
    )


def _label(node: Node) -> str:
    """
    Name a node in a way which says which node it is

    Parameters
    ----------
    node
        Node to name

    Returns
    -------
    :
        The name, built from the roles below it

    Raises
    ------
    TypeError
        `node` is not a node
    """
    if isinstance(node, Leaf):
        return node.role

    if isinstance(node, AllOf):
        return " & ".join(_label(child) for child in node.children)

    raise NotANodeError(node)


# TODO: note on future (delete this once arrives)
# Concatenating rather than replacing. Two used parts of a tree cannot
# share a role today (`all_of` refuses it), but the nodes which make a role
# legitimately repeatable are coming, and silently dropping half of a role
# would be a bad way to find that out.
def _merge(results: Sequence[NodeResultOk], explanation: Explanation) -> NodeResultOk:
    roles: dict[str, tuple[CatalogueEntry, ...]] = {}
    for result in results:
        for role, entries in result.roles.items():
            roles[role] = (*roles.get(role, ()), *entries)

    return NodeResultOk(roles, explanation)


def _worst(results: Sequence[NodeResultNotOk]) -> ExplanationStatusNotOk:
    return min((result.status for result in results), key=_STATUS_PRIORITY.__getitem__)


def _eval_all_of(node: AllOf, ctx: _Context, role_prefix: str) -> NodeResult:
    # Every child is evaluated, even once one has failed, so that the explanation says
    # everything that is wrong with the group rather than the first thing.
    results = [_eval_tree(child, ctx, role_prefix) for child in node.children]
    explanations = tuple(result.explanation for result in results)
    label = f"all_of({_label(node)})"

    failures = [result for result in results if isinstance(result, NodeResultNotOk)]
    if failures:
        status = _worst(failures)

        return NodeResultNotOk(status, Explanation(label, status, parts=explanations))

    # Everything left is `NodeResultOk`,
    # because `failures` returned above if anything was not.
    # The filter is for the type checkers, which cannot see that:
    # passing `results` straight through is a
    # `list[NodeResultOk | NodeResultNotOk]`
    # where `_merge` takes a `Sequence[NodeResultOk]`,
    # and both mypy and ty reject it.
    return _merge(
        [result for result in results if isinstance(result, NodeResultOk)],
        Explanation(label, ExplanationStatusOk.SATISFIED, parts=explanations),
    )


def _eval_tree(node: Node, ctx: _Context, role_prefix: str) -> NodeResult:
    if isinstance(node, Leaf):
        return _eval_leaf(node, ctx, role_prefix)

    if isinstance(node, AllOf):
        return _eval_all_of(node, ctx, role_prefix)

    raise NotANodeError(node)


def group_facets(requirement: Requirement) -> tuple[str, ...]:
    """
    Get the facets a group is keyed on, in the order the key uses

    The modelled facets first, then the escape-hatch ones, so that reading a group key
    does not depend on how the two happen to sort against each other.

    Parameters
    ----------
    requirement
        Requirement being solved

    Returns
    -------
    :
        [Requirement.group_by][esmporium.requirements.Requirement.group_by] followed by
        [group_by_other_terms][esmporium.requirements.Requirement.group_by_other_terms]
    """
    return (*requirement.group_by, *requirement.group_by_other_terms)


def facets_read(requirement: Requirement) -> frozenset[str]:
    """
    Get every facet a solve of this requirement will ask an entry about

    What [needs_facets][esmporium.requirements.Catalogue.find] wants: name these and a
    catalogue can leave everything else unread, which for
    [esmporium.db.DatabaseCatalogue][] means not opening the stored search documents at
    all.

    Parameters
    ----------
    requirement
        Requirement being solved

    Returns
    -------
    :
        The facet names, under the spelling the requirement used

        Facets we model under their canonical names, and the escape-hatch ones exactly
        as they were written, which is how a catalogue holds the two and so how it has
        to be asked for them.

    Examples
    --------
    >>> from esmporium.query import Query
    >>> from esmporium.requirements import leaf, requirement
    >>> req = requirement(
    ...     name="one",
    ...     tree=leaf(Query(variable="tas"), "tas"),
    ...     group_by=("model",),
    ...     prefer={"grid_label": ("gn",)},
    ... )
    >>> sorted(facets_read(req))
    ['grid_label', 'model', 'variable']

    The escape hatches come too, spelled as they were given:

    >>> req = requirement(
    ...     name="two",
    ...     tree=leaf(Query(variable="tas"), "tas"),
    ...     group_by=("model",),
    ...     group_by_other_terms=("cordex-cmip6:driving_source_id",),
    ... )
    >>> sorted(facets_read(req))
    ['cordex-cmip6:driving_source_id', 'model', 'variable']
    """
    named = {
        *group_facets(requirement),
        *requirement.prefer,
        *requirement.prefer_other_terms,
    }
    # The leaves' own facets as well, although a lookup matches on those whatever it is
    # told. Naming them costs nothing and means this is simply "every facet this
    # requirement mentions", which is a great deal easier to check than a rule about
    # which of them a catalogue works out for itself.
    for _, each_leaf in walk_leaves(requirement.tree):
        named |= set_facets(effective_query(each_leaf, requirement.where)).keys()

    return frozenset(named)


_LookupKey = tuple[
    tuple[tuple[str, tuple[str, ...]], ...],
    tuple[tuple[str, tuple[str, ...]], ...],
    VersionFilter,
]
"""
What decides a lookup's answer: the modelled facets asked, the literal ones, and
`versions`

The two groups of facets are kept apart rather than flattened because a facet we model
and the same name in `other_terms` are different questions -- see the note above
[Catalogue][esmporium.requirements.Catalogue] -- so two leaves asking them must not
share an answer.
"""


def _candidates_by_role(
    requirement: Requirement, catalogue: Catalogue
) -> dict[str, tuple[CatalogueEntry, ...]]:
    """
    Ask the catalogue once per distinct question, and map each leaf to its answer

    A leaf's candidates do not depend on the group: the group is a filter applied to
    them afterwards, in [_in_group][(m)._in_group]. So the lookup a leaf needs is the
    same in every group, and one requirement needs one lookup per distinct question
    rather than one per leaf per group.

    Parameters
    ----------
    requirement
        Requirement being solved

    catalogue
        Datasets available

    Returns
    -------
    :
        Role path -> the datasets its leaf's query matches, before grouping

        Two leaves asking the same question share one answer, and so one lookup.

    Raises
    ------
    ClashingFacetsError
        A leaf's effective query sets the same facet in more than one place
    """
    needs = facets_read(requirement)

    found: dict[str, tuple[CatalogueEntry, ...]] = {}
    answers: dict[_LookupKey, tuple[CatalogueEntry, ...]] = {}
    for role_prefix, each_leaf in walk_leaves(requirement.tree):
        query = effective_query(each_leaf, requirement.where)
        asked = asked_facets(query)
        key: _LookupKey = (
            tuple(sorted(asked.modelled.items())),
            tuple(sorted(asked.literal.items())),
            requirement.versions,
        )
        if key not in answers:
            answers[key] = catalogue.find(
                query, versions=requirement.versions, needs_facets=needs
            )

        found[f"{role_prefix}{each_leaf.role}"] = answers[key]

    return found


def _unanswerable_facets(
    facets: Iterable[str], candidates: Sequence[CatalogueEntry]
) -> list[str]:
    """
    Get the modelled facets not one of the candidates can answer for

    Parameters
    ----------
    facets
        The facets to check, i.e. the modelled ones a requirement named

    candidates
        The datasets in hand, at least one

    Returns
    -------
    :
        Those of `facets` which no candidate knows, in the order given
    """
    answerable: set[str] = set()
    for entry in candidates:
        answerable |= entry.modelled_facets()

    return [facet for facet in facets if facet not in answerable]


def _check_project_facets_are_shared(
    facets: Iterable[str], candidates: Sequence[CatalogueEntry], field_name: str
) -> None:
    """
    Check that any project-specific facet is answerable by every candidate

    Returns nothing: this is here to raise. Called after
    [_unanswerable_facets][(m)._unanswerable_facets], so a facet no candidate knows has
    already been reported as the clearer problem and what is left is the genuinely
    mixed case.

    Parameters
    ----------
    facets
        The facets to check, i.e. the modelled ones a requirement named

    candidates
        The datasets in hand, at least one

    field_name
        Field the facets were set on, for the message, e.g. `"group_by"`

    Raises
    ------
    MixedProjectFacetError
        A project-specific facet is answerable by some candidates and not others
    """
    # Nothing to do for the overwhelmingly common case, and no pass over the candidates
    # for it either: most requirements group and rank on canonical facets only.
    named = [facet for facet in facets if facet in project_specific_facet_names()]
    if not named:
        return

    for facet in named:
        knowing = sum(1 for entry in candidates if facet in entry.modelled_facets())
        if knowing != len(candidates):
            raise MixedProjectFacetError([facet], field_name, knowing, len(candidates))


# A note for developers: grouping never looks at `entry.project`,
# and neither does [_in_group][(m)._in_group].
# Mixing projects is instead caught by what the candidates can answer:
# `group_by=("product",)` over CMIP5 and CMIP6 raises
# [MixedProjectFacetError][(m).MixedProjectFacetError],
# because only the CMIP5 candidates record `product`
# (see the note above that class for why the test is
# what the candidates answer rather than which projects they come from).
# `group_by=("project", "product")` is the way through,
# and costs nothing because `project` is a column.
def _group_values(
    requirement: Requirement, found: Mapping[str, tuple[CatalogueEntry, ...]]
) -> list[tuple[str | None, ...]]:
    """
    Discover the groups, as the group facets' values every leaf's candidates take

    Parameters
    ----------
    requirement
        Requirement being solved

    found
        Role path -> its leaf's candidates, as
        [_candidates_by_role][(m)._candidates_by_role] built them

    Returns
    -------
    :
        One tuple of group facet values per group, in a stable order

        Empty when no leaf matched anything, in which case there is nothing to group
        and [SolveResult.explain][(m).SolveResult.explain] says so rather than this
        complaining about the facets.

    Raises
    ------
    UngroupableFacetError
        No candidate can answer for one of `requirement.group_by`

    MixedProjectFacetError
        Only some candidates can answer for a project-specific facet in
        `requirement.group_by`
    """
    facets = group_facets(requirement)

    # One leaf's candidates at a time, duplicates included: a dataset which matches two
    # leaves is counted once per leaf. That is what the counts in
    # [UngroupableFacetError][(m).UngroupableFacetError] and
    # [_check_project_facets_are_shared][(m)._check_project_facets_are_shared] are
    # counts of, so collapsing them here would change what those say.
    candidates: list[CatalogueEntry] = []
    for role_prefix, leaf in walk_leaves(requirement.tree):
        candidates.extend(found[f"{role_prefix}{leaf.role}"])

    if not candidates:
        return []

    unanswerable = _unanswerable_facets(requirement.group_by, candidates)
    if unanswerable:
        raise UngroupableFacetError(unanswerable, len(candidates))

    # Not raised when `project` is one of the group facets: each group then holds a
    # single project, so a facet only that project has is answerable throughout it and
    # the `None` group for the others is the group for those projects, which is
    # legible rather than accidental.
    if "project" not in requirement.group_by:
        _check_project_facets_are_shared(requirement.group_by, candidates, "group_by")

    values = {tuple(entry.facet(facet) for facet in facets) for entry in candidates}

    # Sorted so that the groups, and so the explanations, come out in the same order
    # every time.
    return sorted(
        values, key=lambda group: tuple("" if v is None else v for v in group)
    )


def solve(requirement: Requirement, catalogue: Catalogue) -> SolveResult:
    """
    Solve a requirement against a catalogue, group by group

    The catalogue is asked once per distinct question the requirement contains, before
    the first group is evaluated, and told which facets will be read
    ([facets_read][(m).facets_read]). Two things follow which a caller can see. One
    solve answers about a single state of the catalogue, so a search saving datasets
    while it runs is picked up by the next solve rather than part-way through this one.
    And the entries it hands back answer for the facets this requirement named; asking
    one about anything else raises
    [UnloadedFacetError][esmporium.requirements.UnloadedFacetError] rather than
    answering `None`, which would be indistinguishable from a dataset that records no
    value.

    Parameters
    ----------
    requirement
        What the analysis needs

    catalogue
        The datasets available

    Returns
    -------
    :
        Every group which was discovered, sorted into satisfied, unsatisfied and
        ambiguous, each with an explanation

    Raises
    ------
    UngroupableFacetError
        `group_by` names a facet no dataset can answer for, so no group can be formed.

    MixedProjectFacetError
        A project-specific facet in `group_by` or `prefer` is answerable by only some
        of the datasets found, i.e. they span more than one project.

        A `NotImplementedError`: see
        [MixedProjectFacetError][(m).MixedProjectFacetError] for the way through.

    UnrankableFacetError
        `prefer` names a facet no tied candidate can answer for.

    """
    satisfied: dict[GroupKey, Solution] = {}
    unsatisfied: dict[ExplanationStatusNotOk, dict[GroupKey, UnsolvedGroup]] = {
        ExplanationStatusNotOk.UNSATISFIED: {},
        ExplanationStatusNotOk.AMBIGUOUS: {},
    }

    found = _candidates_by_role(requirement, catalogue)

    facets = group_facets(requirement)
    for group_values in _group_values(requirement, found):
        group_dict = dict(zip(facets, group_values, strict=True))
        key: GroupKey = tuple(group_dict.items())
        subject = _describe_group(group_dict)
        ctx = _Context(requirement=requirement, found=found, group=group_dict)

        evaluated = _eval_tree(requirement.tree, ctx, role_prefix="")
        if isinstance(evaluated, NodeResultOk):
            satisfied[key] = Solution(
                key=key,
                roles=evaluated.roles,
                explanation=Explanation(
                    subject,
                    ExplanationStatusOk.SATISFIED,
                    parts=(evaluated.explanation,),
                ),
            )

        else:
            unsatisfied[evaluated.status][key] = UnsolvedGroup(
                key=key,
                status=evaluated.status,
                explanation=Explanation(
                    subject, evaluated.status, parts=(evaluated.explanation,)
                ),
            )

    return SolveResult(
        requirement=requirement,
        satisfied=satisfied,
        unsatisfied=unsatisfied[ExplanationStatusNotOk.UNSATISFIED],
        ambiguous=unsatisfied[ExplanationStatusNotOk.AMBIGUOUS],
    )
