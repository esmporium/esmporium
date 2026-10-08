"""
Solving a requirement: filling roles with datasets, group by group

The requirements tree says what is needed, the catalogue says what
exists, and the solver works out (group by group) whether the one can
be filled from the other.

The same tree is solved once per group: group_by says what one run is,
and each run gets its own verdict.
"""

# A note for whoever adds the next node type: [_eval][(m)._eval] is the fourth of the
# dispatch functions named in the note at the top of
# [`esmporium.requirements.tree`][]. Like the other three it ends in a `TypeError`
# rather than a silent fallthrough, so a forgotten branch fails loudly.

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from esmporium.db.schema import DATASET_FACET_COLUMNS
from esmporium.formatting import readable_list
from esmporium.query import QueryProtocol
from esmporium.requirements.catalogue import (
    Catalogue,
    CatalogueEntry,
    set_facets,
)
from esmporium.requirements.tree import (
    AllOf,
    Leaf,
    Node,
    NotANodeError,
    Requirement,
    effective_query,
    walk_leaves,
)

GroupKey = tuple[tuple[str, str | None], ...]
"""
`(facet, value)` pairs identifying a group, i.e. one run to be solved

A value can be `None` because a facet can be: CMIP5 has no concept of a grid, so
[`CatalogueEntry.grid_label`][esmporium.requirements.CatalogueEntry.grid_label] is
`None` for every CMIP5 dataset.
"""


# TODO future:
# A third, `"undetermined"`, arrives with constraints: a check which needs metadata the
# catalogue cannot supply can neither pass nor fail, and answering "no" on its behalf
# would be a lie. Nothing here can produce that yet, because no check exists to need it,
# so it is deliberately absent rather than defined and unreachable.
#
# TODO future:
# Two more join them later, for the same reason as `"undetermined"`: `"degraded"`
# when a check passes with a complaint, and `"absent"` when an optional part is
# dropped.
class ExplanationStatus(str, Enum):
    """
    Statuses an [Explanation][(m).Explanation] can carry
    """

    SATISFIED = "satisfied"
    """Everything asked for was found"""

    UNSATISFIED = "unsatisfied"
    """There is no dataset"""

    AMBIGUOUS = "ambiguous"
    """There are too many datasets and nothing to choose between them"""

    def __str__(self) -> str:
        """
        Get the status as it is written, e.g. `"satisfied"`

        Returns
        -------
        :
            The status' value
        """
        # Without this, `str` on a member gives "ExplanationStatus.SATISFIED" from
        # Python 3.11 on, which is what [Explanation.render][(m).Explanation.render]
        # would then put in front of a reader.
        return self.value


NotOkStatus = Literal[ExplanationStatus.UNSATISFIED, ExplanationStatus.AMBIGUOUS]
"""
Ways in which (part of) a requirement can fail to resolve

[ExplanationStatus][(m).ExplanationStatus] without `SATISFIED`, so that a field which
can only have failed says so in its type.

A `Literal` of two members rather than an enum of its own because an enum cannot be a
subset of another enum: two enums would mean two places to add the next status, and
two members meaning `"unsatisfied"` which are not the same object.
"""

_STATUS_PRIORITY: dict[NotOkStatus, int] = {
    ExplanationStatus.UNSATISFIED: 0,
    ExplanationStatus.AMBIGUOUS: 1,
}
"""
Which status a group takes when its parts fail in different ways, lowest first

`"unsatisfied"` wins over `"ambiguous"`: a group missing a dataset cannot be run
however the ambiguity is resolved, so the missing dataset is the honest headline. The
explanation still carries both, because nothing short-circuits -- see
[_eval_all_of][(m)._eval_all_of].
"""


@dataclass(frozen=True)
class Explanation:
    """
    Why (part of) a requirement did or did not resolve

    Output rather than diagnostics: [SolveResult.explain][(m).SolveResult.explain]
    renders these for a person to read. Most groups in a real archive do not resolve,
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
        >>> satisfied = ExplanationStatus.SATISFIED
        >>> unsatisfied = ExplanationStatus.UNSATISFIED
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
        line = f"{'  ' * indent}[{self.status}] {self.subject}"
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
class Resolved:
    """
    What one node of the tree resolved to, within one run (per group)
    """

    roles: Mapping[str, tuple[CatalogueEntry, ...]]
    """Role path -> the datasets filling it"""

    explanation: Explanation
    """How this node resolved"""


@dataclass(frozen=True)
class Unresolved:
    """
    Why one node of the tree did not resolve, within one group
    """

    status: NotOkStatus
    """Resolution status"""

    explanation: Explanation
    """Explanation"""


NodeResult = Resolved | Unresolved
"""The result of evaluating one node of the tree, within one group"""


@dataclass(frozen=True)
class ResolvedRun:
    """
    A run (per group) for which the requirement is satisfied

    In other words, one run of the analysis which can go ahead, and the datasets to run
    it on.
    """

    key: GroupKey
    """Values of the `group_by` facets"""

    roles: Mapping[str, tuple[CatalogueEntry, ...]]
    """
    Role path -> the datasets filling it

    Always a tuple, whatever the
    [cardinality][esmporium.requirements.Requirement.cardinality], so that everything
    reading this has one shape to handle. [one][(m).ResolvedRun.one] is the way to
    ask for the single dataset in a role.
    """

    explanation: Explanation
    """How the group was resolved"""

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
            `role` was not resolved

        ValueError
            `role` holds more than one dataset, i.e. the requirement was probably
            solved with `cardinality="all"`
        """
        entries = self.roles[role]
        if len(entries) != 1:
            msg = (
                f"{role!r} holds {len(entries)} datasets, so there is no single one to "
                "return. Read `roles` directly (solving with `cardinality='one'` "
                "may also fix this)."
            )
            raise ValueError(msg)

        return entries[0]


@dataclass(frozen=True)
class UnresolvedRun:
    """
    A run (per group) for which the requirement is not satisfied
    """

    key: GroupKey
    """Values of the `group_by` facets"""

    status: NotOkStatus
    """Status"""

    explanation: Explanation
    """Explanation"""


@dataclass(frozen=True)
class SolveResult:
    """
    The result of solving a requirement: every group, sorted into what became of it

    Every group which was discovered appears in exactly one of the three mappings.
    """

    requirement: Requirement
    """
    The requirement which was solved

    Kept so that a result can answer for itself, which is what makes "has this become
    satisfiable?" a comparison of two results:
    [requirement_hash][esmporium.requirements.Requirement.requirement_hash] tells you
    whether both solves asked the same question, so that a group which moved from
    `unsatisfied` to `resolved` means new data arrived rather than someone having
    quietly edited what was asked for.
    """

    resolved: Mapping[GroupKey, ResolvedRun] = field(default_factory=dict)
    """Groups which can be run"""

    unsatisfied: Mapping[GroupKey, UnresolvedRun] = field(default_factory=dict)
    """Groups missing a dataset"""

    ambiguous: Mapping[GroupKey, UnresolvedRun] = field(default_factory=dict)
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
        groups: list[ResolvedRun | UnresolvedRun] = [
            *self.resolved.values(),
            *self.unsatisfied.values(),
            *self.ambiguous.values(),
        ]
        if not groups:
            # `_no_groups` names the requirement itself, so it needs no title.
            return self._no_groups()

        title = (
            f"Requirement {self.requirement.name!r}, grouped by "
            f"{readable_list(self.requirement.group_by)}:"
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
            f"{readable_list(self.requirement.group_by)}, so no candidates means no "
            f"groups. The leaves asked for: {asked}."
        )


@dataclass(frozen=True)
class _Context:
    """What stays the same while one group is evaluated"""

    requirement: Requirement
    catalogue: Catalogue
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
    # Only `extra` keys every candidate carries: `facet` raises for a key an entry does
    # not have, and a message about an ambiguity should not fail with an error of its
    # own.
    shared_extra = set.intersection(*(set(candidate.extra) for candidate in candidates))

    return tuple(
        facet
        for facet in (*DATASET_FACET_COLUMNS, *sorted(shared_extra))
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

        # The issue tracker rather than advice, because a catalogue backed by our
        # database cannot get here: ingestion refuses two datasets which agree on
        # every column, `id_project_specific` included (see
        # [`UnhandledDatasetClashError`][esmporium.db.UnhandledDatasetClashError]),
        # so these two differ in a facet which exists but reached neither a column
        # nor `extra`. That is ours to fix, not the user's to work around, and
        # "narrow the query" is not offered because there is nothing visible to
        # narrow on.
        return (
            f"{len(candidates)} candidates which agree on every facet, so no facet can "
            f"choose between them: {listed}. They differ only in their "
            "project-specific ID, which `prefer` compares no more than a query can. "
            "This should not happen: datasets agreeing on every column are stored "
            "separately only when their project-specific IDs differ, so the facet "
            "which tells these apart exists but has not reached the solver. Please "
            "raise an issue at https://github.com/esmporium/esmporium/issues quoting "
            "the message above. `cardinality='all'` keeps them all in the meantime."
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
    if not ctx.requirement.prefer:
        example = differing[0]

        return (
            f"{lead}. Nothing was given to choose between them: set `prefer` on the "
            f"requirement (e.g. `prefer={{{example!r}: (...)}}`), narrow the query, or "
            "use `cardinality='all'` to keep them all."
        )

    preferences = "; ".join(
        f"{facet} in the order {readable_list(order)}"
        for facet, order in ctx.requirement.prefer.items()
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
    cardinality: Literal["one", "all"],
) -> tuple[CatalogueEntry, ...] | Unresolved:
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

    # This way of doing preferences is tightly coupled to using a list of values.
    # If we ever open this up to allowing more custom injections,
    # this will need to be extracted out and re-written.
    for facet in sorted(ctx.requirement.prefer):
        order = ctx.requirement.prefer[facet]
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

    if cardinality == "one" and len(remaining) > 1:
        return Unresolved(
            ExplanationStatus.AMBIGUOUS,
            Explanation(
                subject,
                ExplanationStatus.AMBIGUOUS,
                _ambiguous_message(remaining, ctx),
            ),
        )

    return tuple(remaining)


def _in_group(entry: CatalogueEntry, ctx: _Context) -> bool:
    return all(entry.facet(facet) == value for facet, value in ctx.group.items())


def _eval_leaf(leaf: Leaf, ctx: _Context, role_prefix: str) -> NodeResult:
    path = f"{role_prefix}{leaf.role}"
    query = effective_query(leaf, ctx.requirement.where)
    found = ctx.catalogue.find(query)

    candidates = [entry for entry in found if _in_group(entry, ctx)]
    if not candidates:
        return Unresolved(
            ExplanationStatus.UNSATISFIED,
            Explanation(
                path,
                ExplanationStatus.UNSATISFIED,
                f"no dataset matches {describe_query(query)} "
                f"for {_describe_group(ctx.group)}",
            ),
        )

    chosen = _choose(candidates, ctx, path, ctx.requirement.cardinality)
    if isinstance(chosen, Unresolved):
        return chosen

    return Resolved(
        {path: chosen},
        Explanation(
            path,
            ExplanationStatus.SATISFIED,
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
def _merge(results: Sequence[Resolved], explanation: Explanation) -> Resolved:
    roles: dict[str, tuple[CatalogueEntry, ...]] = {}
    for result in results:
        for role, entries in result.roles.items():
            roles[role] = (*roles.get(role, ()), *entries)

    return Resolved(roles, explanation)


def _worst(results: Sequence[Unresolved]) -> NotOkStatus:
    return min((result.status for result in results), key=_STATUS_PRIORITY.__getitem__)


def _eval_all_of(node: AllOf, ctx: _Context, role_prefix: str) -> NodeResult:
    # Every child is evaluated, even once one has failed, so that the explanation says
    # everything that is wrong with the group rather than the first thing.
    results = [_eval(child, ctx, role_prefix) for child in node.children]
    explanations = tuple(result.explanation for result in results)
    label = f"all_of({_label(node)})"

    failures = [result for result in results if isinstance(result, Unresolved)]
    if failures:
        status = _worst(failures)

        return Unresolved(status, Explanation(label, status, parts=explanations))

    # Everything left is `Resolved`, because `failures` returned above if anything
    # was not. The filter is for the type checkers, which cannot see that: passing
    # `results` straight through is a `list[Resolved | Unresolved]` where `_merge`
    # takes a `Sequence[Resolved]`, and both mypy and ty reject it.
    return _merge(
        [result for result in results if isinstance(result, Resolved)],
        Explanation(label, ExplanationStatus.SATISFIED, parts=explanations),
    )


def _eval(node: Node, ctx: _Context, role_prefix: str) -> NodeResult:
    if isinstance(node, Leaf):
        return _eval_leaf(node, ctx, role_prefix)

    if isinstance(node, AllOf):
        return _eval_all_of(node, ctx, role_prefix)

    raise NotANodeError(node)


def _group_values(
    requirement: Requirement, catalogue: Catalogue
) -> list[tuple[str | None, ...]]:
    """
    Discover the groups, as the `group_by` values every leaf's candidates take

    Parameters
    ----------
    requirement
        Requirement being solved

    catalogue
        Datasets available

    Returns
    -------
    :
        One tuple of `group_by` values per group, in a stable order
    """
    values: set[tuple[str | None, ...]] = set()
    for _, leaf in walk_leaves(requirement.tree):
        query = effective_query(leaf, requirement.where)
        found = catalogue.find(query)
        values.update(
            tuple(entry.facet(facet) for facet in requirement.group_by)
            for entry in found
        )

    # Sorted so that the groups, and so the explanations, come out in the same order
    # every time.
    return sorted(
        values, key=lambda group: tuple("" if v is None else v for v in group)
    )


def solve(requirement: Requirement, catalogue: Catalogue) -> SolveResult:
    """
    Solve a requirement against a catalogue, group by group

    Parameters
    ----------
    requirement
        What the analysis needs

    catalogue
        The datasets available

    Returns
    -------
    :
        Every group which was discovered, sorted into resolved, unsatisfied and
        ambiguous, each with an explanation

    Raises
    ------
    UnrecordedFacetError
        `group_by`, `prefer` or a leaf's query names a facet no dataset can answer for.

        Raised rather than reported as an unresolved group, because it is a mistake in
        the requirement rather than a fact about the data: no amount of new data would
        make the facet answerable, and reporting it group by group would make one
        mistake look like missing data everywhere.
    """
    resolved: dict[GroupKey, ResolvedRun] = {}
    unresolved: dict[NotOkStatus, dict[GroupKey, UnresolvedRun]] = {
        ExplanationStatus.UNSATISFIED: {},
        ExplanationStatus.AMBIGUOUS: {},
    }

    for group_values in _group_values(requirement, catalogue):
        group_dict = dict(zip(requirement.group_by, group_values, strict=True))
        key: GroupKey = tuple(group_dict.items())
        subject = _describe_group(group_dict)
        ctx = _Context(requirement=requirement, catalogue=catalogue, group=group_dict)

        evaluated = _eval(requirement.tree, ctx, role_prefix="")
        if isinstance(evaluated, Resolved):
            resolved[key] = ResolvedRun(
                key=key,
                roles=evaluated.roles,
                explanation=Explanation(
                    subject, ExplanationStatus.SATISFIED, parts=(evaluated.explanation,)
                ),
            )
        else:
            unresolved[evaluated.status][key] = UnresolvedRun(
                key=key,
                status=evaluated.status,
                explanation=Explanation(
                    subject, evaluated.status, parts=(evaluated.explanation,)
                ),
            )

    return SolveResult(
        requirement=requirement,
        resolved=resolved,
        unsatisfied=unresolved[ExplanationStatus.UNSATISFIED],
        ambiguous=unresolved[ExplanationStatus.AMBIGUOUS],
    )
