"""
Solving a requirement: filling roles with datasets, group by group

[`Catalogue.find`][esmporium.requirements.Catalogue] answers "which datasets match this
one query?". An analysis asks something else: "do I have everything I need, *together*,
and for which models?". The answer to that is not a list of datasets, it is a verdict
per group, each with a reason attached.

[solve][(m).solve] is where the two halves of this package meet.
[`esmporium.requirements.tree`][] says what is needed,
[`esmporium.requirements.catalogue`][] says what exists,
and this works out, group by group, whether the one can be filled from the other.

Four things happen here which happen nowhere else:

1. **The groups are discovered**, as the distinct combinations of
   [group_by][esmporium.requirements.Requirement.group_by] values across every leaf's
   candidates. Forty models come out of the data rather than being listed up front.
1. **Each role is filled**, one dataset per slot,
   [prefer][esmporium.requirements.Requirement.prefer] breaking ties. A tie it cannot
   break is reported as `"ambiguous"` rather than guessed at.
1. **Each group is judged on its own.** One model resolving while the next does not is
   the normal case, not an error.
1. **The result explains itself**, see [Explanation][(m).Explanation].

The solver is **greedy and does not backtrack**: a choice made for one leaf is never
revisited in order to satisfy another. That is a deliberate trade, and it is why the
explanation matters as much as the answer: when a group does not resolve, the
explanation is what tells a user whether to widen a query, set `prefer`, or accept the
loss.

Today a tree holds leaves and `all_of` and nothing else, so greed has nothing to bite
on -- each leaf is independent once the group is fixed. It starts to mean something
when lineages and alternatives arrive.

Where this sits in the flow: a requirement becomes searches, esmporium searches ESGF
and stores what it finds, a catalogue reads those rows back, and this decides what they
add up to.
"""

# A note for whoever adds the next node type: [_eval][(m)._eval] is the fourth of the
# dispatch functions named in the note at the top of
# [`esmporium.requirements.tree`][]. Like the other three it ends in a `TypeError`
# rather than a silent fallthrough, so a forgotten branch fails loudly.

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from esmporium.db.schema import DATASET_FACET_COLUMNS
from esmporium.formatting import readable_list
from esmporium.query import QueryProtocol
from esmporium.requirements.catalogue import (
    Catalogue,
    CatalogueEntry,
    UnrecordedFacetError,
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
`(facet, value)` pairs identifying a group, i.e. one run of the analysis

A value can be `None` because a facet can be: CMIP5 has no concept of a grid, so
[`CatalogueEntry.grid_label`][esmporium.requirements.CatalogueEntry.grid_label] is
`None` for every CMIP5 dataset.
"""

NotOkStatus = Literal["unsatisfied", "ambiguous"]
"""
Ways in which (part of) a requirement can fail to resolve

`"unsatisfied"` is "there is no dataset", `"ambiguous"` is "there are too many and
nothing to choose between them".

A third, `"undetermined"`, arrives with constraints: a check which needs metadata the
catalogue cannot supply can neither pass nor fail, and answering "no" on its behalf
would be a lie. Nothing here can produce that yet, because no check exists to need it,
so it is deliberately absent rather than defined and unreachable.
"""

ExplanationStatus = Literal["satisfied", "unsatisfied", "ambiguous"]
"""
Statuses an [Explanation][(m).Explanation] can carry

The statuses of [NotOkStatus][(m).NotOkStatus], plus `"satisfied"`. Two more join them
later, for the same reason as `"undetermined"`: `"degraded"` when a check passes with a
complaint, and `"absent"` when an optional part is dropped.
"""

_STATUS_PRIORITY: dict[NotOkStatus, int] = {
    "unsatisfied": 0,
    "ambiguous": 1,
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

    The solver does not backtrack, so this is half the product rather than a debugging
    aid: a group which did not resolve is only useful if it says what was tried.
    """

    subject: str
    """What this explanation is about, e.g. a role path or a group"""

    status: ExplanationStatus
    """Outcome"""

    message: str = ""
    """Details, if there are any to add"""

    # @Claude is 'children' the software dev name? In future we will be looking for
    # children of experiments so is this language confusing?
    children: tuple[Explanation, ...] = ()
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
        >>> print(
        ...     Explanation(
        ...         "model=MIROC6",
        ...         "unsatisfied",
        ...         children=(
        ...             Explanation("tas", "satisfied", "#1 ('CMIP6.a.tas')"),
        ...             Explanation("rlut", "unsatisfied", "no dataset matches ..."),
        ...         ),
        ...     ).render()
        ... )
        [unsatisfied] model=MIROC6
          [satisfied] tas: #1 ('CMIP6.a.tas')
          [unsatisfied] rlut: no dataset matches ...
        """
        # @Claude what is the subject showing? i.e. what in the error message
        # is the model/group/project that isn't satisfied? Should more/less/different
        # information be provided here for the user?
        line = f"{'  ' * indent}[{self.status}] {self.subject}"
        if self.message:
            line = f"{line}: {self.message}"

        return "\n".join([line, *(child.render(indent + 1) for child in self.children)])


@dataclass(frozen=True)
class Resolved:
    """
    What one node of the tree resolved to, within one group

    Nodes resolve from the leaves up and are merged as they go (see `_merge`),
    so a group's result is the `Resolved` of the tree's root.

    Three more fields join `roles` as the tree grows, each carrying what its own node
    type produces: `lineages` (the chain of parents behind each dataset), `choices`
    (which alternative an `any_of` used) and `notes` (an optional part dropped, a check
    which passed with a complaint). None of them are here yet, because nothing can put
    anything in them yet.
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
    """Why the node did not resolve"""

    explanation: Explanation
    """Details"""


NodeResult = Resolved | Unresolved
"""The result of evaluating one node of the tree, within one group"""


@dataclass(frozen=True)
class ResolvedGroup:
    """
    A group for which the requirement is satisfied

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
    reading this has one shape to handle. [one][(m).ResolvedGroup.one] is the way to
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
            `role` holds more than one dataset, i.e. the requirement was solved with
            `cardinality="all"`
        """
        entries = self.roles[role]
        if len(entries) != 1:
            msg = (
                f"{role!r} holds {len(entries)} datasets, so there is no single one to "
                "return. Read `roles` directly, or solve with `cardinality='one'`."
            )
            raise ValueError(msg)

        return entries[0]


@dataclass(frozen=True)
class UnresolvedGroup:
    """
    A group for which the requirement is not satisfied
    """

    key: GroupKey
    """Values of the `group_by` facets"""

    status: NotOkStatus
    """Why the group did not resolve"""

    explanation: Explanation
    """Details"""


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

    resolved: Mapping[GroupKey, ResolvedGroup] = field(default_factory=dict)
    """Groups which can be run"""

    unsatisfied: Mapping[GroupKey, UnresolvedGroup] = field(default_factory=dict)
    """Groups missing a dataset"""

    ambiguous: Mapping[GroupKey, UnresolvedGroup] = field(default_factory=dict)
    """Groups where a role had several candidates and nothing to choose between them"""

    def explain(self) -> str:
        """
        Explain every group

        Returns
        -------
        :
            Rendered explanations, one block per group, in a stable order.

            When no group was discovered at all there is nothing to render, so the
            answer says why instead: an empty string would read as a bug.
        """
        groups: list[ResolvedGroup | UnresolvedGroup] = [
            *self.resolved.values(),
            *self.unsatisfied.values(),
            *self.ambiguous.values(),
        ]
        if not groups:
            return self._no_groups()

        return "\n\n".join(
            group.explanation.render()
            for group in sorted(groups, key=lambda group: _sortable(group.key))
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


# @Claude is this too smart? Shouldn't this just be an already defined error?
_ENTRY_FACET_ADVICE = (
    "Both `group_by` and `prefer` compare entries rather than queries, so they can "
    "only name facets every entry records or carries in its `extra`."
)
"""What to say when `group_by` or `prefer` names a facet no entry can answer for"""

# @Claude is this too smart? Shouldn't this just be an already defined error?
_QUERY_FACET_ADVICE = (
    "Note that a facet in a query's `other_terms` is never translated, so it reaches "
    "the catalogue spelt as it was written: `other_terms={'table_id': ('Amon',)}` asks "
    "for 'table_id', not 'processing_id', and no entry knows it. Declare the facet on "
    "a query class instead, and it is translated on the way in."
)
"""What to say when a leaf's query names a facet no entry can answer for"""


@contextlib.contextmanager
def _asking_about(source: str, advice: str, requirement: Requirement) -> Iterator[None]:
    """
    Say which part of a requirement asked, if a facet turns out to be unanswerable

    [`CatalogueEntry.facet`][esmporium.requirements.CatalogueEntry.facet] explains
    perfectly well that no dataset records the facet it was asked about. What it cannot
    know is who asked, and the fix differs: a facet in `group_by` or `prefer` is
    compared against entries, so it has to be one they carry, whereas a facet in a
    leaf's query may simply be spelt for a search API rather than for us.

    Parameters
    ----------
    source
        The part of the requirement doing the asking, e.g. ``"`prefer`"``

    advice
        What to advise, [_ENTRY_FACET_ADVICE][(m)._ENTRY_FACET_ADVICE] or
        [_QUERY_FACET_ADVICE][(m)._QUERY_FACET_ADVICE].

        Named by the call site, which knows what it is asking on behalf of, rather
        than worked out here from how `source` happens to be spelt.

    requirement
        The requirement being solved

    Yields
    ------
    :
        Nothing; this is here for its `except`
    """
    try:
        yield
    except UnrecordedFacetError as exc:
        # Appending to the message, rather than raising something new, on purpose. The
        # class stays the same, so anything catching `UnrecordedFacetError` keeps
        # working, and its `facets` and `entry_id` stay exactly as
        # `CatalogueEntry.facet` set them. All this adds is the sentence the catalogue
        # was not in a position to write.
        exc.args = (
            f"{exc.args[0]} This came up while solving requirement "
            f"{requirement.name!r}, from {source}. {advice}",
        )
        raise


def _sortable(key: GroupKey) -> tuple[str, ...]:
    return tuple("" if value is None else value for _, value in key)


# @Claude what is the role of this function?
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


# @claude do we need id_project_specific here? what is the use of it? to us,
# id_project_specific is just another column to satisfy uniqueness in our main dataset
def _describe_entry(entry: CatalogueEntry) -> str:
    # Both IDs, because they answer different questions. The integer is the key to the
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
        # @Claude see above about project_specific_id?? What is the benefit of
        # describing
        # it in the error message here...
        return (
            f"{len(candidates)} candidates which agree on every facet, so no facet can "
            f"choose between them: {listed}. The same dataset can be published under "
            "more than one project-specific ID, which is what this usually is. "
            "`prefer` cannot help; pick one by ID, or use `cardinality='all'`."
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
    # `prefer` first, then `cardinality` decides what to do with whatever survives it.
    # That is the order the design note gives, and it is why `prefer` is applied even
    # when `cardinality="all"`: ranking and keeping are different questions.
    #
    # Sorted rather than in the order the mapping was written, because narrowing one
    # facet at a time makes the first facet applied outrank the rest, and the mapping's
    # order is deliberately not part of `requirement_hash`. Insertion order would
    # therefore let two requirements which hash the same resolve differently. Sorting
    # is not a decision about which facet *should* outrank which -- the design note
    # names that as still to settle -- only a guarantee that one requirement always
    # solves the same way.
    with _asking_about("`prefer`", _ENTRY_FACET_ADVICE, ctx.requirement):
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
            "ambiguous",
            Explanation(subject, "ambiguous", _ambiguous_message(remaining, ctx)),
        )

    return tuple(remaining)


def _in_group(entry: CatalogueEntry, ctx: _Context) -> bool:
    with _asking_about("`group_by`", _ENTRY_FACET_ADVICE, ctx.requirement):
        return all(entry.facet(facet) == value for facet, value in ctx.group.items())


def _eval_leaf(leaf: Leaf, ctx: _Context, prefix: str) -> NodeResult:
    path = f"{prefix}{leaf.role}"
    query = effective_query(leaf, ctx.requirement.where)
    with _asking_about(
        f"the query on leaf {leaf.role!r}", _QUERY_FACET_ADVICE, ctx.requirement
    ):
        found = ctx.catalogue.find(query)

    candidates = [entry for entry in found if _in_group(entry, ctx)]
    if not candidates:
        # @Claude, who put this TODO below? Was it you? Is this what we want to be able
        # to do? Doesn't this go against the 'greedy', no backtracking logic? Or am
        # I wrong here?
        # TODO: say which facet was the one which found nothing, i.e. drop each facet
        # in turn, search again, and report the relaxation which would have matched.
        # That is the question a user actually has here ("is `rlut` missing entirely,
        # or only for this variant?"), and the query alone does not answer it. Left
        # out for now because it costs a search per facet.
        return Unresolved(
            "unsatisfied",
            Explanation(
                path,
                "unsatisfied",
                f"no dataset matches {describe_query(query)}",
            ),
        )

    chosen = _choose(candidates, ctx, path, ctx.requirement.cardinality)
    if isinstance(chosen, Unresolved):
        return chosen

    return Resolved(
        {path: chosen},
        Explanation(
            path,
            "satisfied",
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


def _merge(results: Sequence[Resolved], explanation: Explanation) -> Resolved:
    roles: dict[str, tuple[CatalogueEntry, ...]] = {}
    for result in results:
        for role, entries in result.roles.items():
            # Concatenating rather than replacing. Two used parts of a tree cannot
            # share a role today (`all_of` refuses it), but the nodes which make a role
            # legitimately repeatable are coming, and silently dropping half of a role
            # would be a bad way to find that out.
            roles[role] = (*roles.get(role, ()), *entries)

    return Resolved(roles, explanation)


def _worst(results: Sequence[Unresolved]) -> NotOkStatus:
    return min((result.status for result in results), key=_STATUS_PRIORITY.__getitem__)


# @Claude prefix has a meaning in search (i.e. the prefix for a STAC search request).
# Is this what you mean by prefix here? See tree.py where we had to rename prefix
# for this very reason.
# UNless it is the same prefix as used in search/
def _eval_all_of(node: AllOf, ctx: _Context, prefix: str) -> NodeResult:
    # Every child is evaluated, even once one has failed, so that the explanation says
    # everything that is wrong with the group rather than the first thing.
    results = [_eval(child, ctx, prefix) for child in node.children]
    explanations = tuple(result.explanation for result in results)
    label = f"all_of({_label(node)})"

    failures = [result for result in results if isinstance(result, Unresolved)]
    if failures:
        status = _worst(failures)

        return Unresolved(status, Explanation(label, status, children=explanations))

    return _merge(
        [result for result in results if isinstance(result, Resolved)],
        Explanation(label, "satisfied", children=explanations),
    )


def _eval(node: Node, ctx: _Context, prefix: str) -> NodeResult:
    if isinstance(node, Leaf):
        return _eval_leaf(node, ctx, prefix)

    if isinstance(node, AllOf):
        return _eval_all_of(node, ctx, prefix)

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
        with _asking_about(
            f"the query on leaf {leaf.role!r}", _QUERY_FACET_ADVICE, requirement
        ):
            found = catalogue.find(query)

        with _asking_about("`group_by`", _ENTRY_FACET_ADVICE, requirement):
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
    resolved: dict[GroupKey, ResolvedGroup] = {}
    unsatisfied: dict[GroupKey, UnresolvedGroup] = {}
    ambiguous: dict[GroupKey, UnresolvedGroup] = {}
    unresolved: dict[NotOkStatus, dict[GroupKey, UnresolvedGroup]] = {
        "unsatisfied": unsatisfied,
        "ambiguous": ambiguous,
    }

    for values in _group_values(requirement, catalogue):
        group = dict(zip(requirement.group_by, values, strict=True))
        key: GroupKey = tuple(group.items())
        subject = ", ".join(f"{facet}={value}" for facet, value in key)
        ctx = _Context(requirement=requirement, catalogue=catalogue, group=group)

        evaluated = _eval(requirement.tree, ctx, "")
        if isinstance(evaluated, Resolved):
            resolved[key] = ResolvedGroup(
                key=key,
                roles=evaluated.roles,
                explanation=Explanation(
                    subject, "satisfied", children=(evaluated.explanation,)
                ),
            )
        else:
            unresolved[evaluated.status][key] = UnresolvedGroup(
                key=key,
                status=evaluated.status,
                explanation=Explanation(
                    subject, evaluated.status, children=(evaluated.explanation,)
                ),
            )

    return SolveResult(
        requirement=requirement,
        resolved=resolved,
        unsatisfied=unsatisfied,
        ambiguous=ambiguous,
    )
