"""
Choosing which dataset version is a child's parent, out of the ones we hold

A parent declaration names a *simulation* -- a model, an experiment and a variant --
and a simulation is published as many datasets, each of which can be published more
than once. Turning that declaration into one dataset version is therefore two choices,
and this module makes both:

1. **which dataset** of the simulation, decided by the child's own variable and the
   facets which say how that variable was reported. See
   [match_on_facets][(m).match_on_facets].
2. **which version of that dataset**, decided by the child's own version, because
   nothing published says which. See [choose_parent_version][(m).choose_parent_version].

The facets which identify the *simulation* -- project, model, experiment and variant --
are **compared by nothing here**, and that is the important part: those are the child's
*parent's* values, not the child's own. A parent is in a different experiment from its
child, usually under a different variant, so comparing them child-to-candidate would
reject every real parent. Finding the right simulation is
[candidate_parent_datasets][esmporium.db.candidate_parent_datasets]' job; choosing
within it is this module's.

`institution` is not part of even that, and cannot be: no source publishes the parent's
institution and it really does differ from the child's. `E3SM-1-0 historical` is
published by `UCSB` where the `piControl` it branches from is published by
`E3SM-Project`. So institution is a tie-break here and never a requirement.

Everything here is a **pure function over candidates**: no session, no network, no
lookup. That is what lets the same rule serve two situations which would otherwise drift
apart -- a parent just fetched by a search, and a parent fetched weeks ago that is
already stored. The candidates come from somewhere else; the decision is made here.

The matchers are split **by project and not by search API**. By the time anything
reaches this module the results are [Dataset][esmporium.db.schema.Dataset] rows, so the
difference between a Solr document and a STAC item has already been spent. What is left
is genuinely per project: CMIP5 has no concept of a grid at all, and `processing_id`
means a CMOR table for CMIP5 and CMIP6 but a variable branding suffix for CMIP7. Each
project's matcher therefore spells its own facets out rather than sharing a constant
with another project, so that one changing cannot quietly change the other.

The version choice, by contrast, is **not** per project: a version string is a DRS-wide
concept and means the same thing everywhere. There is one rule, and a seam to replace
it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from esmporium.db.schema import Dataset, DatasetVersion

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence


@dataclass(frozen=True)
class MatchOutcome:
    """
    What matching a parent *dataset* decided, which is one of three answers and not two

    `parent_dataset` is the answer when there is one. The other two are both "no
    parent", and they are not the same thing:

    - **nothing matched**, with `candidates` empty: the simulation does not publish
      what the child needs. The lineage is intact and this one variable has a hole.
    - **several matched**, with `parent_dataset` unset and `candidates` populated: the
      rule could not choose. That is worth reporting rather than resolving by picking
      the lowest id, which is what hid it before this module existed.

    The version of the chosen dataset is a second question, answered separately by
    [VersionMatchOutcome][(m).VersionMatchOutcome].
    """

    parent_dataset: Dataset | None = None
    """
    The parent dataset chosen, or `None` if none was

    Named for its type, because the word "parent" alone is ambiguous in this codebase:
    a [DatasetVersionParent][esmporium.db.schema.DatasetVersionParent] names a
    simulation, and the `parent` field of every failure in
    [esmporium.db.parents_to_database][] is a
    [ParentSimulation][esmporium.db.ParentSimulation]. This one is a dataset.
    """

    candidates: tuple[Dataset, ...] = ()
    """
    The candidates which survived the facet comparison but could not be told apart

    Empty when `parent_dataset` is set, and empty when nothing matched at all.
    """

    reason: str | None = None
    """
    Why no single parent dataset was chosen, in words, or `None` when one was

    Written for a person reading a failure, so it names the facets and values involved.
    """

    @property
    def is_ambiguous(self) -> bool:
        """
        Whether the rule found several parent datasets it could not choose between

        Returns
        -------
        :
            `True` when no parent dataset was chosen but candidates survived.
        """
        return self.parent_dataset is None and bool(self.candidates)


class ParentMatcher(Protocol):
    """
    Picks the one dataset which is a child's parent, out of some candidates

    Pure: given the same child and the same candidates it returns the same answer,
    whether those candidates came out of a search a moment ago or out of the database.
    An implementation must not query, fetch or commit.

    Picks a *dataset*, not a version of one: see
    [ParentVersionChooser][(m).ParentVersionChooser] for the second step.
    """

    def __call__(self, child: Dataset, candidates: Sequence[Dataset]) -> MatchOutcome:
        """
        Choose this child's parent dataset

        Parameters
        ----------
        child
            The dataset whose parent is wanted

        candidates
            The datasets of the declared parent simulation to choose between

            May be empty, may contain datasets of other simulations (a broad search
            can return them), and may contain several which are equally good.

        Returns
        -------
        :
            The choice. See [MatchOutcome][(m).MatchOutcome].
        """


def facets_agree(child: Dataset, candidate: Dataset, facet: str) -> bool:
    """
    Whether one facet of a candidate matches the child's

    `None` equals `None`, which is what makes a CMIP5 match work: both sides have no
    grid label, and two absences are an agreement rather than a failure. This mirrors
    the `coalesce` in [Dataset][esmporium.db.schema.Dataset]'s own identity index, which
    treats `NULL` and `''` alike for the same reason.

    Parameters
    ----------
    child
        The dataset whose parent is wanted

    candidate
        A possible parent dataset

    facet
        The column to compare

    Returns
    -------
    :
        Whether the two agree on `facet`
    """
    return bool(getattr(child, facet) == getattr(candidate, facet))


def match_on_facets(
    child: Dataset,
    candidates: Sequence[Dataset],
    facets: Collection[str] = (
        "variable",
        "reporting_interval",
        "processing_id",
        "grid_label",
    ),
    *,
    prefer_same_institution: bool = True,
) -> MatchOutcome:
    """
    Choose a parent dataset by requiring some facets to agree

    The engine every project's matcher calls, in two passes:

    1. keep candidates which agree with the child on `facets`;
    2. if more than one survives and `prefer_same_institution`, keep those whose
       institution matches the child's -- but only if that leaves any, because the
       parent's institution genuinely can differ.

    Institution is a tie-break and never a requirement. Were it required, a `UCSB`
    `E3SM-1-0 historical` would never find its `E3SM-Project` `piControl`.

    **`candidates` are assumed to be datasets of the declared parent simulation
    already.** Nothing here re-checks the project, model, experiment or variant, because
    those are the *parent's* and differ from the child's by definition -- see this
    module's docstring.

    Parameters
    ----------
    child
        The dataset whose parent is wanted

    candidates
        The datasets of the declared parent simulation to choose between

    facets
        The facets which must agree, on top of the simulation ones

        What such a set is for: one simulation publishes `tas` more than once.
        `CanESM5 piControl r1i1p1f1` has it as both `Amon`/`mon` and `day`/`day`, and
        CMIP5's `CanESM2` does the same, so a match on variable alone would pick
        between them by accident. The facets which say *how* the variable was reported
        -- its reporting interval, its processing id and, where the project has one,
        its grid -- are what turn "the simulation publishes this variable" into "this
        is the dataset which continues the child's own series".

        Every project's matcher passes its own, so the default here is only ever what
        a project we have never met gets: see
        [DEFAULT_PARENT_MATCHER][(m).DEFAULT_PARENT_MATCHER]. It compares the grid,
        which is right for every project that has one and harmless for a project where
        both sides are `NULL`.

    prefer_same_institution
        Whether to break a tie by preferring the child's own institution

    Returns
    -------
    :
        The choice. See [MatchOutcome][(m).MatchOutcome].
    """
    matching = [
        candidate
        for candidate in candidates
        if all(facets_agree(child, candidate, facet) for facet in facets)
    ]
    if not matching:
        wanted = {facet: getattr(child, facet) for facet in facets}
        return MatchOutcome(
            reason=(
                f"none of the {len(candidates)} dataset(s) of the parent simulation "
                f"match {wanted!r}"
            )
        )

    if len(matching) == 1:
        return MatchOutcome(parent_dataset=matching[0])

    if prefer_same_institution:
        same_institution = [
            candidate
            for candidate in matching
            if facets_agree(child, candidate, "institution")
        ]
        # Only if it leaves something: the parent's institution can differ, so an empty
        # result here means the tie-break does not apply rather than that nothing fits.
        if len(same_institution) == 1:
            return MatchOutcome(parent_dataset=same_institution[0])
        if same_institution:
            matching = same_institution

    return MatchOutcome(
        candidates=tuple(matching),
        reason=(
            f"{len(matching)} datasets match equally well, differing only in facets "
            f"this project's matcher does not compare "
            f"(institutions: {sorted({one.institution for one in matching})})"
        ),
    )


def match_cmip5_parent(child: Dataset, candidates: Sequence[Dataset]) -> MatchOutcome:
    """
    Choose a CMIP5 parent dataset, comparing everything except the grid

    CMIP5 has no concept of a grid label, so
    [Dataset.grid_label][esmporium.db.schema.Dataset.grid_label] is `NULL` on both
    sides and comparing it would be comparing two absences. It is left out rather than
    relied on to agree, so that the rule says what it means.

    Parameters
    ----------
    child
        The dataset whose parent is wanted

    candidates
        The datasets to choose between

    Returns
    -------
    :
        The choice
    """
    return match_on_facets(
        child,
        candidates,
        ("variable", "reporting_interval", "processing_id"),
    )


def match_cmip6_parent(child: Dataset, candidates: Sequence[Dataset]) -> MatchOutcome:
    """
    Choose a CMIP6 parent dataset, comparing the grid as well

    `processing_id` here is the CMOR table, which is what separates `tas` on `Amon` from
    `tas` on `day` within one simulation.

    Parameters
    ----------
    child
        The dataset whose parent is wanted

    candidates
        The datasets to choose between

    Returns
    -------
    :
        The choice
    """
    return match_on_facets(
        child,
        candidates,
        ("variable", "reporting_interval", "processing_id", "grid_label"),
    )


def match_cmip7_parent(child: Dataset, candidates: Sequence[Dataset]) -> MatchOutcome:
    """
    Choose a CMIP7 parent dataset, comparing the grid as well

    The same facets as CMIP6, written out again on purpose rather than shared:
    `processing_id` is a variable branding suffix here rather than a CMOR table, so the
    two agreeing today is a coincidence of CMIP7's design and not something to rely on.
    Spelling them out separately is what lets one change without the other, the way
    [esmporium.search.search_api_facade.store][] names each project's facade parameters
    even where two projects share them.

    Parameters
    ----------
    child
        The dataset whose parent is wanted

    candidates
        The datasets to choose between

    Returns
    -------
    :
        The choice
    """
    return match_on_facets(
        child,
        candidates,
        ("variable", "reporting_interval", "processing_id", "grid_label"),
    )


PARENT_MATCHERS_BY_PROJECT: Mapping[str, ParentMatcher] = {
    "CMIP5": match_cmip5_parent,
    "CMIP6": match_cmip6_parent,
    "CMIP7": match_cmip7_parent,
}
"""
The matcher to use for each project we know, keyed by
[Dataset.project][esmporium.db.schema.Dataset.project]

Pre-built so that nothing has to be injected for the projects we support, and injectable
so that a user who knows better about their own data can replace one.
"""

DEFAULT_PARENT_MATCHER: ParentMatcher = match_on_facets
"""
The matcher for a project with no entry of its own

[match_on_facets][(m).match_on_facets] with its own facets left at their default, which
includes the grid: right for every project that has one and harmless for a project where
both sides are `NULL`. It is a default rather than a refusal because a project we have
not met is more likely to be CMIP6-shaped than to be unmatchable, and
[matcher_for_project][(m).matcher_for_project] can be told to refuse instead.
"""


class UnknownProjectMatcherError(KeyError):
    """
    Raised when a project has no matcher and no default was allowed

    Worth being able to ask for: matching the wrong way round is silent, where a refusal
    is not.
    """

    def __init__(self, project: str, known: Collection[str]) -> None:
        self.project = project
        """The project we have no matcher for"""

        self.known = tuple(sorted(known))
        """The projects we do have one for"""

        super().__init__(
            f"No parent matcher is registered for project {project!r}. "
            f"Known projects: {', '.join(repr(one) for one in self.known)}. "
            "Pass a matcher of your own, or allow the default by calling "
            "matcher_for_project with use_default=True, which compares variable, "
            "reporting interval, processing id and grid label."
        )


def matcher_for_project(
    project: str,
    matchers: Mapping[str, ParentMatcher] = PARENT_MATCHERS_BY_PROJECT,
    *,
    use_default: bool = True,
) -> ParentMatcher:
    """
    Get the parent-dataset matcher for one project

    Parameters
    ----------
    project
        The project to match for, as
        [Dataset.project][esmporium.db.schema.Dataset.project] spells it

    matchers
        The matchers to choose from. Pass a mapping of your own to override one, e.g.
        `{**PARENT_MATCHERS_BY_PROJECT, "CMIP6": my_matcher}`.

    use_default
        Whether a project with no entry falls back to
        [DEFAULT_PARENT_MATCHER][(m).DEFAULT_PARENT_MATCHER], rather than raising

    Returns
    -------
    :
        The matcher to use

    Raises
    ------
    UnknownProjectMatcherError
        `project` has no matcher and `use_default` is `False`
    """
    found = matchers.get(project)
    if found is not None:
        return found

    if use_default:
        return DEFAULT_PARENT_MATCHER

    raise UnknownProjectMatcherError(project, matchers)


@dataclass(frozen=True)
class VersionMatchOutcome:
    """
    What choosing *which version* of a parent dataset decided

    The same three answers as [MatchOutcome][(m).MatchOutcome], one step further down:
    `parent_version` when there is one, nothing at all when the dataset has no versions,
    and `candidates` populated when there are several and the rule could not choose.

    A separate type rather than a reuse of `MatchOutcome`, because the two stages fail
    for different reasons and are fixed in different ways: a dataset-stage ambiguity is
    answered by a [ParentMatcher][(m).ParentMatcher] of your own, a version-stage one by
    a [ParentVersionChooser][(m).ParentVersionChooser].
    """

    parent_version: DatasetVersion | None = None
    """The version chosen, or `None` if none was"""

    candidates: tuple[DatasetVersion, ...] = ()
    """
    The versions which could not be told apart

    Empty when `parent_version` is set, and empty when there were no versions at all.
    """

    reason: str | None = None
    """Why no single version was chosen, in words, or `None` when one was"""

    @property
    def is_ambiguous(self) -> bool:
        """
        Whether there were several versions the rule could not choose between

        Returns
        -------
        :
            `True` when no version was chosen but candidates survived.
        """
        return self.parent_version is None and bool(self.candidates)


class ParentVersionChooser(Protocol):
    """
    Picks which version of a parent dataset a child version branched from

    Pure, for the same reason [ParentMatcher][(m).ParentMatcher] is: an implementation
    must not query, fetch or commit.

    This is the second of the two steps, and the one where there is **nothing published
    to read**. A child dataset version branched from exactly one version of its parent
    dataset -- that is a fact about what was run -- but no source says which: the parent
    vocabulary names a simulation and, where it is published at all, a branch time.
    Never a version. So the answer has to be decided, and a decision nobody can see is
    worse than no decision, which is why an unresolvable case is reported instead of
    settled.
    """

    def __call__(
        self, child: DatasetVersion, candidates: Sequence[DatasetVersion]
    ) -> VersionMatchOutcome:
        """
        Choose the version of the parent dataset this child version branched from

        Parameters
        ----------
        child
            The version whose parent is wanted

        candidates
            The versions of the already-matched parent dataset to choose between

            Every version of it that we hold. May be empty, and may contain several.

        Returns
        -------
        :
            The choice. See [VersionMatchOutcome][(m).VersionMatchOutcome].
        """


def choose_parent_version(
    child: DatasetVersion, candidates: Sequence[DatasetVersion]
) -> VersionMatchOutcome:
    """
    Choose a parent version by preferring the child's own, then by there being only one

    Three cases, in order:

    1. **one candidate carries the child's own version string** -- that one. A version
       string is a publication datestamp, so a parent published in the same batch as its
       child is the one that generation of the child was produced against. It is also
       the only signal in the data at all, which matters more than it being a strong
       one.
    2. **the parent dataset has exactly one version** -- that one, whether or not its
       string matches. There is no choice to make, so there is nothing to report.
    3. **several versions and none of them the child's** -- ambiguous. Which one a child
       branched from is then a question only a person can answer, so it is handed back
       rather than guessed at. Inject a
       [ParentVersionChooser][(m).ParentVersionChooser] of your own to answer it in
       code.

    Not split per project, unlike the dataset-stage matchers: a version string is a
    DRS-wide concept and means the same thing in every project.

    Parameters
    ----------
    child
        The version whose parent is wanted

    candidates
        The versions of the already-matched parent dataset to choose between

    Returns
    -------
    :
        The choice. See [VersionMatchOutcome][(m).VersionMatchOutcome].
    """
    if not candidates:
        return VersionMatchOutcome(
            reason="we hold no versions of the matched parent dataset"
        )

    # At most one can match: a version string is unique within a dataset.
    for candidate in candidates:
        if candidate.version == child.version:
            return VersionMatchOutcome(parent_version=candidate)

    if len(candidates) == 1:
        return VersionMatchOutcome(parent_version=candidates[0])

    return VersionMatchOutcome(
        candidates=tuple(candidates),
        reason=(
            f"the parent dataset has {len(candidates)} versions "
            f"({', '.join(sorted(one.version for one in candidates))}) and none of "
            f"them is the child's own ({child.version!r}), so which one it branched "
            f"from cannot be decided from the metadata"
        ),
    )


DEFAULT_PARENT_VERSION_CHOOSER: ParentVersionChooser = choose_parent_version
"""
The version chooser used when none is injected

See [choose_parent_version][(m).choose_parent_version]. One rule for every project,
because a version string means the same thing in all of them.
"""
