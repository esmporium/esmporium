"""
Choosing which dataset version is a child's parent, out of the ones we hold

The definitions used in this module:
A parent declaration names a *simulation* -- a model, an experiment and a variant --
and a simulation is published as many datasets, each of which can be published more
than once. Turning that declaration into one dataset version is therefore two choices,
and this module makes both:

1. **which dataset** of the simulation, decided by the child's own variable and the
   facets which say how that variable was reported. See
   [match_on_facets][(m).match_on_facets].
2. **which version of that dataset** to use, which nothing published decides and
   nothing could. See [choose_parent_version][(m).choose_parent_version].
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from esmporium.db.schema import Dataset, DatasetVersion, ParentVersionChoice

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence


@dataclass(frozen=True)
class MatchOutcome:
    """
    What matching a parent *dataset* decided, which is one of three answers

    `parent_dataset` is the answer when there is one. The other two are both "no
    parent", and they are not the same thing:

    - **nothing matched**, with `candidates` empty: the simulation does not publish
      what the child needs. The lineage is intact and this one variable has a hole.
    - **several matched**, with `parent_dataset` unset and `candidates` populated: the
      rule could not choose.

    The version of the chosen dataset is a second question, answered separately by
    [VersionMatchOutcome][(m).VersionMatchOutcome].
    """

    parent_dataset: Dataset | None = None
    """
    The parent dataset chosen, or `None` if none was
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


# Note for developers:
# Pre-built so that nothing has to be injected for the projects we support, and
# injectable so that a user who knows better about their own data can replace
# one.
PARENT_MATCHERS_BY_PROJECT: Mapping[str, ParentMatcher] = {
    "CMIP5": match_cmip5_parent,
    "CMIP6": match_cmip6_parent,
    "CMIP7": match_cmip7_parent,
}
"""
The matcher to use for each project we know, keyed by
[Dataset.project][esmporium.db.schema.Dataset.project]
"""

DEFAULT_PARENT_MATCHER: ParentMatcher = match_on_facets
"""
The matcher for a project with no entry of its own
"""


class UnknownProjectMatcherError(KeyError):
    """
    Raised when a project has no matcher and no default was allowed
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
    and `candidates` populated when there are several and the chooser declined.
    """

    parent_version: DatasetVersion | None = None
    """The version chosen, or `None` if none was"""

    choice: ParentVersionChoice = ParentVersionChoice.CHOSEN
    """
    How `parent_version` was arrived at, and so how much of a decision it was

    See [ParentVersionChoice][esmporium.db.schema.ParentVersionChoice]. Meaningless when
    `parent_version` is `None`.

    Defaults to [CHOSEN][esmporium.db.schema.ParentVersionChoice.CHOSEN], which is what
    an injected [ParentVersionChooser][(m).ParentVersionChooser] gets for free: anything
    that is not our own rule speaking is somebody who knows the data speaking.
    [choose_parent_version][(m).choose_parent_version] sets the other two itself.
    """

    candidates: tuple[DatasetVersion, ...] = ()
    """
    The versions an injected chooser was given and declined to choose between

    Empty when `parent_version` is set, and empty when there were no versions at all.
    The shipped default never leaves this populated; it has a rule for every case.
    """

    passed_over: tuple[DatasetVersion, ...] = ()
    """
    The versions that lost, when `parent_version` was reached by a real decision

    Empty unless `choice` is
    [DEFAULTED][esmporium.db.schema.ParentVersionChoice.DEFAULTED]. Carried so that what
    is reported to a caller names what it did not pick, which is the part they would
    need in order to disagree.
    """

    reason: str | None = None
    """
    Why no version was chosen, in words, or `None` when one was

    Covers both no-answer cases: no versions were held at all, or a chooser was given
    several and declined. Written for a person reading the report it ends up in.
    """

    @property
    def is_ambiguous(self) -> bool:
        """
        Whether a chooser was given several versions and declined to pick

        Returns
        -------
        :
            `True` when no version was chosen but candidates survived.
        """
        return self.parent_version is None and bool(self.candidates)


class ParentVersionChooser(Protocol):
    """
    Picks which version of a matched parent dataset to use
    """

    def __call__(
        self, child: DatasetVersion, candidates: Sequence[DatasetVersion]
    ) -> VersionMatchOutcome:
        """
        Choose which version of the matched parent dataset to use

        Parameters
        ----------
        child
            The version whose parent is wanted

        candidates
            The versions of the already-matched parent dataset to choose between

            Every version of it that we hold, unfiltered: retracted ones included, and
            not narrowed to the latest. Judging those is the chooser's business.

        Returns
        -------
        :
            The choice. See [VersionMatchOutcome][(m).VersionMatchOutcome].
        """


def choose_parent_version(
    child: DatasetVersion, candidates: Sequence[DatasetVersion]
) -> VersionMatchOutcome:
    """
    Choose a parent version: the child's own if it is published, else the newest

    Three cases, and it always answers:

    1. **one version held** -- that one, labelled
       [ONLY_VERSION][esmporium.db.schema.ParentVersionChoice.ONLY_VERSION]. No decision
       was made, so there is nothing to report.
    2. **several, one of them carrying the child's own version string** -- that one,
       labelled [CHILD_VERSION][esmporium.db.schema.ParentVersionChoice.CHILD_VERSION].
       Two datasets sharing a publication date is usually one publication event, so that
       release is the one which belongs with the child: same CMOR tables, same fixes,
       same generation. Preferred **even when it is older** than other releases, because
       a contemporaneous pair is internally consistent where a 2019 child against a 2021
       parent may not be.
    3. **several, none of them the child's** -- the highest `version` string, labelled
       [DEFAULTED][esmporium.db.schema.ParentVersionChoice.DEFAULTED].

    Retracted releases are dropped before 2 and 3, so a retracted match never beats a
    live newer release. If *every* release is retracted the filter is skipped rather
    than the chain being abandoned: a retracted parent is still the parent.

    Parameters
    ----------
    child
        The version whose parent is wanted

        Read only for its `version` string, in case 2.

    candidates
        The versions of the already-matched parent dataset to choose between

    Returns
    -------
    :
        The choice. See [VersionMatchOutcome][(m).VersionMatchOutcome].

        Never ambiguous, and `parent_version` is `None` only when `candidates` is empty.

    Notes
    -----
    A CMIP5 parent can look as though it has several versions when it has one.
    [DatasetVersion.version][esmporium.db.schema.DatasetVersion.version] notes that
    CMIP5 replicas carry inconsistent version strings across nodes (a date on one, `1`
    on another), and a version row's identity is `(dataset_id, version)`, so one
    publication seen two ways is two rows. Taking the highest string picks the date over
    the bare integer, which is the informative one -- but it is reported as a decision
    all the same, so CMIP5 slightly over-reports having made one.
    """
    if not candidates:
        return VersionMatchOutcome(
            reason="we hold no versions of the matched parent dataset"
        )

    if len(candidates) == 1:
        return VersionMatchOutcome(
            parent_version=candidates[0],
            choice=ParentVersionChoice.ONLY_VERSION,
        )

    # A retracted parent is still the parent, so a dataset whose every release is
    # retracted falls back to the whole list rather than to no answer at all.
    live = [one for one in candidates if not one.retracted] or list(candidates)

    # At most one can match: a version string is unique within a dataset.
    chosen = next((one for one in live if one.version == child.version), None)
    choice = ParentVersionChoice.CHILD_VERSION
    if chosen is None:
        chosen = max(live, key=lambda one: one.version)
        choice = ParentVersionChoice.DEFAULTED

    return VersionMatchOutcome(
        parent_version=chosen,
        choice=choice,
        passed_over=tuple(one for one in candidates if one is not chosen),
    )


DEFAULT_PARENT_VERSION_CHOOSER: ParentVersionChooser = choose_parent_version
"""
The version chooser used when none is injected
"""
