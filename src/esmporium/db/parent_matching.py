"""
Choosing which dataset is a child's parent, out of the ones a search turned up

A parent declaration names a *simulation* -- a model, an experiment and a variant -- and
a simulation is published as many datasets. Turning that into one dataset needs the
child's own variable, and the facets which say how that variable was reported. That
choice is this module's whole job.

Everything here is a **pure function over candidates**: no session, no network, no
lookup. That is what lets the same rule serve two situations which would otherwise drift
apart -- a parent just fetched by a search, and a parent fetched weeks ago that is
already stored. The candidates come from somewhere else; the decision is made here.

The matchers are split **by project and not by search API**. By the time anything
reaches this module the results are [Dataset][esmporium.db.schema.Dataset] rows, so the
difference between a Solr document and a STAC item has already been spent. What is left
is genuinely per project: CMIP5 has no concept of a grid at all, and `processing_id`
means a CMOR table for CMIP5 and CMIP6 but a variable branding suffix for CMIP7.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from esmporium.db.schema import Dataset

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence


SIMULATION_FACETS: tuple[str, ...] = ("project", "model", "experiment", "variant_label")
"""
The facets which identify a simulation, rather than a dataset within it

**Not compared by any matcher here**, and that is the important part: these are the
child's *parent's* values, not the child's own. A parent is in a different experiment
from its child, usually under a different variant, so comparing them child-to-candidate
would reject every real parent. Selecting the right simulation is
[candidate_parent_datasets][esmporium.db.candidate_parent_datasets]' job; choosing
within it is this module's.

Recorded here so that the division is written down in the place someone would look for
it. Note that `institution` is deliberately absent even from this: no source publishes
the parent's institution and it really does differ from the child's. `E3SM-1-0
historical` is published by `UCSB` where the `piControl` it branches from is published
by `E3SM-Project`.
"""

REPORTING_FACETS: tuple[str, ...] = ("variable", "reporting_interval", "processing_id")
"""
The facets which pick one dataset out of a simulation, for every project

These are what the match is really about. One simulation publishes `tas` more than once:
`CanESM5 piControl r1i1p1f1` has it as both `Amon`/`mon` and `day`/`day`, and CMIP5's
`CanESM2` does the same. A match on variable alone would pick between them by accident.
"""

GRIDDED_REPORTING_FACETS: tuple[str, ...] = (*REPORTING_FACETS, "grid_label")
"""
[REPORTING_FACETS][(m).REPORTING_FACETS] plus the grid, for projects which have one

CMIP5 has no concept of a grid label, so its column is always `NULL` and comparing it
says nothing. CMIP6 and CMIP7 both do, and two grids of one variable are two datasets.
"""


@dataclass(frozen=True)
class MatchOutcome:
    """
    What matching decided, which is one of three answers and not two

    `parent` is the answer when there is one. The other two are both "no parent", and
    they are not the same thing:

    - **nothing matched**, with `candidates` empty: the simulation does not publish
      what the child needs. The lineage is intact and this one variable has a hole.
    - **several matched**, with `parent` unset and `candidates` populated: the rule
      could not choose. That is worth reporting rather than resolving by picking the
      lowest id, which is what hid it before this module existed.
    """

    parent: Dataset | None = None
    """The dataset chosen, or `None` if none was"""

    candidates: tuple[Dataset, ...] = ()
    """
    The candidates which survived the facet comparison but could not be told apart

    Empty when `parent` is set, and empty when nothing matched at all.
    """

    reason: str | None = None
    """
    Why no single parent was chosen, in words, or `None` when one was

    Written for a person reading a failure, so it names the facets and values involved.
    """

    @property
    def is_ambiguous(self) -> bool:
        """
        Whether the rule found several parents it could not choose between

        Returns
        -------
        :
            `True` when no parent was chosen but candidates survived.
        """
        return self.parent is None and bool(self.candidates)


class ParentMatcher(Protocol):
    """
    Picks the one dataset which is a child's parent, out of some candidates

    Pure: given the same child and the same candidates it returns the same answer,
    whether those candidates came out of a search a moment ago or out of the database.
    An implementation must not query, fetch or commit.
    """

    def __call__(self, child: Dataset, candidates: Sequence[Dataset]) -> MatchOutcome:
        """
        Choose this child's parent

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
        A possible parent

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
    facets: Collection[str] = GRIDDED_REPORTING_FACETS,
    *,
    prefer_same_institution: bool = True,
) -> MatchOutcome:
    """
    Choose a parent by requiring some facets to agree, the engine every default uses

    Two passes:

    1. keep candidates which agree with the child on `facets`;
    2. if more than one survives and `prefer_same_institution`, keep those whose
       institution matches the child's -- but only if that leaves any, because the
       parent's institution genuinely can differ.

    Institution is a tie-break and never a requirement. Were it required, a `UCSB`
    `E3SM-1-0 historical` would never find its `E3SM-Project` `piControl`.

    **`candidates` are assumed to be datasets of the declared parent simulation
    already.** Nothing here re-checks the model, experiment or variant, because those
    are the *parent's* and differ from the child's by definition -- see
    [SIMULATION_FACETS][(m).SIMULATION_FACETS].

    Parameters
    ----------
    child
        The dataset whose parent is wanted

    candidates
        The datasets of the declared parent simulation to choose between

    facets
        The facets which must agree, on top of the simulation ones

        Defaults to [GRIDDED_REPORTING_FACETS][(m).GRIDDED_REPORTING_FACETS]. A project
        with no grid concept passes
        [REPORTING_FACETS][(m).REPORTING_FACETS] instead.

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
        return MatchOutcome(parent=matching[0])

    if prefer_same_institution:
        same_institution = [
            candidate
            for candidate in matching
            if facets_agree(child, candidate, "institution")
        ]
        # Only if it leaves something: the parent's institution can differ, so an empty
        # result here means the tie-break does not apply rather than that nothing fits.
        if len(same_institution) == 1:
            return MatchOutcome(parent=same_institution[0])
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
    Choose a CMIP5 parent, comparing everything except the grid

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
    return match_on_facets(child, candidates, REPORTING_FACETS)


def match_cmip6_parent(child: Dataset, candidates: Sequence[Dataset]) -> MatchOutcome:
    """
    Choose a CMIP6 parent, comparing the grid as well

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
    return match_on_facets(child, candidates, GRIDDED_REPORTING_FACETS)


def match_cmip7_parent(child: Dataset, candidates: Sequence[Dataset]) -> MatchOutcome:
    """
    Choose a CMIP7 parent, comparing the grid as well

    The same facets as CMIP6, and a separate function on purpose: `processing_id` is a
    variable branding suffix here rather than a CMOR table, so the two agreeing today is
    a coincidence of CMIP7's design and not something to rely on. Naming it separately
    is what lets one change without the other, the way
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
    return match_on_facets(child, candidates, GRIDDED_REPORTING_FACETS)


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

Compares the grid, which is right for every project that has one and harmless for a
project where both sides are `NULL`. It is a default rather than a refusal because a
project we have not met is more likely to be CMIP6-shaped than to be unmatchable, and
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
    Get the matcher for one project

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
