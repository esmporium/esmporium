"""
Finding and recording the datasets a dataset branched from

Ingestion records what a dataset *says* its parent is
(see [save_parent_info][esmporium.db.save_parent_info]). That is a simulation --
a model, an experiment and a variant -- and naming one is not the same as holding it.
This module closes that gap, in two steps which are deliberately separate:

- **resolving**, which asks only what is already in the database. No requests, so it
  is both the cheap path and the one which makes a second analysis free: a parent
  simulation found while chasing `tas` is the parent of `tos` as well, because a parent
  belongs to the simulation and not to the variable. Turning that simulation into one
  stored dataset *version* is [esmporium.db.parent_matching][]'s two-step decision.
- **searching**, which is the only part that costs anything. One search per *distinct*
  parent simulation, so many children pointing at one control run are one request
  between them.

Both steps start from a **parent fact**: one source's assertion about where a version
branched from, i.e. the `parent_*` columns of
[DatasetVersionParent][esmporium.db.schema.DatasetVersionParent], listed in
[PARENT_FACT_COLUMNS][esmporium.db.PARENT_FACT_COLUMNS]. A version *has* facts once any
source has spoken, which is not the same as our knowing its parent: a row whose
`parent_*` columns are all `None` declared no parent, and that counts.
"""

from __future__ import annotations

import datetime
import warnings
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import httpx
from sqlmodel import Session, col, select

from esmporium.db.parent_matching import (
    DEFAULT_PARENT_VERSION_CHOOSER,
    PARENT_MATCHERS_BY_PROJECT,
    matcher_for_project,
)
from esmporium.db.results_to_database import (
    build_result_processor,
    check_engine_is_configured,
    flushed_id,
)
from esmporium.db.schema import (
    Dataset,
    DatasetParentLink,
    DatasetVersion,
    DatasetVersionParent,
    ParentInfoSource,
    ParentResolution,
    ParentVersionChoice,
)
from esmporium.query.canonical_query import QueryCanonical
from esmporium.query.translate import translate_canonical_to_projects
from esmporium.search.check_query_values import (
    FindingKind,
    check_query_values_single_project,
    close_matches_difflib,
)
from esmporium.search.search.run import (
    build_parent_dataset_request,
    search_single_project,
)
from esmporium.search.search_api_facade.selectors import DEFAULT_SEARCH_API_SELECTOR

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from esmporium.db.parent_matching import ParentMatcher, ParentVersionChooser
    from esmporium.search.check_query_values import CloseMatcher
    from esmporium.search.health import SearchAPICallObserver
    from esmporium.search.search_api_facade.selectors import SearchAPIFacadeSelector


def _utcnow() -> datetime.datetime:
    """Get the current time, as a timezone-aware UTC datetime."""
    return datetime.datetime.now(datetime.timezone.utc)


class ParentFactsProvider(Protocol):
    """
    Makes sure some dataset versions have parent facts recorded, or says it cannot
    """

    def __call__(self, session: Session, dataset_version_ids: Collection[int]) -> None:
        """
        Record parent facts for any of these versions which has none

        Parameters
        ----------
        session
            The session to write into. Does not commit; the walk owns the boundary.

        dataset_version_ids
            The versions to see to. Ones which already have an assertion should be left
            alone -- re-reading a header costs a request for an answer we hold.
        """


class NoParentFactsProviderError(RuntimeError):
    """
    Raised when a dataset's parent facts cannot be obtained and nothing was offered
    """

    def __init__(self, missing: Mapping[str, int]) -> None:
        self.missing = dict(missing)
        """How many versions had no parent facts, per project"""

        counts = ", ".join(
            f"{count} {project}" for project, count in sorted(self.missing.items())
        )
        super().__init__(
            f"No parent facts are recorded for some datasets ({counts}), and no "
            "facts provider was given to obtain them. Only CMIP7 publishes parent "
            "information in the search index, so for every other project the facts "
            "come from a file header, which this package does not read yet. Pass "
            "find_parents a facts_provider which records them (writing through "
            "esmporium.db.save_parent_info with ParentInfoSource.FILE_HEADER), or "
            "restrict the walk to datasets whose facts are already stored."
        )


class ParentSearcher(Protocol):
    """
    Something which finds one parent simulation's datasets and stores them
    """

    def __call__(self, session: Session, item: ParentSearchPlanItem) -> None:
        """
        Find the datasets of one planned parent, and write them into `session`

        Parameters
        ----------
        session
            The session to write what is found into

        item
            The parent to find, and the variables wanted of it
        """


@dataclass(frozen=True, order=True)
class ParentSimulation:
    """
    One simulation a dataset declared as its parent

    The unit a parent search is planned in, and so the unit children are deduplicated
    into: every dataset of one simulation declares the same parent, so one of these can
    stand for hundreds of rows.
    """

    project: str
    """
    The project the parent belongs to, e.g. `CMIP7`

    See
    [DatasetVersionParent.parent_project][esmporium.db.schema.DatasetVersionParent.parent_project].
    """

    model: str
    """The climate model that ran it. See [Dataset.model][esmporium.db.schema.Dataset.model]."""  # noqa: E501

    experiment: str
    """The experiment. See [Dataset.experiment][esmporium.db.schema.Dataset.experiment]."""  # noqa: E501

    variant_label: str
    """
    Its variant label

    See
    [Dataset.variant_label][esmporium.db.schema.Dataset.variant_label]. Frequently
    *not* the child's own, which is why it is read rather than assumed.
    """

    def __str__(self) -> str:
        """Render as a space-separated identity, for error messages."""
        return f"{self.project} {self.model} {self.experiment} {self.variant_label}"


class IncompleteParentError(ValueError):
    """
    Raised when a declared parent names too little to be searched for
    """

    def __init__(self, parent_info: DatasetVersionParent) -> None:
        self.parent_info = parent_info
        """The row we could not read a simulation out of"""

        named = {
            "parent_project": parent_info.parent_project,
            "parent_model": parent_info.parent_model,
            "parent_experiment": parent_info.parent_experiment,
            "parent_variant_label": parent_info.parent_variant_label,
        }
        missing = sorted(column for column, value in named.items() if value is None)
        super().__init__(
            f"Dataset version {parent_info.dataset_version_id} declares a parent but "
            f"does not say {', '.join(missing)}, so there is nothing to search for. "
            f"It said {named!r}. A parent needs a project, a model, an experiment and "
            f"a variant to be findable, and the missing ones cannot be filled in from "
            f"the child without guessing which run it came from. Please raise an issue "
            f"at https://github.com/esmporium/esmporium/issues, quoting the version "
            f"and these values."
        )


class CrossProjectParentError(ValueError):
    """
    Raised when a dataset declares a parent in a different project from its own

    No project we search does this: CMIP7 records declare a CMIP7 era, and CMIP5 infers
    the era from the child. CMIP6Plus does -- MOHC's LESFMIP runs declare a CMIP6
    `piControl` -- so registering it makes this reachable. The assertion is recorded
    either way, so nothing is lost by refusing to chase it.
    """

    def __init__(self, parent_info: DatasetVersionParent, project: str) -> None:
        self.parent_info = parent_info
        """The row we refused"""

        self.project = project
        """The project of the dataset which declared it"""

        super().__init__(
            f"Dataset version {parent_info.dataset_version_id} is in project "
            f"{project!r} but declares a parent in "
            f"{parent_info.parent_project!r}. Finding it would mean searching another "
            f"project, and deciding which project that published MIP era is served "
            f"under, which is a guess because a MIP era and a project are not the same "
            f"concept. CMIP6Plus is known to declare a CMIP6 parent; chasing one "
            f"needs a facade registered for the other project, which nothing wires up "
            f"yet."
        )


class UnknownUntilExperimentError(ValueError):
    """
    Raised when the experiment a walk is told to stop at does not exist

    Checked before anything is searched for, because a misspelled target is the one
    failure which looks exactly like success: the walk simply never matches it, climbs
    to the top of every chain and reports that none of them got there. Finding out at
    the start costs one request and turns that into a named failure.
    """

    def __init__(self, experiment: str, suggestions: tuple[str, ...]) -> None:
        self.experiment = experiment
        """The experiment we were told to stop at"""

        self.suggestions = suggestions
        """
        Experiments the API does publish which look close to it

        Carried but deliberately not put in the message: a guess rendered into an error
        reads as advice, and `difflib`'s notion of close is a default rather than a
        judgement about experiment names. Every other reader of a
        [FacetFinding][esmporium.search.FacetFinding] treats these as data too.
        """

        super().__init__(
            f"The experiment to walk back to, {experiment!r}, is not one the search "
            f"API publishes -- it is either misspelled or not a name this project "
            f"uses. Nothing was searched for: a target which does not exist can never "
            f"be reached, so every chain would have climbed to its top and reported "
            f"that it never got there."
        )


@dataclass(frozen=True)
class ParentNotFound:
    """
    A dataset declared a parent which the search index has nothing for
    """

    child: ParentSimulation
    """The simulation which declared it"""

    parent: ParentSimulation
    """The parent which could not be found"""

    def __str__(self) -> str:
        """Render the failure as the message shown to the caller."""
        return (
            f"{self.child} declares parent ({self.parent}) but the search index "
            f"publishes no datasets for it, so the chain cannot be completed."
        )


@dataclass(frozen=True)
class ParentNotAncestor:
    """
    A chain reached a dataset which declares no parent without passing the target
    """

    reached: ParentSimulation
    """
    The root the chain ended at

    Deliberately the root rather than the dataset the walk started from. A control run
    has many descendants and they merge as the chain is climbed, so by the time a top
    is reached there is no single starting point to name -- which of them came through
    here is answered by the links, not by this.
    """

    until_experiment: tuple[str, ...]
    """The experiments the walk was looking for"""

    def __str__(self) -> str:
        """Render the failure as the message shown to the caller."""
        wanted = ", ".join(repr(experiment) for experiment in self.until_experiment)
        return (
            f"{self.reached} declares no parent, so a chain climbing through it ends "
            f"there without passing through any of {wanted}."
        )


@dataclass(frozen=True)
class VariableGap:
    """
    A parent simulation exists but does not publish one of its children's variables

    No [DatasetParentLink][esmporium.db.schema.DatasetParentLink] is written, so a
    later search which does find that variable can resolve the row then.
    """

    child_dataset_version_id: int
    """The version whose parent could not be matched"""

    parent: ParentSimulation
    """The parent simulation which exists but does not publish the variable"""

    variable: str
    """The variable the parent does not publish"""

    def __str__(self) -> str:
        """Render as the message shown to the caller."""
        return (
            f"{self.parent} does not publish {self.variable!r}, which dataset version "
            f"{self.child_dataset_version_id} needs from it."
        )


@dataclass(frozen=True)
class AmbiguousParent:
    """
    Several datasets fit a declared parent equally well and the matcher could not choose
    """

    child_dataset_version_id: int
    """The version whose parent could not be chosen"""

    parent: ParentSimulation
    """The simulation it declared"""

    candidate_dataset_ids: tuple[int, ...]
    """The datasets which fitted equally well, as their ids"""

    reason: str
    """What the matcher said about why it could not choose"""

    def __str__(self) -> str:
        """Render as the message shown to the caller."""
        return (
            f"dataset version {self.child_dataset_version_id} declares parent "
            f"({self.parent}) and {self.reason}; candidates are datasets "
            f"{list(self.candidate_dataset_ids)}"
        )


@dataclass(frozen=True)
class AmbiguousParentVersion:
    """
    The parent dataset was found, and which *version* of it was branched from is unclear
    """

    child_dataset_version_id: int
    """The version whose parent version could not be chosen"""

    parent: ParentSimulation
    """The simulation it declared"""

    parent_dataset_id: int
    """The parent dataset which *was* matched, as its id"""

    candidate_dataset_version_ids: tuple[int, ...]
    """The versions of that dataset which could not be told apart, as their ids"""

    reason: str
    """What the chooser said about why it could not choose"""

    def __str__(self) -> str:
        """Render as the message shown to the caller."""
        return (
            f"dataset version {self.child_dataset_version_id} declares parent "
            f"({self.parent}), which matched dataset {self.parent_dataset_id}, but "
            f"{self.reason}; candidate versions are "
            f"{list(self.candidate_dataset_version_ids)}"
        )


_DECIDED_VERSION_CHOICES = frozenset(
    {ParentVersionChoice.CHILD_VERSION, ParentVersionChoice.DEFAULTED}
)
"""
The [ParentVersionChoice][esmporium.db.schema.ParentVersionChoice]s that are decisions

i.e. the ones where more than one release was held and a rule of ours picked. The other
two are not: `ONLY_VERSION` had nothing to pick from, and `CHOSEN` means the caller
picked, so neither is something to report back to them.
"""


@dataclass(frozen=True)
class ParentVersionDecision:
    """
    A parent dataset was published more than once and we picked which release to use

    A note on the dates: The releases passed over
    are often *all newer* than the child, so it can read as though a child branched from
    data that did not exist yet. A version string is a publication date, not a run date:
    the branch happened on the modelling centre's own disks before either end of it was
    published, and every release publishes that same run. See
    [ParentVersionChoice][esmporium.db.schema.ParentVersionChoice].

    To decide it yourself, inject a
    [ParentVersionChooser][esmporium.db.ParentVersionChooser].
    """

    child_dataset_version_id: int
    """The version whose parent release was decided"""

    parent: ParentSimulation
    """The simulation it declared"""

    parent_dataset_id: int
    """The parent dataset that was matched, as its id"""

    chosen_dataset_version_id: int
    """The release we used, as its id"""

    choice: ParentVersionChoice
    """
    Which rule picked it

    See [ParentVersionChoice][esmporium.db.schema.ParentVersionChoice]. Only ever
    [CHILD_VERSION][esmporium.db.schema.ParentVersionChoice.CHILD_VERSION] or
    [DEFAULTED][esmporium.db.schema.ParentVersionChoice.DEFAULTED] here; the other two
    values are not decisions, so they are not recorded as any.
    """

    passed_over_dataset_version_ids: tuple[int, ...]
    """
    The releases we did not use, as their ids

    Carried because it is the part a caller would need in order to disagree.
    """

    def __str__(self) -> str:
        """Render as the message shown to the caller."""
        if self.choice == ParentVersionChoice.CHILD_VERSION:
            why = (
                "one of them carries the child's own version string, so that release "
                "was used"
            )
        else:
            why = (
                "none of them carries the child's own version string, so the newest "
                "was used"
            )

        return (
            f"dataset version {self.child_dataset_version_id} declares parent "
            f"({self.parent}), which matched dataset {self.parent_dataset_id}; it is "
            f"published more than once and {why} "
            f"({self.chosen_dataset_version_id}), passing over "
            f"{list(self.passed_over_dataset_version_ids)}."
        )


class ParentVersionDecidedWarning(UserWarning):
    """
    Warned once per walk when any parent was held in more than one release
    """


class ParentResolutionError(RuntimeError):
    """
    One or more chains could not be walked back to the target

    Every failure is gathered into one error rather than the first one stopping the
    walk, so a run over many datasets reports everything wrong with it at once.
    """

    def __init__(self, failures: Sequence[ParentNotFound | ParentNotAncestor]) -> None:
        self.failures = list(failures)
        """Every chain which could not be walked, in the order they were found"""

        body = "\n".join(f"  - {failure}" for failure in self.failures)
        super().__init__(
            f"{len(self.failures)} parent chain(s) could not be resolved:\n{body}"
        )


@dataclass(frozen=True)
class ParentSearchPlanItem:
    """
    One search a walk needs to do: a parent simulation, and what to ask it for
    """

    parent: ParentSimulation
    """The simulation to search for"""

    variables: tuple[str, ...]
    """
    The variables to ask for, sorted
    """

    child_dataset_version_ids: tuple[int, ...]
    """
    The versions which declared this parent, sorted

    Carried so that one search's results can be resolved against exactly the rows which
    were waiting for it.
    """

    def to_canonical_query(self) -> QueryCanonical:
        """
        Build the canonical query which looks for this parent

        Returns
        -------
        :
            A single-project query naming the parent's model, experiment and variant,
            and the variables wanted of it.
        """
        return QueryCanonical(
            project=(self.parent.project,),
            model=(self.parent.model,),
            experiment=(self.parent.experiment,),
            variant_label=(self.parent.variant_label,),
            variable=self.variables,
        )


@dataclass(frozen=True)
class ParentResolutionResult:
    """What one pass of resolving against the stored datasets managed"""

    linked: tuple[DatasetParentLink, ...] = ()
    """The link rows written, flushed"""

    variable_gaps: tuple[VariableGap, ...] = ()
    """
    Children whose parent simulation is held but does not publish their variable

    Informational. See [VariableGap][(m).VariableGap].
    """

    ambiguous: tuple[AmbiguousParent, ...] = ()
    """
    Children whose parent *dataset* the matcher could not choose

    Informational here, and left unresolved so a better matcher can be injected and the
    resolve re-run. See [AmbiguousParent][(m).AmbiguousParent].
    """

    ambiguous_versions: tuple[AmbiguousParentVersion, ...] = ()
    """
    Children whose parent dataset was matched but whose parent *version* was not

    Only ever populated by an injected chooser which declined to pick; the shipped
    default always answers. Left unresolved so the resolve can be re-run. See
    [AmbiguousParentVersion][(m).AmbiguousParentVersion].
    """

    version_decisions: tuple[ParentVersionDecision, ...] = ()
    """
    Children whose parent was held in several releases, so which one to use was decided

    Resolved and linked, so this is informational -- but it is the normal case for a
    republished parent, not an edge. See
    [ParentVersionDecision][(m).ParentVersionDecision].
    """

    still_unresolved: tuple[int, ...] = ()
    """
    The ids of the assertion rows which found nothing, sorted

    Either the parent is not in the database yet, which is what a search is for, or it
    is there without the child's variable, in which case it is in `variable_gaps` too.
    """


@dataclass
class ParentWalkResult:
    """What a whole walk managed"""

    linked: list[DatasetParentLink] = field(default_factory=list)
    """Every link written, in the order they were written"""

    terminals: list[ParentSimulation] = field(default_factory=list)
    """
    Where each chain stopped

    Either the target experiment was reached, or a root of the tree was (which is a
    [ParentNotAncestor][(m).ParentNotAncestor] as well, when a target was being looked
    for).
    """

    variable_gaps: list[VariableGap] = field(default_factory=list)
    """Every variable a found ancestor did not publish. See [VariableGap][(m).VariableGap]."""  # noqa: E501

    ambiguous: list[AmbiguousParent] = field(default_factory=list)
    """
    Every parent *dataset* the matcher could not choose

    See [AmbiguousParent][(m).AmbiguousParent]. Not a failure: the walk carries on and
    simply does not climb through an ambiguous link.
    """

    ambiguous_versions: list[AmbiguousParentVersion] = field(default_factory=list)
    """
    Every parent *version* an injected chooser declined to pick

    See [AmbiguousParentVersion][(m).AmbiguousParentVersion]. Not a failure either, and
    not climbed through for the same reason. Empty unless a chooser was injected.
    """

    version_decisions: list[ParentVersionDecision] = field(default_factory=list)
    """
    Every parent release our own rules picked, out of several they were given
    """

    searched: list[ParentSimulation] = field(default_factory=list)
    """
    Every simulation a search was actually fired for

    One entry per request, so `len(searched)` against the number of children walked is
    how much the deduplication saved.
    """

    hops: int = 0
    """How many times the walk climbed"""


def plan_parent_searches(
    session: Session,
    dataset_version_ids: Collection[int] | None = None,
    *,
    extra_variables: Collection[str] = (),
) -> tuple[ParentSearchPlanItem, ...]:
    """
    Work out which parent simulations still need searching for, and for what

    Reads the assertions which are
    [UNRESOLVED][esmporium.db.schema.ParentResolution.UNRESOLVED] and groups them by the
    simulation they name, so that many children of one control run become one search.

    Parameters
    ----------
    session
        The session to read from

    dataset_version_ids
        Which versions' parents to plan for. `None`, the default, plans for every
        unresolved assertion in the database.

    extra_variables
        Variables to ask every parent for on top of the ones its children need

        Widening, for the case where more of a control run is wanted than the children
        happen to use.

    Returns
    -------
    :
        One item per distinct parent simulation, sorted by that simulation, so the
        order a walk fires its searches in is stable and reproducible.

    Raises
    ------
    IncompleteParentError
        A row declares a parent but does not name a project, model, experiment and
        variant.

    CrossProjectParentError
        A row declares a parent in a different project from the dataset which
        declared it.
    """
    statement = (
        select(DatasetVersionParent, Dataset)
        .join(
            DatasetVersion,
            col(DatasetVersion.id) == col(DatasetVersionParent.dataset_version_id),
        )
        .join(Dataset, col(Dataset.id) == col(DatasetVersion.dataset_id))
        .where(DatasetVersionParent.resolution == ParentResolution.UNRESOLVED)
    )
    if dataset_version_ids is not None:
        if not dataset_version_ids:
            return ()

        statement = statement.where(
            col(DatasetVersionParent.dataset_version_id).in_(tuple(dataset_version_ids))
        )

    wanted: dict[ParentSimulation, set[str]] = {}
    children: dict[ParentSimulation, set[int]] = {}
    for parent_info, child in session.exec(statement).all():
        parent = parent_simulation(parent_info, child.project)
        wanted.setdefault(parent, set()).update(extra_variables)
        wanted[parent].add(child.variable)
        children.setdefault(parent, set()).add(parent_info.dataset_version_id)

    return tuple(
        ParentSearchPlanItem(
            parent=parent,
            variables=tuple(sorted(wanted[parent])),
            child_dataset_version_ids=tuple(sorted(children[parent])),
        )
        for parent in sorted(wanted)
    )


def parent_simulation(
    parent_info: DatasetVersionParent, project: str
) -> ParentSimulation:
    """
    Read the simulation a row declares, refusing one we could not search for

    Parameters
    ----------
    parent_info
        The assertion row to read

    project
        The project of the dataset which declared it, which the parent's declared
        project has to agree with

    Returns
    -------
    :
        The simulation the row names

    Raises
    ------
    IncompleteParentError
        The row does not name all four of project, model, experiment and variant.

    CrossProjectParentError
        The declared project is not `project`.
    """
    if (
        parent_info.parent_project is None
        or parent_info.parent_model is None
        or parent_info.parent_experiment is None
        or parent_info.parent_variant_label is None
    ):
        raise IncompleteParentError(parent_info)

    # TODO: this equality is what would have to go if a caller ever has facades for
    # both projects. Three things would need settling, and only the first is here:
    # `find_parents` takes one `selector` and uses it for every search, so a parent in
    # another project would need one that covers that project too; a published MIP era
    # would need mapping to a project, which is the identity for CMIP6Plus -> CMIP6 but
    # not in general; and the opposite mistake -- a record asserting its own era for a
    # parent published under another -- is invisible here, and surfaces as a parent the
    # index has nothing for. `candidate_parent_datasets` and `matcher_for_project`
    # already work off whatever project the parent names, so neither would change.
    if parent_info.parent_project != project:
        raise CrossProjectParentError(parent_info, project)

    return ParentSimulation(
        project=parent_info.parent_project,
        model=parent_info.parent_model,
        experiment=parent_info.parent_experiment,
        variant_label=parent_info.parent_variant_label,
    )


def resolve_parent_links(
    session: Session,
    dataset_version_ids: Collection[int] | None = None,
    *,
    matchers: Mapping[str, ParentMatcher] = PARENT_MATCHERS_BY_PROJECT,
    version_chooser: ParentVersionChooser = DEFAULT_PARENT_VERSION_CHOOSER,
) -> ParentResolutionResult:
    """
    Match every declared parent simulation against what we hold, and link them

    Parameters
    ----------
    session
        The session to read and write with

    dataset_version_ids
        Which versions' parents to resolve. `None`, the default, tries every
        unresolved assertion in the database.

    matchers
        Which parent-dataset matcher to use per project. Defaults to the pre-built
        ones; pass a mapping of your own to override one, e.g.
        `{**PARENT_MATCHERS_BY_PROJECT, "CMIP6": my_matcher}`. See
        [esmporium.db.parent_matching][].

    version_chooser
        How to pick which version of the matched parent dataset to use.

        Defaults to [choose_parent_version][esmporium.db.choose_parent_version].
        Pass one of your own to decide it
        differently; nothing published can decide it, so a rule is all that either of
        you has.

    Returns
    -------
    :
        What was linked, what is still waiting, which variables an ancestor we do hold
        turned out not to publish, and which parent releases were picked by us rather
        than read.

    Raises
    ------
    IncompleteParentError
        A row declares a parent but does not name enough of it.

    CrossProjectParentError
        A row declares a parent in another project.

    UnconfiguredSQLiteEngineError
        `session` is bound to a SQLite engine that has not been configured with
        [esmporium.db.configure_sqlite_for_concurrency][].
    """
    check_engine_is_configured(session)

    statement = (
        select(DatasetVersionParent, DatasetVersion, Dataset)
        .join(
            DatasetVersion,
            col(DatasetVersion.id) == col(DatasetVersionParent.dataset_version_id),
        )
        .join(Dataset, col(Dataset.id) == col(DatasetVersion.dataset_id))
        .where(DatasetVersionParent.resolution == ParentResolution.UNRESOLVED)
        .order_by(col(DatasetVersionParent.id))
    )
    if dataset_version_ids is not None:
        if not dataset_version_ids:
            return ParentResolutionResult()

        statement = statement.where(
            col(DatasetVersionParent.dataset_version_id).in_(tuple(dataset_version_ids))
        )

    linked: list[DatasetParentLink] = []
    variable_gaps: list[VariableGap] = []
    ambiguous: list[AmbiguousParent] = []
    ambiguous_versions: list[AmbiguousParentVersion] = []
    version_decisions: list[ParentVersionDecision] = []
    still_unresolved: list[int] = []

    # Grouped by child version and run through precedence, so a version described by
    # two sources is resolved once, from the one we believe. One dict per thing being
    # collected rather than one dict of tuples, so each says what it holds.
    assertions: defaultdict[int, list[DatasetVersionParent]] = defaultdict(list)
    child_versions: dict[int, DatasetVersion] = {}
    child_datasets: dict[int, Dataset] = {}
    for parent_info, child_version, child_dataset in session.exec(statement).all():
        version_id = parent_info.dataset_version_id
        assertions[version_id].append(parent_info)
        # Both are joined off `version_id`, so every assertion of one version brings
        # the same two rows back and re-assigning them changes nothing.
        child_versions[version_id] = child_version
        child_datasets[version_id] = child_dataset

    for version_id, held in sorted(assertions.items()):
        preferred = preferred_parent_info(held)
        if preferred is None:  # pragma: no cover - a version with rows has a winner
            continue

        resolved = _resolve_one_assertion(
            session,
            preferred,
            child_version=child_versions[version_id],
            child=child_datasets[version_id],
            matchers=matchers,
            version_chooser=version_chooser,
        )
        if resolved.link is not None:
            linked.append(resolved.link)
        if resolved.variable_gap is not None:
            variable_gaps.append(resolved.variable_gap)
        if resolved.ambiguous is not None:
            ambiguous.append(resolved.ambiguous)
        if resolved.ambiguous_version is not None:
            ambiguous_versions.append(resolved.ambiguous_version)
        if resolved.version_decision is not None:
            version_decisions.append(resolved.version_decision)
        if resolved.still_unresolved:
            still_unresolved.append(flushed_id(preferred))

    return ParentResolutionResult(
        linked=tuple(linked),
        variable_gaps=tuple(variable_gaps),
        ambiguous=tuple(ambiguous),
        ambiguous_versions=tuple(ambiguous_versions),
        version_decisions=tuple(version_decisions),
        still_unresolved=tuple(sorted(still_unresolved)),
    )


def candidate_parent_datasets(
    session: Session, parent: ParentSimulation
) -> tuple[Dataset, ...]:
    """
    Find every dataset we hold of one parent simulation

    The **query**, with no policy in it: which of these is a given child's parent is
    [esmporium.db.parent_matching][]'s decision, not this function's. Splitting them is
    what lets the same rule choose between datasets a search just returned and datasets
    that were already stored, instead of one path being a `WHERE` clause and the other a
    function that can drift from it.

    Parameters
    ----------
    session
        The session to read from

    parent
        The simulation to find the datasets of

    Returns
    -------
    :
        Every dataset of that simulation, ordered by id so the answer is stable

        Empty when we hold none, which is what a search is for.
    """
    return tuple(
        session.exec(
            select(Dataset)
            .where(
                Dataset.project == parent.project,
                Dataset.model == parent.model,
                Dataset.experiment == parent.experiment,
                Dataset.variant_label == parent.variant_label,
            )
            .order_by(col(Dataset.id))
        ).all()
    )


@dataclass(frozen=True)
class _ResolvedChild:
    """
    What resolving one child version's declared parent produced
    """

    link: DatasetParentLink | None = None
    """The link written, when the parent resolved to one stored version"""

    variable_gap: VariableGap | None = None
    """The gap to report, when the parent simulation does not publish the variable"""

    ambiguous: AmbiguousParent | None = None
    """The ambiguity to report, when several datasets of the simulation fitted"""

    ambiguous_version: AmbiguousParentVersion | None = None
    """The ambiguity to report, when a chooser declined to pick between versions"""

    version_decision: ParentVersionDecision | None = None
    """
    The decision to report, when several releases of the parent dataset were held

    Set *alongside* `link`, unlike the other three: the parent resolved and the chain
    climbs, and this says the release it climbed through was picked by us.
    """

    still_unresolved: bool = False
    """Whether the assertion stays in the queue for a later search or chooser"""


def _resolve_one_assertion(  # noqa: PLR0913 - one argument per thing the two decisions need
    session: Session,
    parent_info: DatasetVersionParent,
    *,
    child_version: DatasetVersion,
    child: Dataset,
    matchers: Mapping[str, ParentMatcher],
    version_chooser: ParentVersionChooser,
) -> _ResolvedChild:
    """
    Turn one version's declared parent into one stored version, or say why not
    """
    parent = parent_simulation(parent_info, child.project)
    matcher = matcher_for_project(child.project, matchers)
    outcome = matcher(child, candidate_parent_datasets(session, parent))
    if outcome.is_ambiguous:
        # Several datasets fit equally well. Reported and left unresolved rather than
        # settled by picking one, because picking would be a guess that no reader could
        # later tell from a real answer.
        return _ResolvedChild(
            still_unresolved=True,
            ambiguous=AmbiguousParent(
                child_dataset_version_id=parent_info.dataset_version_id,
                parent=parent,
                candidate_dataset_ids=tuple(
                    flushed_id(one) for one in outcome.candidates
                ),
                reason=outcome.reason or "the matcher could not choose",
            ),
        )

    parent_dataset = outcome.parent_dataset
    if parent_dataset is None:
        gap = VariableGap(
            child_dataset_version_id=parent_info.dataset_version_id,
            parent=parent,
            variable=child.variable,
        )
        if _variable_known_unpublished(session, parent, child.variable):
            # Already settled for a sibling: that this simulation does not publish this
            # variable is a fact about the simulation, so a new dataset of the same
            # variable inherits it instead of firing the same fruitless search again.
            # This is the cross-dataset reuse, applied to the gaps.
            parent_info.resolution = ParentResolution.VARIABLE_NOT_PUBLISHED
            parent_info.resolved_at = _utcnow()
            session.add(parent_info)
            session.flush()
            return _ResolvedChild(variable_gap=gap)

        return _ResolvedChild(
            still_unresolved=True,
            variable_gap=gap if _parent_simulation_is_held(session, parent) else None,
        )

    # The dataset is settled; which version of it was branched from is the second
    # question, and the one nothing published answers.
    parent_dataset_id = flushed_id(parent_dataset)
    version_outcome = version_chooser(
        child_version, candidate_parent_versions(session, parent_dataset_id)
    )
    parent_version = version_outcome.parent_version
    if parent_version is None:
        # Either several versions fit (the ordinary case: nothing published says which)
        # or we hold none at all, which ingestion cannot produce but is not worth
        # guessing past either. Left unresolved so injecting a chooser is all it takes.
        if not version_outcome.is_ambiguous:
            return _ResolvedChild(still_unresolved=True)

        return _ResolvedChild(
            still_unresolved=True,
            ambiguous_version=AmbiguousParentVersion(
                child_dataset_version_id=parent_info.dataset_version_id,
                parent=parent,
                parent_dataset_id=parent_dataset_id,
                candidate_dataset_version_ids=tuple(
                    flushed_id(one) for one in version_outcome.candidates
                ),
                reason=version_outcome.reason
                or "the chooser could not choose a version",
            ),
        )

    link = DatasetParentLink(
        parent_info_id=flushed_id(parent_info),
        child_dataset_version_id=parent_info.dataset_version_id,
        parent_dataset_id=parent_dataset_id,
        parent_dataset_version_id=flushed_id(parent_version),
        version_choice=version_outcome.choice,
    )
    session.add(link)
    parent_info.resolution = ParentResolution.RESOLVED
    parent_info.resolved_at = _utcnow()
    session.add(parent_info)
    session.flush()

    decision = None
    if version_outcome.choice in _DECIDED_VERSION_CHOICES:
        # Linked and climbable, and still worth saying: more than one release of the
        # right dataset was held, so which one this points at is ours, not theirs.
        decision = ParentVersionDecision(
            child_dataset_version_id=parent_info.dataset_version_id,
            parent=parent,
            parent_dataset_id=parent_dataset_id,
            chosen_dataset_version_id=flushed_id(parent_version),
            choice=version_outcome.choice,
            passed_over_dataset_version_ids=tuple(
                flushed_id(one) for one in version_outcome.passed_over
            ),
        )

    return _ResolvedChild(link=link, version_decision=decision)


def candidate_parent_versions(
    session: Session, parent_dataset_id: int
) -> tuple[DatasetVersion, ...]:
    """
    Find every version we hold of one already-matched parent dataset

    Parameters
    ----------
    session
        The session to read from

    parent_dataset_id
        The parent dataset to find the versions of, as its
        [Dataset.id][esmporium.db.schema.Dataset]

    Returns
    -------
    :
        Every version of that dataset, ordered by id so the answer is stable

        Empty only if we hold the dataset and none of its versions, which ingestion
        cannot produce.
    """
    return tuple(
        session.exec(
            select(DatasetVersion)
            .where(DatasetVersion.dataset_id == parent_dataset_id)
            .order_by(col(DatasetVersion.id))
        ).all()
    )


def _variable_known_unpublished(
    session: Session, parent: ParentSimulation, variable: str
) -> bool:
    """
    Whether a sibling has already established this simulation lacks this variable
    """
    return (
        session.exec(
            select(DatasetVersionParent)
            .join(
                DatasetVersion,
                col(DatasetVersion.id) == col(DatasetVersionParent.dataset_version_id),
            )
            .join(Dataset, col(Dataset.id) == col(DatasetVersion.dataset_id))
            .where(
                DatasetVersionParent.resolution
                == ParentResolution.VARIABLE_NOT_PUBLISHED,
                DatasetVersionParent.parent_project == parent.project,
                DatasetVersionParent.parent_model == parent.model,
                DatasetVersionParent.parent_experiment == parent.experiment,
                DatasetVersionParent.parent_variant_label == parent.variant_label,
                Dataset.variable == variable,
            )
        ).first()
        is not None
    )


def _parent_simulation_is_held(session: Session, parent: ParentSimulation) -> bool:
    """
    Whether we hold any dataset of `parent` at all, whatever its variable
    """
    return bool(candidate_parent_datasets(session, parent))


def check_until_experiments_exist(  # noqa: PLR0913 - the keyword-only extras are deliberate injection seams
    until_experiment: Sequence[str],
    project: str,
    selector: SearchAPIFacadeSelector = DEFAULT_SEARCH_API_SELECTOR,
    *,
    client: httpx.Client | None = None,
    close_matches: CloseMatcher = close_matches_difflib,
    api_call_observer: SearchAPICallObserver | None = None,
) -> None:
    """
    Check the experiments a walk will stop at are ones the API publishes

    Run before anything is searched for. A misspelled target is the one failure which
    looks like an ordinary negative result: the walk never matches it, climbs every
    chain to its top and reports that none of them got there. One request up front
    turns that into a typo report.

    Parameters
    ----------
    until_experiment
        The experiments to look for

    project
        The project whose API publishes them

    selector
        Chooses which facade to ask. See
        [check_query_values_single_project][esmporium.search.check_query_values_single_project].

    client
        The HTTP client to ask with. If `None`, one is built for the call.

    close_matches
        How to suggest alternatives for a value that is not published.

    api_call_observer
        Told about each request made. See [esmporium.search.health][].

    Raises
    ------
    UnknownUntilExperimentError
        One of `until_experiment` is not a value the API publishes, and nothing is
        wrong with the shape of it either.
    """
    if not until_experiment:
        return

    # Rendered into the project's own spelling first, for the same reason
    # `ParentSearchPlanItem.to_query` is: the checker canonicalises what it is handed,
    # and a `QueryCanonical` cannot be canonicalised.
    rendered = translate_canonical_to_projects(
        QueryCanonical(project=(project,), experiment=tuple(until_experiment))
    )
    outcome = check_query_values_single_project(
        rendered[project],
        selector,
        client=client,
        close_matches=close_matches,
        api_call_observer=api_call_observer,
    )
    for report in outcome.reports.values():
        for finding in report.findings:
            # MALFORMED means the API describes this facet with a pattern rather than
            # a list of values, so it can judge shape but not existence -- which is no
            # evidence that the experiment is missing. CASE, TYPO and UNKNOWN all are.
            if finding.facet != "experiment" or finding.kind == FindingKind.MALFORMED:
                continue

            raise UnknownUntilExperimentError(finding.value, finding.suggestions)


def find_parents(  # noqa: PLR0913 - one argument per thing a walk has to be told
    session: Session,
    dataset_version_ids: Collection[int] | None = None,
    *,
    until_experiment: Sequence[str],
    extra_variables: Collection[str] = (),
    selector: SearchAPIFacadeSelector = DEFAULT_SEARCH_API_SELECTOR,
    check_until_exists: bool = True,
    max_hops: int = 16,
    client: httpx.Client | None = None,
    api_call_observer: SearchAPICallObserver | None = None,
    searcher: ParentSearcher | None = None,
    facts_provider: ParentFactsProvider | None = None,
    matchers: Mapping[str, ParentMatcher] = PARENT_MATCHERS_BY_PROJECT,
    version_chooser: ParentVersionChooser = DEFAULT_PARENT_VERSION_CHOOSER,
) -> ParentWalkResult:
    """
    Walk back up the parent chain of some datasets, searching for what is missing

    Each turn of the loop resolves what the database can answer for nothing, searches
    once per parent simulation still missing, and resolves again. A chain stops when it
    reaches one of `until_experiment`; a chain which reaches a record declaring no
    parent without having passed through one of them is a failure, as is a declared
    parent the index has nothing for.

    Parameters
    ----------
    session
        The session to read and write with

    dataset_version_ids
        Which versions to start from. `None`, the default, starts from every unresolved
        assertion in the database.

    extra_variables
        Variables to ask every ancestor for beyond the ones its children need. See
        [ParentSearchPlanItem.variables][(m).ParentSearchPlanItem.variables].

    until_experiment
        The experiments to walk back to, e.g. `("piControl", "esm-piControl")`

        **Required, with no default.** "Walk to the top" is not a usable default: CMIP7
        `piControl` declares a parent of `piControl-spinup`, which nobody publishes, so
        a walk with no target would end in a
        [ParentNotFound][(m).ParentNotFound] on every chain. Where a lineage should stop
        is a question about the science, which is why it is named by experiment rather
        than counted in branches.

        Pass `()` to walk until each chain declares no parent, accepting that for CMIP7
        that currently fails.

    selector
        Chooses which facade each search uses. See
        [esmporium.search.search_single_project][].

    check_until_exists
        Whether to confirm `until_experiment` exists before searching for anything

        `True` by default, because a misspelled target is indistinguishable from a
        lineage which does not reach it. Pass `False` to skip the one request it costs,
        e.g. when offline or when the values are already known good.

    max_hops
        How many times to climb before giving up

        A guard, not the stopping rule -- `until_experiment` is (see this module's
        docstring). This is insurance against a chain which never ends, and the default
        is set well above any real lineage so that reaching it means something is wrong
        rather than that a chain was simply long.

    client
        The HTTP client to search with, shared across every hop. If `None`, one is
        built for the call and closed at the end.

    api_call_observer
        Told about each search request. See [esmporium.search.health][] for how to
        build one that records them.

    searcher
        How to find a planned parent's datasets. `None`, the default, really searches.
        See [ParentSearcher][(m).ParentSearcher].

    facts_provider
        How to obtain parent facts for a dataset which has none, called once per hop.

        `None`, the default, obtains nothing, which is right for CMIP7 -- its facts
        arrive with the search -- and raises
        [NoParentFactsProviderError][(m).NoParentFactsProviderError] for any dataset
        that needs facts and has none. See
        [ParentFactsProvider][(m).ParentFactsProvider].

    matchers
        Which parent-dataset matcher to use per project, passed through to
        [resolve_parent_links][(m).resolve_parent_links].

    version_chooser
        How to pick which version of a matched parent dataset to use, passed through to
        [resolve_parent_links][(m).resolve_parent_links].

    Returns
    -------
    :
        What was linked, where each chain stopped, which parent releases were picked by
        us, anything neither matching step could choose, which searches were fired and
        how many times the walk climbed.

    Raises
    ------
    UnknownUntilExperimentError
        `until_experiment` names an experiment the API does not publish. Raised before
        anything is searched for.

    NoParentFactsProviderError
        A dataset's parent facts are not recorded and no `facts_provider` was given to
        obtain them.

    ParentResolutionError
        One or more chains could not be walked back to the target. Raised once, after
        the last commit, with every failure on it.

    TooManyParentHopsError
        The walk climbed `max_hops` times and was still finding new ancestors.

    IncompleteParentError, CrossProjectParentError
        A declared parent that cannot be searched for. See those errors.

    UnconfiguredSQLiteEngineError
        `session` is bound to a SQLite engine that has not been configured with
        [esmporium.db.configure_sqlite_for_concurrency][].

    Warns
    -----
    ParentVersionDecidedWarning
        At least one parent is published more than once, so which release to use was
        picked by a rule rather than read. Raised once for the whole walk, however many
        links it covers.
    """
    check_engine_is_configured(session)

    owns_client = client is None
    client = client if client is not None else httpx.Client(follow_redirects=True)
    try:

        def search(session: Session, item: ParentSearchPlanItem) -> None:
            """Really search, which is what a walk does unless told otherwise."""
            _search_for_parent(
                session,
                item,
                selector=selector,
                client=client,
                api_call_observer=api_call_observer,
            )

        return _walk(
            session,
            dataset_version_ids,
            until_experiment=tuple(until_experiment),
            extra_variables=extra_variables,
            selector=selector,
            check_until_exists=check_until_exists,
            max_hops=max_hops,
            client=client,
            api_call_observer=api_call_observer,
            searcher=search if searcher is None else searcher,
            facts_provider=facts_provider,
            matchers=matchers,
            version_chooser=version_chooser,
        )
    finally:
        if owns_client:
            client.close()


class TooManyParentHopsError(RuntimeError):
    """
    Raised when a walk climbed its hop limit and was still finding new ancestors
    """

    def __init__(self, max_hops: int, outstanding: Sequence[ParentSimulation]) -> None:
        self.max_hops = max_hops
        """The limit that was reached"""

        self.outstanding = list(outstanding)
        """The ancestors still waiting to be climbed to when the walk stopped"""

        body = "\n".join(f"  - {parent}" for parent in self.outstanding)
        super().__init__(
            f"Climbed {max_hops} parent hops and there are still "
            f"{len(self.outstanding)} ancestor(s) to go:\n{body}\n"
            f"A real lineage is not this deep and a cycle cannot get here, because a "
            f"simulation is never revisited, so this is published metadata describing "
            f"a chain that does not end. Everything found so far has been saved. "
            f"Raise max_hops if the lineage really is this long."
        )


@dataclass(frozen=True)
class _Classified:
    """
    One turn's frontier, sorted into what to climb and what has stopped

    A frontier is a set of dataset version ids. Each one's assertion puts it in exactly
    one of these: it is where the walk was looking for, it declares no parent, or it
    has a parent to go and get.
    """

    reached_target: tuple[int, ...] = ()
    """Versions whose own experiment is one the walk was walking back to"""

    tops: tuple[int, ...] = ()
    """
    Versions which declare no parent, so their chain ends here

    A [ParentNotAncestor][(m).ParentNotAncestor] when a target was being looked for,
    and an ordinary terminal when one was not.
    """

    broken: tuple[tuple[int, ParentSimulation], ...] = ()
    """
    Versions whose declared parent we have already searched for and not found

    Each with the parent it named. Terminal, so no search is fired again, but still a
    [ParentNotFound][(m).ParentNotFound] -- the recorded state stops the request being
    repeated, not the chain from being broken.
    """

    to_climb: tuple[int, ...] = ()
    """Versions which declare a parent that is not a target, so the walk goes on"""


SOURCE_PRECEDENCE: tuple[ParentInfoSource, ...] = (
    ParentInfoSource.FIX,
    ParentInfoSource.FILE_HEADER,
    ParentInfoSource.SEARCH_METADATA,
)
"""
Which source to believe first, most trusted first

A version can carry one row per source
(see [DatasetVersionParent][esmporium.db.schema.DatasetVersionParent]), and they can
disagree -- which is the point of holding them separately. This is the order a reader
resolves that with:

- a **fix** wins, because it exists only when someone decided the published value is
  wrong, and an ingest that could overwrite it would make it pointless;
- a **file header** beats the index, because the header is what the data itself says;
- the **index** is the fallback, and is all CMIP7 has.

If a project ever needs a different order, this is the thing to pass round rather
than a new column.
"""


def preferred_parent_info(
    rows: Sequence[DatasetVersionParent],
    precedence: Sequence[ParentInfoSource] = SOURCE_PRECEDENCE,
) -> DatasetVersionParent | None:
    """
    Pick the one answer to believe, out of a version's rows

    Parameters
    ----------
    rows
        The rows held for one dataset version, in any order

    precedence
        Which source to believe first. See [SOURCE_PRECEDENCE][(m).SOURCE_PRECEDENCE].

    Returns
    -------
    :
        The row to believe, or `None` if `rows` is empty

        A row whose source is not in `precedence` at all is considered last, so an
        unknown source is used rather than dropped.
    """
    if not rows:
        return None

    order = {source: rank for rank, source in enumerate(precedence)}

    return min(rows, key=lambda row: (order.get(row.source, len(order)), row.id or 0))


def ensure_parent_facts(
    session: Session,
    dataset_version_ids: Collection[int],
    facts_provider: ParentFactsProvider | None,
) -> None:
    """
    See that every one of these versions has parent facts, or refuse to carry on

    The per-hop step. Run on the versions a hop has just brought in, so that an
    ancestor is treated exactly as the datasets the walk started from were.

    Parameters
    ----------
    session
        The session to read and write with

    dataset_version_ids
        The versions to see to

    facts_provider
        How to obtain facts for a version which has none, or `None` to obtain nothing

    Raises
    ------
    NoParentFactsProviderError
        A version still has no facts once the provider has run, or had none and no
        provider was given. Counted per project, because which project a dataset belongs
        to is what decides where its facts should have come from.
    """
    if not dataset_version_ids:
        return

    if facts_provider is not None:
        facts_provider(session, dataset_version_ids)
        session.flush()

    missing = _projects_of_versions_without_facts(session, dataset_version_ids)
    if missing:
        raise NoParentFactsProviderError(missing)


def _projects_of_versions_without_facts(
    session: Session, dataset_version_ids: Collection[int]
) -> dict[str, int]:
    """Count the versions here which carry no parent assertion, by project."""
    with_facts = set(
        session.exec(
            select(DatasetVersionParent.dataset_version_id).where(
                col(DatasetVersionParent.dataset_version_id).in_(
                    tuple(dataset_version_ids)
                )
            )
        ).all()
    )
    without = tuple(set(dataset_version_ids) - with_facts)
    if not without:
        return {}

    rows = session.exec(
        select(Dataset.project)
        .join(DatasetVersion, col(DatasetVersion.dataset_id) == col(Dataset.id))
        .where(col(DatasetVersion.id).in_(without))
    ).all()

    counts: dict[str, int] = {}
    for project in rows:
        counts[project] = counts.get(project, 0) + 1

    return counts


def _classify_frontier(
    session: Session,
    frontier: Collection[int],
    until_experiment: tuple[str, ...],
) -> _Classified:
    """
    Sort a frontier into what the walk should do with each of its versions
    """
    if not frontier:  # pragma: no cover - the walk loop only runs on a full frontier
        return _Classified()

    datasets = {
        flushed_id(version): dataset
        for version, dataset in session.exec(
            select(DatasetVersion, Dataset)
            .join(Dataset, col(Dataset.id) == col(DatasetVersion.dataset_id))
            .where(col(DatasetVersion.id).in_(tuple(frontier)))
        ).all()
    }
    held: dict[int, list[DatasetVersionParent]] = {}
    for parent_info in session.exec(
        select(DatasetVersionParent).where(
            col(DatasetVersionParent.dataset_version_id).in_(tuple(frontier))
        )
    ).all():
        held.setdefault(parent_info.dataset_version_id, []).append(parent_info)

    reached_target: list[int] = []
    tops: list[int] = []
    broken: list[tuple[int, ParentSimulation]] = []
    to_climb: list[int] = []
    for version_id, dataset in sorted(datasets.items()):
        if dataset.experiment in until_experiment:
            reached_target.append(version_id)
            continue

        preferred = preferred_parent_info(held.get(version_id, []))
        if preferred is None:  # pragma: no cover - ensure_parent_facts refused first
            continue

        parent_info = preferred
        if parent_info.resolution == ParentResolution.NO_PARENT_DECLARED:
            tops.append(version_id)
        elif parent_info.resolution == ParentResolution.NOT_ON_ESGF:
            # Terminal, but still a broken chain every time it is walked. Recorded so
            # a later run says so again rather than quietly succeeding: the row is
            # what stops the *search* being repeated, not what makes the failure
            # go away.
            broken.append((version_id, parent_simulation(parent_info, dataset.project)))
        elif parent_info.resolution == ParentResolution.VARIABLE_NOT_PUBLISHED:
            # Not climbable and not a failure: see that state's docstring.
            pass
        else:
            to_climb.append(version_id)

    return _Classified(
        reached_target=tuple(reached_target),
        tops=tuple(tops),
        broken=tuple(broken),
        to_climb=tuple(to_climb),
    )


def _simulation_of(session: Session, dataset_version_id: int) -> ParentSimulation:
    """
    Describe a stored version as a simulation, so it can be named in a failure

    The same four facets a declared parent carries, read off the dataset instead, so
    that a message about a chain names both ends of it the same way.
    """
    dataset = session.exec(
        select(Dataset)
        .join(DatasetVersion, col(DatasetVersion.dataset_id) == col(Dataset.id))
        .where(DatasetVersion.id == dataset_version_id)
    ).one()

    return ParentSimulation(
        project=dataset.project,
        model=dataset.model,
        experiment=dataset.experiment,
        variant_label=dataset.variant_label,
    )


def _simulations_of(
    session: Session, dataset_version_ids: Collection[int]
) -> tuple[ParentSimulation, ...]:
    """Describe some stored versions as the distinct simulations they belong to."""
    return tuple(
        sorted(
            {_simulation_of(session, version_id) for version_id in dataset_version_ids}
        )
    )


def _versions_of_parents(links: Sequence[DatasetParentLink]) -> tuple[int, ...]:
    """
    Read the parent versions some links point at, which is the walk's next frontier
    """
    return tuple(sorted({link.parent_dataset_version_id for link in links}))


def _search_for_parent(
    session: Session,
    item: ParentSearchPlanItem,
    *,
    selector: SearchAPIFacadeSelector,
    client: httpx.Client,
    api_call_observer: SearchAPICallObserver | None,
) -> None:
    """
    Search for one parent simulation and ingest what comes back

    **The request is narrowed to the variables wanted**, which
    [ParentSearchPlanItem.variables][(m).ParentSearchPlanItem.variables] carries: the
    union of what the children declaring this parent need, not everything the simulation
    published.
    """
    search_single_project(
        item.to_canonical_query(),
        selector,
        client=client,
        api_call_observer=api_call_observer,
        processor=build_result_processor(session),
        build_request=build_parent_dataset_request,
    )


def _record_search_outcomes(
    session: Session,
    plan: Sequence[ParentSearchPlanItem],
    failures: list[ParentNotFound | ParentNotAncestor],
) -> None:
    """
    Record what each hop's searches settled, so nothing is asked for twice
    """
    for item in plan:
        simulation_is_held = _parent_simulation_is_held(session, item.parent)
        resolution = (
            ParentResolution.VARIABLE_NOT_PUBLISHED
            if simulation_is_held
            else ParentResolution.NOT_ON_ESGF
        )
        for version_id in item.child_dataset_version_ids:
            # Through precedence, not `.first()`: with several sources, which row is
            # settled must be the one that was acted on.
            parent_info = preferred_parent_info(
                tuple(
                    session.exec(
                        select(DatasetVersionParent).where(
                            DatasetVersionParent.dataset_version_id == version_id,
                            DatasetVersionParent.resolution
                            == ParentResolution.UNRESOLVED,
                        )
                    ).all()
                )
            )
            if parent_info is None:
                continue

            parent_info.resolution = resolution
            parent_info.resolved_at = _utcnow()
            session.add(parent_info)
            if not simulation_is_held:
                failures.append(
                    ParentNotFound(
                        child=_simulation_of(session, version_id), parent=item.parent
                    )
                )

    session.flush()


def _projects_to_check(
    session: Session, dataset_version_ids: Collection[int] | None
) -> tuple[str, ...]:
    """
    Find the distinct projects a walk will be searching in

    One gate request per project, because which experiments exist is a question about a
    project's API rather than about the walk.
    """
    statement = (
        select(Dataset.project)
        .join(DatasetVersion, col(DatasetVersion.dataset_id) == col(Dataset.id))
        .join(
            DatasetVersionParent,
            col(DatasetVersionParent.dataset_version_id) == col(DatasetVersion.id),
        )
        .distinct()
    )
    if dataset_version_ids is not None:
        statement = statement.where(
            col(DatasetVersionParent.dataset_version_id).in_(tuple(dataset_version_ids))
        )

    return tuple(sorted(session.exec(statement).all()))


def _starting_frontier(
    session: Session, dataset_version_ids: Collection[int] | None
) -> tuple[int, ...]:
    """
    Work out which versions a walk begins from

    When the caller names versions, **those are the frontier**, whether or not they
    carry parent facts yet. That is what lets a walk start on a project whose facts have
    to be fetched: a CMIP6 dataset has no assertion until a header has been read, and a
    frontier built only from existing rows would never include it, so the walk would
    quietly do nothing.

    `None` means every version we hold an assertion for, which is the "catch up on
    everything" request and cannot demand facts of datasets nobody asked about. Rows
    which are already terminal are included: a walk which starts on a record declaring
    no parent has still failed to reach the target, and leaving them out would make that
    silent.
    """
    if dataset_version_ids is not None:
        return tuple(sorted(set(dataset_version_ids)))

    return tuple(
        sorted(
            session.exec(
                select(DatasetVersionParent.dataset_version_id).distinct()
            ).all()
        )
    )


def _frontier_needing_facts(
    session: Session, frontier: Collection[int], until_experiment: tuple[str, ...]
) -> tuple[int, ...]:
    """
    Narrow a frontier to the versions whose parent facts the walk actually needs
    """
    if not frontier:  # pragma: no cover - the walk loop only runs on a full frontier
        return ()

    rows = session.exec(
        select(DatasetVersion.id, Dataset.experiment)
        .join(Dataset, col(Dataset.id) == col(DatasetVersion.dataset_id))
        .where(col(DatasetVersion.id).in_(tuple(frontier)))
    ).all()

    return tuple(
        sorted(
            version_id
            for version_id, experiment in rows
            if experiment not in until_experiment
        )
    )


def _walk(  # noqa: PLR0913, PLR0912, PLR0915 - every argument is one `find_parents` was given, every branch is one state a frontier can be in, and every statement is one step of a turn
    session: Session,
    dataset_version_ids: Collection[int] | None,
    *,
    until_experiment: tuple[str, ...],
    extra_variables: Collection[str],
    selector: SearchAPIFacadeSelector,
    check_until_exists: bool,
    max_hops: int,
    client: httpx.Client,
    api_call_observer: SearchAPICallObserver | None,
    searcher: ParentSearcher,
    facts_provider: ParentFactsProvider | None,
    matchers: Mapping[str, ParentMatcher],
    version_chooser: ParentVersionChooser,
) -> ParentWalkResult:
    """
    Do the climbing, with the client already open

    Split out from [find_parents][(m).find_parents] so the client is closed on every
    path out, including the failures, without the walk itself sitting inside a `try`.
    """
    result = ParentWalkResult()
    failures: list[ParentNotFound | ParentNotAncestor] = []
    searched: set[ParentSimulation] = set()
    # Resolving runs twice per turn -- once before the searches and once after -- so
    # anything found by the first pass would otherwise be reported again by the second.
    seen_gaps: set[VariableGap] = set()
    seen_ambiguous: set[AmbiguousParent] = set()
    seen_ambiguous_versions: set[AmbiguousParentVersion] = set()
    seen_version_decisions: set[ParentVersionDecision] = set()

    def record(resolution: ParentResolutionResult) -> None:
        """Fold one resolve's outcome into the walk's, without repeating anything."""
        result.linked.extend(resolution.linked)
        for gap in resolution.variable_gaps:
            if gap not in seen_gaps:
                seen_gaps.add(gap)
                result.variable_gaps.append(gap)
        # Both ambiguities are carried up too. A walk which dropped them would report
        # a chain as simply having stopped, when what happened is that a choice was
        # waiting to be made.
        for unchosen in resolution.ambiguous:
            if unchosen not in seen_ambiguous:
                seen_ambiguous.add(unchosen)
                result.ambiguous.append(unchosen)
        for unchosen_version in resolution.ambiguous_versions:
            if unchosen_version not in seen_ambiguous_versions:
                seen_ambiguous_versions.add(unchosen_version)
                result.ambiguous_versions.append(unchosen_version)
        for decision in resolution.version_decisions:
            if decision not in seen_version_decisions:
                seen_version_decisions.add(decision)
                result.version_decisions.append(decision)

    if check_until_exists and until_experiment:
        for project in _projects_to_check(session, dataset_version_ids):
            check_until_experiments_exist(
                until_experiment,
                project,
                selector,
                client=client,
                api_call_observer=api_call_observer,
            )

    frontier = _starting_frontier(session, dataset_version_ids)
    # Every version which has been a frontier. A cycle in published metadata -- two
    # simulations each declaring the other as its parent -- would otherwise ping-pong
    # between them forever, because each turn's next frontier differs from this turn's
    # even though neither is new. Refusing to climb back into a version we have already
    # climbed is what makes the walk terminate on its own, rather than on `max_hops`.
    climbed: set[int] = set()
    while frontier:
        climbed.update(frontier)

        # Facts first, and only for what might be climbed: a dataset sitting at the
        # target is never climbed, so reading its parent is work nobody asked for. This
        # is the one step applied identically at every hop, which is what makes an
        # ancestor an ordinary dataset rather than a special case.
        ensure_parent_facts(
            session,
            _frontier_needing_facts(session, frontier, until_experiment),
            facts_provider,
        )
        # Free before expensive: everything the database can already answer is linked
        # before a single request. On a second analysis over data a first one fetched,
        # this is the whole walk.
        record(
            resolve_parent_links(
                session,
                frontier,
                matchers=matchers,
                version_chooser=version_chooser,
            )
        )

        classified = _classify_frontier(session, frontier, until_experiment)
        for version_id in classified.reached_target:
            result.terminals.append(_simulation_of(session, version_id))
        for version_id, parent in classified.broken:
            failures.append(
                ParentNotFound(child=_simulation_of(session, version_id), parent=parent)
            )
        for version_id in classified.tops:
            reached = _simulation_of(session, version_id)
            result.terminals.append(reached)
            if until_experiment:
                failures.append(
                    ParentNotAncestor(
                        reached=reached, until_experiment=until_experiment
                    )
                )
        session.commit()

        if not classified.to_climb:
            break

        plan = tuple(
            item
            for item in plan_parent_searches(
                session, classified.to_climb, extra_variables=extra_variables
            )
            if item.parent not in searched
        )
        if plan:
            for item in plan:
                searched.add(item.parent)
                result.searched.append(item.parent)
                searcher(session, item)

            # The ancestors are stored now, so what was waiting on them can be linked.
            record(
                resolve_parent_links(
                    session,
                    classified.to_climb,
                    matchers=matchers,
                    version_chooser=version_chooser,
                )
            )
            _record_search_outcomes(session, plan, failures)
            session.commit()

        # The next frontier is the ancestors this turn reached, whether they were
        # searched for or were already here. Reading it off the links rather than off
        # the searches is what makes the fast path climb too.
        links = _links_from(session, classified.to_climb)
        frontier = tuple(
            version_id
            for version_id in _versions_of_parents(links)
            if version_id not in climbed
        )
        if not frontier:
            break

        # Counted per *climb*, not per search: a chain whose ancestors are all stored
        # already needs no searches at all, and a never-ending one of those has to stop
        # too.
        result.hops += 1
        if result.hops > max_hops:
            raise TooManyParentHopsError(max_hops, _simulations_of(session, frontier))

    # Before the failures are raised, so a walk which ends in one still says what its
    # successful chains were resting on.
    _warn_if_a_version_was_decided(result.version_decisions)

    if failures:
        raise ParentResolutionError(failures)

    return result


def _warn_if_a_version_was_decided(
    decisions: Sequence[ParentVersionDecision],
) -> None:
    """
    Say once that some parent releases were picked by us, or say nothing

    See [ParentVersionDecidedWarning][(m).ParentVersionDecidedWarning] for why this is
    one warning and not one per link.
    """
    if not decisions:
        return

    matched = sum(
        1 for one in decisions if one.choice == ParentVersionChoice.CHILD_VERSION
    )
    defaulted = len(decisions) - matched
    simulations = sorted({str(one.parent) for one in decisions})
    named = ", ".join(simulations[:3])
    if len(simulations) > 3:  # noqa: PLR2004 - three is just how many fit in a message
        named += f", and {len(simulations) - 3} more"

    warnings.warn(
        f"{len(decisions)} parent link(s) point at a release we picked, because the "
        f"parent dataset is published more than once. Across "
        f"{len(simulations)} parent simulation(s): {named}. Of those links: {matched} "
        f"matched the child's own version string (usually the same publication batch, "
        f"so the release that belongs with the child); {defaulted} fell back to the "
        f"newest release that has not been retracted. "
        "Nothing published says which release was branched from, so this is a rule of "
        "ours rather than something read. If the releases look too new to be the "
        "parent -- they are often all dated after the child -- that is expected: a "
        "version string is a publication date, the branch happened on the modelling "
        "centre's own disks before either end of it reached ESGF, and every release "
        "publishes that same run. Each link carries its ParentVersionChoice, and the "
        "details are on ParentWalkResult.version_decisions. To decide it yourself, "
        "pass a version_chooser to find_parents.",
        ParentVersionDecidedWarning,
        stacklevel=2,
    )


def _links_from(
    session: Session, dataset_version_ids: Collection[int]
) -> tuple[DatasetParentLink, ...]:
    """Find every link hanging off some child versions."""
    if not dataset_version_ids:  # pragma: no cover - nothing to climb breaks first
        return ()

    return tuple(
        session.exec(
            select(DatasetParentLink)
            .where(
                col(DatasetParentLink.child_dataset_version_id).in_(
                    tuple(dataset_version_ids)
                )
            )
            .order_by(col(DatasetParentLink.id))
        ).all()
    )
