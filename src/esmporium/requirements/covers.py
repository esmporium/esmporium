"""
Covers: one dataset in a lineage has to span another

A check which needs more than the
[Catalogue][esmporium.requirements.Catalogue] protocol provides -- time ranges and
branch points -- so it defines [CatalogueWithTimeInfo][(m).CatalogueWithTimeInfo]
beside itself and is a `Constraint[CatalogueWithTimeInfo]`. See
[esmporium.requirements.constraints][] for the pattern.

Every position here is a position on a stated
[TimeAxis][esmporium.datasets.TimeAxis] -- a whole number of steps on a natural axis,
or the published value on a dated one, -- and every range is
half-open ([TimeRange][esmporium.datasets.TimeRange]). Two positions are combined or
compared only after checking, at that line, that their axes allow it. Nothing here
converts between axes: a branch point arrives already placed on the axes the two runs
are recorded on. Resolving a length such as "twenty years" against an axis is
[esmporium.datasets.calendars][]'s job.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, model_validator

from esmporium.datasets import (
    MONTHS_PER_YEAR,
    BranchPoint,
    BranchTimeInChild,
    MismatchedTimeAxisError,
    NoEpochError,
    Position,
    Span,
    TimeAxis,
    TimeRange,
    TimeStep,
    UnknownCalendarError,
    UnknownProjectBranchTimeInChildError,
    UnknownProjectTimeAxisError,
    VersionTimeRange,
    advance,
    branch_time_in_child_convention,
    has_no_time_axis,
    projects_with_a_known_time_axis_vocabulary,
    retreat,
)
from esmporium.formatting import readable_list
from esmporium.requirements.catalogue import Catalogue, CatalogueEntry, set_facets
from esmporium.requirements.constraints import (
    ISSUE_TRACKER_URL,
    ConstraintView,
    Degraded,
    EnrichmentInput,
    Fail,
    MissingFeature,
    MissingTimeFact,
    NeedsEnrichment,
    Outcome,
    Pass,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from esmporium.query import QueryCanonical
    from esmporium.requirements.relations import Lineage

NO_PADDING: tuple[Span, Span] = (Span(0, TimeStep.YEARS), Span(0, TimeStep.YEARS))
"""
Padding which requires nothing either side

What an unstated ideal falls back to when the hard requirement is `None` too, so that
the soft check is never looser than the hard one.
"""


# TODO: recording a decision so that step 14 in PLAN.md does not optimise it
# away. `Covers` and the `SameTimeRange` which is still to come (PLAN step 14, for the
# siblings of step 18) look like one check with a flag, and they are not:
#
#   Covers          ancestry. The two runs' axes do *not* line up, and the branch
#                   points between them are what resolve that. No branch point means
#                   `NeedsEnrichment`, not a failure.
#   SameTimeRange   siblings, or any pair on one axis. There *is* no branch point to
#                   ask for, so axes which do not line up are a loud `Fail`: it is a
#                   question we can answer and the answer is no.
#
# So comparing two ranges directly on a shared calendar belongs to `SameTimeRange`, and
# folding it into `Covers` would lose the distinction between "misaligned, which is
# expected here" and "misaligned, which means something is wrong with the data".


# TODO (PR R3 in docs/further-background/enrichment-storage.md):
# once an entry is a version (`entry.id` is a `DatasetVersion` id),
# these answer for that version and no `VersionFilter` is needed here.
# TODO future:
# What [Covers][(m).Covers] needs. Neither
# [InMemoryCatalogue][esmporium.requirements.InMemoryCatalogue] nor
# [DatabaseCatalogue][esmporium.db.DatabaseCatalogue] provides it yet, because
# nothing records the time range of a dataset yet.
class CatalogueWithTimeInfo(Catalogue, Protocol):
    """
    A catalogue which can say when a dataset's data is and where it branched from

    Both methods answer `None` for "nobody has recorded this", which `Covers` reports
    as [NeedsEnrichment][esmporium.requirements.NeedsEnrichment] rather than as a
    failure.
    """

    def get_time_range(self, entry: CatalogueEntry) -> VersionTimeRange | None:
        """
        Get the span of a dataset's data

        Parameters
        ----------
        entry
            Dataset to get the span of

        Returns
        -------
        :
            The half-open span of its describing version, whether its files fill that
            span, and how many there were -- or `None` if nothing has recorded it

            A version whose files leave a gap still has a span, so the two are
            separate facts and both are needed: `Covers` refuses a gapped range rather
            than treating it as continuous.
        """
        ...

    def get_branch_point(self, entry: CatalogueEntry) -> BranchPoint | None:
        """
        Get where a dataset branched from its parent

        Parameters
        ----------
        entry
            Dataset to get the branch point of, i.e. the child

        Returns
        -------
        :
            The instant `entry` branched, on both runs' clocks -- or `None` if nothing
            has recorded it

            - `in_parent` is the published `branch_time_in_parent`, a position on the
              axis the *parent's* range is recorded on.
            - `in_child` is the published `branch_time_in_child`, a position on the axis
              *`entry`'s* range is recorded on, or `None` where it was not published.
              What stands in for it then is the convention of the child's project:
              see [esmporium.datasets.branch_time_in_child][].

            A fix may set either end, and a value it sets is used like a published one.
            A CMIP5 branch time is published without units and so cannot be placed on
            any axis as it stands; such a dataset answers `None` until a fix records
            the interpreted value.
        """
        ...


class CoversNeedsATimeAxisError(ValueError):
    """
    Raised when a leaf can only select data which has no time axis
    """

    def __init__(
        self, role: str, intervals: Iterable[str], projects: Iterable[str]
    ) -> None:
        """
        Initialise the error

        Parameters
        ----------
        role
            Role of the leaf

        intervals
            The reporting intervals its query asks for

        projects
            The projects those intervals were judged against
        """
        self.role = role
        self.intervals = tuple(sorted(intervals))
        self.projects = tuple(sorted(projects))
        super().__init__(
            f"Leaf {role!r} has a `Covers` check and selects only "
            f"{readable_list(self.intervals)} data, which has no time axis in "
            f"{readable_list(self.projects)}. Coverage is a question about when the "
            "data is, so there is nothing for this check to judge: a fixed field is "
            "published once for a simulation rather than over time. Drop the check "
            "from this leaf, or widen its query -- a leaf which selects fixed fields "
            "among others is fine, and reports those candidates as failing when it is "
            "solved."
        )


class CoversModeError(ValueError):
    """
    Raised when a `Covers`' mode and its thresholds do not agree
    """

    def __init__(self, message: str) -> None:
        """
        Initialise the error

        Parameters
        ----------
        message
            What does not agree, and what to write instead
        """
        super().__init__(message)


class Covers(BaseModel):
    """
    One dataset in a lineage has to cover the span of another

    For example
    `Covers(role="control", mode="containment", pad=(Span(0, TimeStep.YEARS),
    Span(0, TimeStep.YEARS)))`
    requires each dataset's control to cover the dataset's span, which is what an
    anomaly from the control needs.

    Attach it to the leaf whose lineage it judges, or above it.

    **The two runs are lined up through their branch points.** `target`'s span is
    mapped onto `role`'s axis one branch at a time, so `role` has to be an ancestor of
    `target`. Coverage is then judged along the whole series: the run `role` holds has
    to span `target` *and* every parent between the two. For example, a ZEC experiment
    branches off 1pctCO2, which branches off piControl, so a control covering the ZEC
    run has to span from the ZEC run back through 1pctCO2.

    **Nothing is defaulted.** `mode` and `pad` both have to be stated, every
    time.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: str
    """
    The covering dataset, named as the lineage names it

    Usually the `role` of the lineage, e.g. `"control"`.
    `"self"` is the dataset which filled the leaf's role,
    `"chain.<i>"` a parent in between,
    and `"end"` is the last member of the lineage whatever its role.
    See [Lineage.index_of][esmporium.requirements.Lineage.index_of].
    """

    target: str = "self"
    """
    The covered dataset, named the same way as `role`

    The one thing here with a default, because it is role naming rather than part of
    the coverage question: almost every check is about the dataset which filled the
    leaf.
    """

    mode: Literal["containment", "overlap"]
    """
    Which coverage question is being asked

    - `"containment"` -- `role` has to hold everything `target` covers.
    - `"overlap"` -- `role` has to hold [min_overlap][(c).min_overlap] of data from the
      point the two line up, which is the window an anomaly actually uses, and
      `target` has to have that much data to overlap with.
    """

    min_overlap: Span | None = None
    """
    How much data from the alignment point `role` has to hold, in overlap mode

    Required in overlap mode and refused in containment mode. A *length*, not a
    position, so the half-open convention does not touch it: `Span(100,
    TimeStep.MONTHS)` means one hundred months.

    Measured from the point the two series line up, and the data it has to overlap
    with is `target` and its parents taken together. So where `role` is further up
    than `target`'s own parent, a `target` shorter than this still meets it, as long as
    the parents in between fill the window.
    """

    # TODO future:
    # A string shorthand for the four `Span` fields -- `pad=("10 years", "10 years")`,
    # `min_overlap="100 months"` -- via a `Span.parse` mirroring the `__str__` which
    # already exists, and a `BeforeValidator`. That is the "accept any style, store
    # one" move `accept_any_query_style` makes for queries. Deliberately not done yet:
    # one new way of spelling a length is enough for one change.
    pad: tuple[Span, Span] | None
    """
    How much `role` has to cover before and after the span it is compared against

    Required to be stated, and `None` says something specific: **nothing is required**,
    so the hard check is skipped entirely and only
    [ideal_pad][(c).ideal_pad] / [ideal_min_overlap][(c).ideal_min_overlap] are
    judged. That is a coherent thing to mean -- "the control should cover this, and
    there are workarounds if it does not" -- and is different from
    `pad=(Span(0, TimeStep.YEARS), Span(0, TimeStep.YEARS))`, which requires exact
    coverage with no padding.

    A constraint with `pad=None` and no ideal at all would require nothing, and is
    refused.
    """

    ideal_min_overlap: Span | None = None
    """
    How much data from the alignment point `role` should ideally hold

    If this is not met but [min_overlap][(c).min_overlap] is, the outcome is
    [Degraded][esmporium.requirements.Degraded]: the analysis can be run, with
    something to say in the write-up.
    """

    ideal_pad: tuple[Span, Span] | None = None
    """
    How much `role` should ideally cover before and after the span compared against

    `None` means "I have no ideal", which is why these two stay optional while the
    hard thresholds do not: it says exactly what it means rather than standing in for
    a decision nobody made. Where one ideal is given and the other is not, the
    unstated one falls back to the hard requirement, so the soft check is never looser
    than the hard one.
    """

    @model_validator(mode="after")
    def _thresholds_match_the_mode(self) -> Covers:
        # After the fields, so `mode` is available to name in the error. All three of
        # these are mistakes in the requirement rather than facts about any data, so
        # they are refused when the check is built rather than reported when it runs.
        if self.mode == "overlap" and self.min_overlap is None:
            msg = (
                "mode='overlap' needs `min_overlap`: how much data from the branch "
                "point the covering dataset has to hold is the whole question in "
                "overlap mode. For 'cover everything the other run covers', which "
                "cannot be written as a number, use mode='containment'."
            )
            raise CoversModeError(msg)

        if self.mode == "containment" and self.min_overlap is not None:
            msg = (
                f"mode='containment' takes no `min_overlap`, and got "
                f"{self.min_overlap}. Containment is 'cover everything the other run "
                "covers', so the length is the other run's extent rather than "
                "something to state. Use mode='overlap' to ask for a fixed window "
                "instead."
            )
            raise CoversModeError(msg)

        if (
            self.pad is None
            and self.ideal_pad is None
            and self.ideal_min_overlap is None
        ):
            msg = (
                "pad=None means nothing is required, so with no `ideal_pad` and no "
                "`ideal_min_overlap` this check requires nothing at all and would "
                "pass whatever the data. Either state the padding which is required, "
                "or state the ideal which is not met."
            )
            raise CoversModeError(msg)

        return self

    def check_leaf(self, role: str, query: QueryCanonical) -> None:
        """
        Refuse a leaf which can only select data with no time axis

        Returns nothing: this is here to raise. See
        [ChecksItsLeaf][esmporium.requirements.ChecksItsLeaf].

        Parameters
        ----------
        role
            Role of the leaf, named in the error

        query
            The query the leaf will be solved with, i.e. including any facets the
            requirement adds

        Raises
        ------
        CoversNeedsATimeAxisError
            Every dataset the query can select has no time axis
        """
        asked = set_facets(query)
        intervals = asked.get("reporting_interval")
        if not intervals:
            # A query which names no frequency can select anything, so there is
            # nothing to refuse on. The solve-time tier catches whatever it finds.
            return

        # Every project the query names, or -- where it names none, so the query spans
        # the archive -- every project we can answer for. Refusing then means the
        # interval has no time axis in all of them, which `fx` does.
        projects = asked.get("project") or tuple(
            sorted(projects_with_a_known_time_axis_vocabulary())
        )
        try:
            no_time_axis = all(
                has_no_time_axis(project, interval)
                for project in projects
                for interval in intervals
            )
        except UnknownProjectTimeAxisError:
            # We cannot say, so we do not refuse. Whether a project we know nothing
            # about has a time axis is not a question to answer by guessing, and the
            # solve-time tier will say so against the datasets it actually finds.
            return

        if no_time_axis:
            raise CoversNeedsATimeAxisError(role, intervals, projects)

    def evaluate(
        self, view: ConstraintView, catalogue: CatalogueWithTimeInfo
    ) -> Outcome:
        """
        Evaluate whether every lineage in view meets the constraint

        Parameters
        ----------
        view
            The datasets chosen so far, and the chains behind them

        catalogue
            Catalogue, used to get time ranges and branch points

        Returns
        -------
        :
            The worst outcome across the lineages in view, which is
            [Fail][esmporium.requirements.Fail] before
            [MissingFeature][esmporium.requirements.MissingFeature] before
            [NeedsEnrichment][esmporium.requirements.NeedsEnrichment] before
            [Degraded][esmporium.requirements.Degraded] before
            [Pass][esmporium.requirements.Pass]

            Every lineage is judged rather than the first failure ending it, so that a
            `NeedsEnrichment` names every dataset which has to be enriched and one
            call to the enricher clears the lot.

        Raises
        ------
        ValueError
            `role` or `target` is not in a lineage, or `role` is not an ancestor of
            `target`. Both are mistakes in the requirement, so they stop rather than
            being reported.
        """
        if not view.lineages:
            return Fail("Covers needs a lineage, but none was resolved")

        unsupported = self._unsupported_spans()
        if unsupported:
            return MissingFeature(
                f"{readable_list([str(span) for span in unsupported], quote=False)} "
                "cannot be asked for yet: a requirement may be stated in years or "
                "months, and anything finer is not supported. This is a limit of "
                "how the requirement can be *written* rather than of which data we "
                "can judge "
                "-- daily and sub-daily datasets are judged fine, in years or months. "
                f"Please say you wanted it at {ISSUE_TRACKER_URL}."
            )

        cannot_answer = self._catalogue_cannot_answer(catalogue)
        if cannot_answer:
            return Fail(cannot_answer)

        outcomes: list[Outcome] = []
        for leaf_role, lineages in sorted(view.lineages.items()):
            subject = f"{view.prefix}{leaf_role}"
            outcomes.extend(
                self._judge(lineage, catalogue, subject) for lineage in lineages
            )

        return _worst(outcomes)

    def _unsupported_spans(self) -> tuple[Span, ...]:
        """
        Get the thresholds stated in a unit we do not support yet

        Returns
        -------
        :
            Those of this check's spans which are finer than a month, in the order
            they are declared

            A decision about the API surface, and nothing to do with which data we
            can judge: see [evaluate][(c).evaluate]'s `MissingFeature` message.
        """
        stated = [
            self.min_overlap,
            *(self.pad or ()),
            self.ideal_min_overlap,
            *(self.ideal_pad or ()),
        ]

        return tuple(
            span
            for span in stated
            if span is not None and not span.step.is_calendar_free
        )

    def _catalogue_cannot_answer(self, catalogue: CatalogueWithTimeInfo) -> str:
        """
        Say why a catalogue cannot be asked for what this check needs

        Asked explicitly rather than left to an `AttributeError`, because a catalogue
        which cannot serve a constraint is a fact about this solve rather than a bug,
        and the solver no longer turns every exception into a failure.

        Parameters
        ----------
        catalogue
            The catalogue handed to this check

        Returns
        -------
        :
            The message, or `""` when the catalogue can answer
        """
        needed = ["get_time_range", "get_branch_point"]
        absent = [
            name for name in needed if not callable(getattr(catalogue, name, None))
        ]
        if not absent:
            return ""

        return (
            f"this catalogue has no {readable_list(absent)}, so it cannot be asked "
            f"when a dataset's data is. Covers needs a catalogue providing "
            f"`CatalogueWithTimeInfo`, and {type(catalogue).__name__} is not one."
        )

    def _judge(
        self, lineage: Lineage, catalogue: CatalogueWithTimeInfo, subject: str
    ) -> Outcome:
        """
        Judge one lineage

        Parameters
        ----------
        lineage
            The chain being judged

        catalogue
            Catalogue, used to get time ranges and branch points

        subject
            Role path being judged, used in every message

        Returns
        -------
        :
            What this one lineage comes to

        Raises
        ------
        ValueError
            `role` or `target` is not in this lineage, or `role` is not an ancestor of
            `target`
        """
        members = lineage.members
        target_index = lineage.index_of(self.target)
        role_index = lineage.index_of(self.role)
        covering = members[role_index]

        self._check_is_an_ancestor(target_index, role_index)

        involved = self._involved(members, target_index, role_index)

        no_time_axis = self._datasets_with_no_time_axis(involved)
        if no_time_axis:
            described = readable_list(
                [f"#{entry.id} ({entry.reporting_interval})" for entry in no_time_axis],
                quote=False,
            )

            return Fail(
                f"{subject}: {described} "
                f"{'has' if len(no_time_axis) == 1 else 'have'} no time axis, so "
                "there is no span to cover or be covered. A fixed field is published "
                "once for a simulation rather than over time."
            )

        ranges = self._ranges(catalogue, involved, subject)
        if isinstance(ranges, NeedsEnrichment):
            return ranges

        # Before any branch point is asked for, deliberately: a gap is a definite
        # answer, so enriching first would cost a round trip and still fail.
        gapped = [
            entry
            for entry, each in zip(involved, ranges, strict=True)
            if not each.is_contiguous
        ]
        if gapped:
            described = readable_list([f"#{entry.id}" for entry in gapped], quote=False)

            found = (
                f"{described} has a gap in it -- its files do not fill the span they "
                "cover -- so coverage cannot be judged from its first and last step"
                if len(gapped) == 1
                else f"{described} have gaps in them -- their files do not fill the "
                "spans they cover -- so coverage cannot be judged from their first "
                "and last steps"
            )

            return Fail(
                f"{subject}: {found}. Treating a gapped range as continuous is how a "
                "missing decade is reported as covered."
            )

        aligned = self._align(catalogue, involved, ranges, subject)
        if not isinstance(aligned, _Alignment):
            return aligned

        return self._compare(
            covered=involved[0],
            covering=covering,
            have=ranges[-1].range,
            aligned=aligned,
            subject=subject,
        )

    def _involved(
        self, members: Sequence[CatalogueEntry], target_index: int, role_index: int
    ) -> tuple[CatalogueEntry, ...]:
        """
        Get the datasets this check reads, covered one first and covering one last

        That is the chain from the covered dataset up to the covering one, which is
        the series coverage is judged along. A member of the
        lineage below `target` or above `role` is no part of the question, so it is
        neither judged nor asked to be enriched.

        Parameters
        ----------
        members
            The lineage's members

        target_index
            Position of the covered dataset

        role_index
            Position of the covering dataset

        Returns
        -------
        :
            The covered dataset, any parents between the two, and the covering one
        """
        return tuple(members[target_index : role_index + 1])

    def _check_is_an_ancestor(self, target_index: int, role_index: int) -> None:
        """
        Check the covering dataset is one the covered one descends from

        Returns nothing: this is here to raise.

        Parameters
        ----------
        target_index
            Position of the covered dataset in the lineage

        role_index
            Position of the covering dataset

        Raises
        ------
        ValueError
            The covering dataset is not an ancestor of the covered one
        """
        if target_index >= role_index:
            msg = (
                f"{self.role!r} must be an ancestor of {self.target!r}: Covers lines "
                "the two up through the branch points between them"
            )
            raise ValueError(msg)

    def _datasets_with_no_time_axis(
        self, entries: Sequence[CatalogueEntry]
    ) -> tuple[CatalogueEntry, ...]:
        """
        Get which of the datasets involved have no time axis at all

        The second of the two tiers: one of these found among other candidates is
        reported rather than refusing the requirement.

        Parameters
        ----------
        entries
            The datasets this check is about

        Returns
        -------
        :
            Those with no time axis, in the order given
        """
        found = []
        for entry in entries:
            try:
                if has_no_time_axis(entry.project, entry.reporting_interval):
                    found.append(entry)
            except UnknownProjectTimeAxisError:
                continue

        return tuple(found)

    def _ranges(
        self,
        catalogue: CatalogueWithTimeInfo,
        involved: Sequence[CatalogueEntry],
        subject: str,
    ) -> tuple[VersionTimeRange, ...] | NeedsEnrichment:
        """
        Get the recorded range of every dataset involved, or say which are missing

        Parameters
        ----------
        catalogue
            Catalogue to ask

        involved
            The datasets this check reads, as
            [_involved][(c).Covers._involved] gives them

        subject
            Role path being judged, used in the message

        Returns
        -------
        :
            One range per dataset, in the order they were given, or
            [NeedsEnrichment][esmporium.requirements.NeedsEnrichment] naming every
            dataset whose range is not recorded
        """
        ranges: list[VersionTimeRange] = []
        unrecorded_ranges: list[CatalogueEntry] = []
        for entry in involved:
            each = catalogue.get_time_range(entry)
            if each is None:
                unrecorded_ranges.append(entry)
            else:
                ranges.append(each)

        # Every one which is missing rather than the first, so that one run of the
        # enricher clears the lot.
        if unrecorded_ranges:
            unrecorded_ranges.sort(key=lambda entry: entry.id)
            listed = readable_list(
                [f"#{entry.id}" for entry in unrecorded_ranges], quote=False
            )

            return _needs(
                subject,
                f"nothing has recorded when {listed} "
                f"{'is' if len(unrecorded_ranges) == 1 else 'are'}",
                "its start and end come off its filenames, so this costs nothing but "
                "has to be done once",
                unrecorded_ranges,
                MissingTimeFact.TIME_RANGE,
            )

        return tuple(ranges)

    def _align(
        self,
        catalogue: CatalogueWithTimeInfo,
        involved: Sequence[CatalogueEntry],
        ranges: Sequence[VersionTimeRange],
        subject: str,
    ) -> _Alignment | Outcome:
        """
        Put the covered dataset's span onto the covering dataset's axis

        Parameters
        ----------
        catalogue
            Catalogue, asked for one branch point per hop

        involved
            The datasets this check reads, as [_involved][(c).Covers._involved] gives
            them

        ranges
            Their ranges, in the same order

        subject
            Role path being judged, used in every message

        Returns
        -------
        :
            The alignment point and the covered run's mapped `end_exclusive`, both as
            positions on the covering dataset's axis -- or why they could not be found
        """
        hops = self._hops(catalogue, involved, ranges, subject)
        if isinstance(hops, (NeedsEnrichment, MissingFeature)):
            return hops

        return _mapped_through(hops, subject)

    def _hops(
        self,
        catalogue: CatalogueWithTimeInfo,
        involved: Sequence[CatalogueEntry],
        ranges: Sequence[VersionTimeRange],
        subject: str,
    ) -> tuple[_Hop, ...] | NeedsEnrichment | MissingFeature:
        """
        Get where each dataset involved branched from the next one up, on both clocks

        A branch point's `in_child` is used whenever it has one, whether the child's
        header published it or a fix set it. Where it has none, the child's project
        says what happens, through
        [branch_time_in_child_convention][esmporium.datasets.branch_time_in_child_convention]:
        a project which publishes it has simply not had it recorded yet, and one which
        does not (CMIP5) has its child's first time bound stand in for it, with that
        assumption carried into every message built on it.

        Parameters
        ----------
        catalogue
            Catalogue to ask

        involved
            The datasets this check reads, as [_involved][(c).Covers._involved] gives
            them

        ranges
            Their ranges, in the same order

        subject
            Role path being judged, used in the message

        Returns
        -------
        :
            One hop per pair of neighbours, so one fewer than there are datasets,
            covered one first -- or
            [MissingFeature][esmporium.requirements.MissingFeature] naming every child
            whose project's convention we do not know, or
            [NeedsEnrichment][esmporium.requirements.NeedsEnrichment] naming every
            dataset whose branch point is not recorded
        """
        hops: list[_Hop] = []
        unrecorded: list[CatalogueEntry] = []
        unknown: list[CatalogueEntry] = []
        for index, child in enumerate(involved[:-1]):
            parent = involved[index + 1]
            point = catalogue.get_branch_point(child)
            if point is None:
                unrecorded.append(child)
                continue

            child_held, parent_held = ranges[index].range, ranges[index + 1].range
            if point.in_child is not None:
                hops.append(
                    _Hop(
                        child=child,
                        parent=parent,
                        child_held=child_held,
                        parent_held=parent_held,
                        in_parent=point.in_parent,
                        in_child=point.in_child,
                    )
                )
                continue

            try:
                convention = branch_time_in_child_convention(child.project)
            except UnknownProjectBranchTimeInChildError:
                unknown.append(child)
                continue

            if convention is BranchTimeInChild.PUBLISHED:
                unrecorded.append(child)
                continue

            # `BranchTimeInChild.ASSUMED_FIRST_TIME_BOUND`: the run is taken to start at
            # its branch, so its own first time bound stands in for `in_child`.
            first = Position(child_held.start, child_held.axis)
            hops.append(
                _Hop(
                    child=child,
                    parent=parent,
                    child_held=child_held,
                    parent_held=parent_held,
                    in_parent=point.in_parent,
                    in_child=first,
                    assumption=(
                        f"#{child.id} is {child.project}, which does not publish "
                        "`branch_time_in_child`, so it is assumed to be "
                        f"#{child.id}'s first time bound, {first}. If "
                        f"#{child.id}'s leading files are "
                        "missing, that is later than its branch; a fix which states "
                        "`branch_time_in_child` replaces this assumption."
                    ),
                )
            )

        # `MissingFeature` before `NeedsEnrichment`, for the reason `_worst` gives:
        # enriching will not make a convention we do not know into one we do.
        if unknown:
            unknown.sort(key=lambda entry: entry.id)
            one = len(unknown) == 1
            listed = readable_list([f"#{entry.id}" for entry in unknown], quote=False)
            projects = readable_list(sorted({entry.project for entry in unknown}))
            onto = "its parent's axis" if one else "their parents' axes"

            return MissingFeature(
                f"{subject}: {listed} {'is' if one else 'are'} from {projects}, and "
                "we do not know whether that publishes `branch_time_in_child`, or what "
                "to assume where it does not, so there is no way to put "
                f"{'its' if one else 'their'} data onto {onto}. Saying so is one "
                "line in `BRANCH_TIME_IN_CHILD_BY_PROJECT`, so please say you wanted "
                f"it at {ISSUE_TRACKER_URL}."
            )

        # Every one which is missing rather than the first, so that one run of the
        # enricher clears the lot.
        if unrecorded:
            unrecorded.sort(key=lambda entry: entry.id)
            listed = readable_list(
                [f"#{entry.id}" for entry in unrecorded], quote=False
            )

            return _needs(
                subject,
                f"nothing has recorded where {listed} branched from "
                f"{'its parent' if len(unrecorded) == 1 else 'their parents'}",
                "the runs are on different axes and the branch point is what lines "
                "them up. A CMIP6 or CMIP7 file header publishes both of its ends, "
                "and a CMIP5 branch time is published without units, so it needs a fix",
                unrecorded,
                MissingTimeFact.BRANCH_POINT,
            )

        return tuple(hops)

    def _compare(
        self,
        *,
        covered: CatalogueEntry,
        covering: CatalogueEntry,
        have: TimeRange,
        aligned: _Alignment,
        subject: str,
    ) -> Outcome:
        """
        Compare what the covering dataset holds against what is needed

        Parameters
        ----------
        covered
            The covered dataset, named when its own data is what falls short

        covering
            The covering dataset, named in every message

        have
            What it holds

        aligned
            Where the two runs line up and where the covered run ends, on `have`'s
            axis, and what the mapping assumed

        subject
            Role path being judged, used in every message

        Returns
        -------
        :
            `Fail` if the hard requirement is not met, `Degraded` if the ideal is not,
            `Pass` otherwise -- or `NeedsEnrichment` if the comparison needs a finer
            axis than the data is recorded on

            Every one but `NeedsEnrichment` names what the mapping assumed, a `Pass`
            included: an answer which rests on an assumption says so.
        """
        if self.pad is not None:
            met = _covers(have, aligned, self.min_overlap, self.pad)
            if isinstance(met, NeedsEnrichment):
                return _needs(
                    subject,
                    met.message,
                    "a file header gives the calendar and the time units, and the "
                    "comparison is exact once it has them",
                    (covering,),
                    MissingTimeFact.CALENDAR,
                )

            if not met.covered:
                return Fail(
                    _with_assumptions(
                        self._described(
                            subject,
                            covered=covered,
                            covering=covering,
                            have=have,
                            needed=met,
                            padding=f"with padding {_described_pad(self.pad)}",
                            wanted="wanted",
                        ),
                        aligned.assumptions,
                    )
                )

        if self.ideal_min_overlap is None and self.ideal_pad is None:
            return _passed(subject, aligned.assumptions)

        # An unstated ideal falls back to the hard requirement, so the soft check is
        # never looser than the hard one -- an `ideal_min_overlap` on its own would
        # otherwise quietly drop the padding which *is* required.
        ideal_overlap = self.ideal_min_overlap or self.min_overlap
        ideal_pad = self.ideal_pad
        if ideal_pad is None:
            ideal_pad = self.pad or NO_PADDING

        ideally = _covers(have, aligned, ideal_overlap, ideal_pad)
        if isinstance(ideally, NeedsEnrichment):
            return _needs(
                subject,
                ideally.message,
                "a file header gives the calendar and the time units, and the "
                "comparison is exact once it has them",
                (covering,),
                MissingTimeFact.CALENDAR,
            )

        if not ideally.covered:
            return Degraded(
                _with_assumptions(
                    self._described(
                        subject,
                        covered=covered,
                        covering=covering,
                        have=have,
                        needed=ideally,
                        padding=f"ideal padding {_described_pad(ideal_pad)} not met",
                        wanted="ideally wanted",
                    ),
                    aligned.assumptions,
                )
            )

        return _passed(subject, aligned.assumptions)

    def _described(  # noqa: PLR0913 - one argument per thing the message names
        self,
        subject: str,
        *,
        covered: CatalogueEntry,
        covering: CatalogueEntry,
        have: TimeRange,
        needed: _Window,
        padding: str,
        wanted: str,
    ) -> str:
        """
        Describe what fell short of what was wanted

        One spelling, used by both the hard and the soft message, because two would be
        free to drift and a reader comparing a `Fail` with a `Degraded` is comparing
        the same numbers. It names each of the two ways a window can fall short which
        applies, and only those:

        - the covering dataset does not hold the window, e.g.
          `tas: #2 covers [0, 200) and needs [90, 260), in years, with padding ...`;
        - the covered run stops before the overlap it is meant to supply, e.g.
          `tas: #1's data runs to 150 on #2's axis, short of the 100 years of overlap
          wanted from 100, which runs to 200, in years`.

        Parameters
        ----------
        subject
            Role path being judged

        covered
            The covered dataset

        covering
            The covering dataset

        have
            What the covering dataset holds

        needed
            What was wanted, on `have`'s axis

        padding
            What was said about padding, attached to the covering dataset's part

        wanted
            How the overlap was wanted, "wanted" or "ideally wanted"

        Returns
        -------
        :
            The description
        """
        units = needed.axis.units
        parts = []
        if not needed.held:
            parts.append(
                f"#{covering.id} covers [{have.start}, {have.end_exclusive}) and needs "
                f"[{needed.start}, {needed.end_exclusive}), in {units}, {padding}"
            )

        if not needed.overlap_supplied:
            parts.append(
                f"#{covered.id}'s data runs to {needed.covered_end} on "
                f"#{covering.id}'s axis, short of the {needed.overlap} of overlap "
                f"{wanted} from {needed.anchor}, which runs to {needed.overlap_end}, "
                f"in {units}"
            )

        return f"{subject}: {'; and '.join(parts)}"


@dataclass(frozen=True)
class _Hop:
    """
    One branch of the series: a child, its parent, and the instant it branched

    The branch point with `in_child` settled, so the mapping never has to ask where
    it came from, together with the data each run holds, so every check on the hop
    has what it compares against to hand.
    """

    child: CatalogueEntry
    """The run which branched"""

    parent: CatalogueEntry
    """The run it branched from"""

    child_held: TimeRange
    """What the child holds, on its own recorded axis"""

    parent_held: TimeRange
    """What the parent holds, on its own recorded axis"""

    in_parent: Position
    """Where it branched, read off the parent's axis"""

    in_child: Position
    """Where it branched, read off the child's axis"""

    assumption: str | None = None
    """
    What `in_child` rests on when it was not published or fixed, as a sentence for a
    message, or `None` when it was
    """

    def onto_parent(self, at: Position) -> Position:
        """
        Get the same instant as a position on the child's axis, read off the parent's

        `at - in_child` is how far after the branch `at` is, counted in the child's
        steps. The same length after `in_parent`, counted in the parent's steps, is
        the same instant. The child's epoch cancels in the subtraction, so the two
        axes may start counting from different dates; the length is read in the
        parent's steps and calendar, so those have to be the child's.

        Parameters
        ----------
        at
            The position, on the child's axis

        Returns
        -------
        :
            The same instant, on the parent's axis

        Raises
        ------
        MismatchedTimeAxisError
            `at` is not on the child's axis, or the child and the parent count in a
            different step or calendar, which
            [_mapped_through][(m)._mapped_through] rules out before mapping, so this
            is a bug
        """
        if not at.axis.is_comparable_with(self.in_child.axis):
            raise MismatchedTimeAxisError(at.axis, self.in_child.axis)

        if not _lengths_cross(self.in_child.axis, self.in_parent.axis):
            raise MismatchedTimeAxisError(self.in_child.axis, self.in_parent.axis)

        after_branch = at.value - self.in_child.value

        return Position(self.in_parent.value + after_branch, self.in_parent.axis)


def _lengths_cross(child: TimeAxis, parent: TimeAxis) -> bool:
    """
    Determine whether a length counted on one axis means the same on another

    Parameters
    ----------
    child
        The axis the length is counted on

    parent
        The axis it is read on

    Returns
    -------
    :
        `True` if both count in the same step, both or neither from an epoch, and in
        the same calendar. The epochs themselves may differ, because a length has
        none.
    """
    return (
        child.units.step is parent.units.step
        and child.units.is_natural == parent.units.is_natural
        and child.calendar == parent.calendar
    )


@dataclass(frozen=True)
class _Alignment:
    """
    Where two runs line up, once the covered one has been mapped

    A type rather than a pair of numbers so that
    [_align][(c).Covers._align] can answer either this or an outcome and the caller
    can tell which it got by name.
    """

    anchor: Position
    """
    Where the series line up, on the covering dataset's axis

    Where the run directly below the covering dataset branched off it, which is the
    first instant of the series below it that runs alongside the covering dataset.
    """

    covered_end: Position
    """The covered run's `end_exclusive`, on the covering dataset's axis"""

    assumptions: tuple[str, ...] = ()
    """
    What the mapping assumed rather than read, one sentence each

    Carried into every message built on this alignment.
    """

    def __post_init__(self) -> None:
        """
        Check both positions are on the same axis

        Raises
        ------
        MismatchedTimeAxisError
            They are not, which the mapping rules out, so this is a bug
        """
        if not self.anchor.axis.is_comparable_with(self.covered_end.axis):
            raise MismatchedTimeAxisError(self.anchor.axis, self.covered_end.axis)


# TODO future:
# Other anchors are coherent and not supported. "piControl has to overlap my scenario,
# and I do not care about the historical run it came through" is anchored at the covered
# run's own mapped start rather than at the bottom of the chain, and `min_overlap`
# could be wanted from either. Saying which is a field on `Covers`, so both wait for
# somebody to ask for one.
def _mapped_through(hops: Sequence[_Hop], subject: str) -> _Alignment | Outcome:
    """
    Map the covered run onto the covering dataset's axis, one branch at a time

    Every run counts time on its own axis, a ruler with its own zero, so the same
    instant reads a different number off each. Here, every axis counts days in the
    same calendar, from a different date:

    ```text
                          historical      ssp585            ssp585's end
                          branches        branches          (what is carried up)
                              │               │                 │
      piControl's axis    0 ──┼───────────────┼─────────────────┼──►
      (days since 0001)    182500          242725            274115
                              │               │                 │
      historical's axis       0 ──────────────┼─────────────────┼──►
      (days since 1850)                     60225             91615
                                              │                 │
      ssp585's axis                           0 ────────────────┼──►
      (days since 2015)                                       31390
    ```

    31390, 91615 and 274115 are the same instant. Each hop reads it off the next axis
    up, with [_Hop.onto_parent][(m)._Hop.onto_parent], until it is on the covering
    dataset's. The alignment point is where the run directly below the covering
    dataset branched off it, 182500 here.

    Everything which can stop the mapping is checked first, in the order the outcomes
    rank (see [_worst][(m)._worst]):

    1. [_ends_off_their_runs][(m)._ends_off_their_runs]: `Fail` if a branch point is
       not on its own runs' axes;
    2. [_broken_joins][(m)._broken_joins]: `Fail` if the series does not join up;
    3. [_uncrossable][(m)._uncrossable]: `MissingFeature` if a child and its parent
       count time in ways a length cannot cross between.

    Parameters
    ----------
    hops
        Where each dataset branched from the next one up, covered one first

    subject
        Role path being judged, used in every message

    Returns
    -------
    :
        The alignment point and the covered run's mapped `end_exclusive`, on the
        covering dataset's axis -- or the first of the outcomes above which applies
    """
    off_their_runs = _ends_off_their_runs(hops, subject)
    if off_their_runs is not None:
        return off_their_runs

    broken = _broken_joins(hops, subject)
    if broken is not None:
        return broken

    uncrossable = _uncrossable(hops, subject)
    if uncrossable is not None:
        return uncrossable

    covered = hops[0].child_held
    covered_end = Position(covered.end_exclusive, covered.axis)
    for hop in hops:
        covered_end = hop.onto_parent(covered_end)

    return _Alignment(
        anchor=hops[-1].in_parent,
        covered_end=covered_end,
        assumptions=tuple(hop.assumption for hop in hops if hop.assumption is not None),
    )


def _ends_off_their_runs(hops: Sequence[_Hop], subject: str) -> Fail | None:
    """
    Check each end of every branch point is on its own run's recorded axis

    One on any other axis is the data disagreeing with itself rather than a question
    about alignment, so it is loud, per settled decision 11.

    Parameters
    ----------
    hops
        Where each dataset branched from the next one up, covered one first

    subject
        Role path being judged, used in the message

    Returns
    -------
    :
        `Fail` naming the first end which is off its run's axis, or `None` if none is
    """
    for hop in hops:
        if not hop.in_parent.axis.is_comparable_with(hop.parent_held.axis):
            return Fail(_off_its_axis(subject, hop, "parent"))

        if not hop.in_child.axis.is_comparable_with(hop.child_held.axis):
            return Fail(_off_its_axis(subject, hop, "child"))

    return None


def _broken_joins(hops: Sequence[_Hop], subject: str) -> Fail | None:
    """
    Check the series joins up, on both sides of every branch

    Coverage is judged along the whole series, and a break anywhere in it leaves a hole
    no amount of covering can fill. The two sides of a join ask **different**
    questions, so they use different comparisons:

    ```text
                      the parent's data has to run UP TO the branch
      parent   ═══════════════════════╡
                                      ▲ the branch
                                      ╞═══════════════════════   child
                                      the child's data has to run FROM the branch

      child side:   child_held.holds(in_child)
                    start <= in_child <  end_exclusive    holds the branch step
      parent side:  parent_held.runs_up_to(in_parent)
                    start <= in_parent <= end_exclusive   may stop exactly there
    ```

    - **Child side, `<`**: the child has to hold the step it branched at. One whose
      data starts later is missing its leading files.
    - **Parent side, `<=`**: the parent's data has to reach the branch, and reaching it
      exactly is a seamless join. `ssp585` branches off `historical` at 2015-01-01,
      which is `historical`'s `end_exclusive`: the first instant *after* its data.
      With `<` there, every scenario chained through `historical` would read as broken.

    The parent side is checked for the runs in between only. The join onto the covering
    dataset itself is left out, because whether *it* holds data at the alignment point
    is the coverage question [_compare][(m).Covers._compare] answers, with the window
    it needed in the message -- and there, a covering run which stops exactly at the
    branch has nothing to overlap with, so it is not enough.

    Parameters
    ----------
    hops
        Where each dataset branched from the next one up, covered one first

    subject
        Role path being judged, used in the message

    Returns
    -------
    :
        `Fail` naming every broken join, so that one look finds them all, or `None` if
        there are none
    """
    broken: list[str] = []
    for hop in hops:
        # `<=` at the top, on purpose: a parent which stops exactly where its child
        # branched joins up. See the diagram above.
        if hop is not hops[-1] and not hop.parent_held.runs_up_to(hop.in_parent):
            broken.append(
                f"#{hop.child.id} branched from #{hop.parent.id} at "
                f"{hop.in_parent.value}, outside #{hop.parent.id}'s data, which runs "
                f"from {hop.parent_held.start} up to {hop.parent_held.end_exclusive}"
            )

        # `<` at the top, on purpose: the child has to hold the step it branched at.
        if not hop.child_held.holds(hop.in_child):
            broken.append(
                f"#{hop.child.id} branched at {hop.in_child.value} on its own axis, "
                f"which is outside the [{hop.child_held.start}, "
                f"{hop.child_held.end_exclusive}) it holds"
            )

    if not broken:
        return None

    return Fail(
        f"{subject}: {readable_list(broken, quote=False)}. Coverage is judged "
        "along the whole series from the covering dataset down, so a run which "
        "branched where it or its parent has no data breaks it."
    )


def _uncrossable(hops: Sequence[_Hop], subject: str) -> MissingFeature | None:
    """
    Check a length after each branch can be read off the parent's axis as it stands

    Parameters
    ----------
    hops
        Where each dataset branched from the next one up, covered one first

    subject
        Role path being judged, used in the message

    Returns
    -------
    :
        `MissingFeature` for the first child and parent which count in a different
        step, one from an epoch and one not, or in a different calendar, or `None`
        if every length crosses as it stands
    """
    for hop in hops:
        child, parent = hop.in_child.axis, hop.in_parent.axis
        if child.units.step is not parent.units.step:
            return MissingFeature(
                f"{subject}: #{hop.child.id} reports every "
                f"{child.units.step.value.rstrip('s')} and #{hop.parent.id} every "
                f"{parent.units.step.value.rstrip('s')}, and comparing a run against "
                "a parent at a different frequency is not supported yet. It is a "
                "fair thing to want -- the two spans are the same lengths written in "
                "different units -- so please say you wanted it at "
                f"{ISSUE_TRACKER_URL}."
            )

        if child.units.is_natural != parent.units.is_natural:
            natural, dated = (
                (hop.child, hop.parent)
                if child.units.is_natural
                else (hop.parent, hop.child)
            )

            return MissingFeature(
                f"{subject}: #{natural.id} counts {child.units.step.value} as a plain "
                f"ordinal and #{dated.id} counts them from a date, and lining up a "
                "run with a parent counted the other way is not supported yet. "
                "Placing one on the other's axis is a conversion the enricher makes "
                "when it records the branch point, so please say you wanted it at "
                f"{ISSUE_TRACKER_URL}."
            )

        # Strict on purpose, as `TimeAxis.is_comparable_with` is: some models use
        # different calendars for a control and its experiments, and whether that
        # matters depends on the data, so any loosening has to say which case it
        # allows rather than dropping the check.
        if child.calendar != parent.calendar:
            return MissingFeature(
                f"{subject}: #{hop.child.id} is in calendar {child.calendar!r} and "
                f"#{hop.parent.id} in {parent.calendar!r}, and lining up a run with a "
                "parent in a different calendar is not supported yet. Whether it "
                "matters depends on the data -- monthly data rarely cares which "
                "calendar each run used, and daily data does -- so please say you "
                f"wanted it at {ISSUE_TRACKER_URL}."
            )

    return None


def _off_its_axis(subject: str, hop: _Hop, end: Literal["parent", "child"]) -> str:
    """
    Say that one end of a branch point is not on the axis of the run it belongs to

    Parameters
    ----------
    subject
        Role path being judged

    hop
        The branch whose point it is

    end
        Which end of the branch point it is

    Returns
    -------
    :
        The message
    """
    run, at, held = (
        (hop.parent, hop.in_parent, hop.parent_held)
        if end == "parent"
        else (hop.child, hop.in_child, hop.child_held)
    )

    return (
        f"{subject}: the {end} end of #{hop.child.id}'s branch point is {at}, but "
        f"#{run.id}'s own data is on a {held.axis.units} axis in calendar "
        f"{held.axis.calendar!r}. The two have to be the same axis for the branch "
        "point to mean anything."
    )


def _with_assumptions(message: str, assumptions: Sequence[str]) -> str:
    """
    Add what an answer assumed to its message

    Parameters
    ----------
    message
        The answer

    assumptions
        What it rests on, one sentence each

    Returns
    -------
    :
        `message`, followed by every assumption
    """
    if not assumptions:
        return message

    return f"{message}. {' '.join(assumptions)}"


def _passed(subject: str, assumptions: Sequence[str]) -> Pass:
    """
    Say that the check passed, and what that rests on

    Parameters
    ----------
    subject
        Role path being judged

    assumptions
        What the answer assumed rather than read, one sentence each

    Returns
    -------
    :
        A `Pass`, whose message names every assumption, or is empty when there are none
    """
    if not assumptions:
        return Pass()

    return Pass(f"{subject}: {' '.join(assumptions)}")


@dataclass(frozen=True)
class _Window:
    """
    The window a comparison asked for, and whether it was covered

    A type rather than a tuple so that the things it carries are named where they are
    read, which is in two message formats and one `if`. A window can fall short in two
    separate ways, and each is kept so the message can say which:

    - the **covering** dataset does not hold all of the window (`held`);
    - in overlap mode, the **covered** run's data stops before the overlap it is meant
      to supply does (`overlap_supplied`).
    """

    start: float
    """First step which has to be covered, on `axis`"""

    end_exclusive: float
    """First step after the last which has to be covered, on `axis`"""

    axis: TimeAxis
    """The covering dataset's recorded axis, which every number here is on"""

    held: bool
    """Whether the covering dataset holds all of `[start, end_exclusive)`"""

    anchor: float
    """The alignment point, where any overlap is counted from"""

    covered_end: float
    """The covered run's `end_exclusive`, read off `axis`"""

    overlap: Span | None
    """The overlap wanted from `anchor`, or `None` in containment mode"""

    overlap_end: float | None
    """Where that overlap ends, or `None` in containment mode"""

    @property
    def overlap_supplied(self) -> bool:
        """
        Whether the covered run has data all the way through the overlap

        Returns
        -------
        :
            `True` in containment mode, which asks for no overlap, and otherwise
            `True` if `covered_end >= overlap_end`
        """
        return self.overlap_end is None or self.covered_end >= self.overlap_end

    @property
    def covered(self) -> bool:
        """
        Whether nothing falls short

        Returns
        -------
        :
            `True` if the covering dataset holds the window and the covered run
            supplies the overlap
        """
        return self.held and self.overlap_supplied


def _covers(
    have: TimeRange,
    aligned: _Alignment,
    min_overlap: Span | None,
    pad: tuple[Span, Span],
) -> _Window | NeedsEnrichment:
    """
    Determine whether one span covers what is needed, with padding

    The one piece of arithmetic, serving both modes with the upper bound swapped:

    ```text
                         BP
                          │
      covering     |──────┼───────────────|
                          │
      covered             |═══════════════════════════|
                          │
             ◄── pad[0] ──┤◄──── needed ────►├ pad[1] ►
    ```

    where `needed` ends at `anchor + min_overlap` in overlap mode and at the covered
    run's own end in containment mode.

    Parameters
    ----------
    have
        What the covering dataset holds

    aligned
        Where the two runs line up, and the covered run's mapped `end_exclusive`

    min_overlap
        How much data from `anchor` is wanted, or `None` for containment

    pad
        How much is wanted before and after that

    Returns
    -------
    :
        The window which was needed and whether it is covered, or
        [NeedsEnrichment][esmporium.requirements.NeedsEnrichment] if a threshold
        cannot be placed on `have`'s axis as it stands

    Raises
    ------
    MismatchedTimeAxisError
        `aligned` is not on `have`'s axis, which the mapping rules out, so this is a
        bug

    Notes
    -----
    The window is **anchored** at the alignment point rather than free to slide inside
    the overlap. A sliding window passes cases where the covering run does not reach
    the alignment point at all, and there is no data there to take an anomaly against.
    """
    # Everything below moves along, and compares against, `have`'s axis.
    if not aligned.anchor.axis.is_comparable_with(have.axis):
        raise MismatchedTimeAxisError(have.axis, aligned.anchor.axis)

    anchor, covered_end = aligned.anchor.value, aligned.covered_end.value

    finer = [
        span
        for span in (min_overlap, *pad)
        if span is not None and _too_fine(span, have.axis)
    ]
    if finer:
        one = len(finer) == 1

        return NeedsEnrichment(
            f"{readable_list([str(span) for span in finer], quote=False)} "
            f"{'is' if one else 'are'} not a whole number of steps on a "
            f"{have.axis.units} axis, so {'it does' if one else 'they do'} not name "
            f"{'a position' if one else 'positions'} on it to compare against"
        )

    try:
        needed_end = (
            advance(anchor, min_overlap, have.axis)
            if min_overlap is not None
            else covered_end
        )
        start = retreat(anchor, pad[0], have.axis)
        end_exclusive = advance(needed_end, pad[1], have.axis)
    except (UnknownCalendarError, NoEpochError) as error:
        # A dated axis which does not know its calendar, which is what a range placed
        # on one before any header was read looks like. `MissingCalendarsExtraError`
        # is deliberately *not* caught: an uninstalled extra is an environment problem
        # rather than a fact about the data, so it must not become a solve status.
        return NeedsEnrichment(str(error))

    # In overlap mode there also has to *be* that much data to overlap with, which
    # `_Window.overlap_supplied` checks as positions, so that a length never has to be
    # turned into a count. What has to reach the far edge of the window is the series
    # from the anchor down, so with a run or two in between it is not the covered
    # run's own extent.
    return _Window(
        start=start,
        end_exclusive=end_exclusive,
        axis=have.axis,
        held=have.start <= start and have.end_exclusive >= end_exclusive,
        anchor=anchor,
        covered_end=covered_end,
        overlap=min_overlap,
        overlap_end=needed_end if min_overlap is not None else None,
    )


def _too_fine(span: Span, axis: TimeAxis) -> bool:
    """
    Determine whether a span cannot be placed on an axis as it stands

    Parameters
    ----------
    span
        The length wanted, which is years or months -- anything finer is refused
        before this as a missing feature

    axis
        The axis to place it on

    Returns
    -------
    :
        `True` if the axis has no position `span` away from a given one

        A dated axis can place any length, given its calendar, so this is only ever
        true of a natural one: six months is not a position on a yearly axis, and a
        natural sub-monthly axis has no calendar-free ordinal at all.
    """
    if not axis.units.is_natural:
        return False

    if axis.units.step is TimeStep.MONTHS:
        return False

    if axis.units.step is TimeStep.YEARS:
        months = (
            span.count * MONTHS_PER_YEAR if span.step is TimeStep.YEARS else span.count
        )

        return months % MONTHS_PER_YEAR != 0

    return True


def _described_pad(pad: tuple[Span, Span]) -> str:
    """
    Describe a pair of padding spans for a message a person is going to read

    Parameters
    ----------
    pad
        The padding before and after

    Returns
    -------
    :
        The two, e.g. `(10 years, 10 years)`
    """
    return f"({pad[0]}, {pad[1]})"


def _needs(
    subject: str,
    what: str,
    remedy: str,
    entries: Iterable[CatalogueEntry],
    missing: MissingTimeFact,
) -> NeedsEnrichment:
    """
    Say that a fact has to be recorded before this can be judged

    One spelling for all three facts, so that every `NeedsEnrichment` a `Covers`
    produces reads the same way: what is not recorded, what recording it takes, and
    then the inputs to hand the enricher.

    Parameters
    ----------
    subject
        Role path being judged

    what
        What is not recorded

    remedy
        What recording it would take, which is the actionable half

    entries
        The datasets which have to be enriched

    missing
        Which fact is missing

    Returns
    -------
    :
        The outcome, carrying one input per dataset
    """
    return NeedsEnrichment(
        f"{subject}: {what}, so whether it is covered is not yet known rather than "
        f"known to be no -- {remedy}. Run the enricher over "
        "`result.enrichment_inputs` and solve again.",
        inputs=tuple(
            EnrichmentInput(dataset_id=entry.id, missing=missing) for entry in entries
        ),
    )


# A note on precedence, which the function below spells out in order. `Fail` leads
# because it is the only definite one: a lineage which definitely is not covered cannot
# be made covered by enriching a different lineage, so the failure is the honest
# headline. `NeedsEnrichment` sits below `MissingFeature` for the same reason it does in
# the solver's own priority -- enriching will not make an unsupported requirement
# supported -- and `Degraded` last, because it is the only one which still passes.
def _worst(outcomes: Sequence[Outcome]) -> Outcome:
    """
    Reduce one outcome per lineage to one outcome for the check

    Parameters
    ----------
    outcomes
        One outcome per lineage judged

    Returns
    -------
    :
        The worst of them, with the messages of every outcome of that kind joined

        `NeedsEnrichment` is the one which really has to be joined rather than picked
        from: its whole value is that one call to the enricher clears every dataset
        the solve could not judge, so every input has to survive.
    """
    failed = [outcome for outcome in outcomes if isinstance(outcome, Fail)]
    if failed:
        return Fail(_joined(failed))

    unsupported = [
        outcome for outcome in outcomes if isinstance(outcome, MissingFeature)
    ]
    if unsupported:
        return MissingFeature(_joined(unsupported))

    needed = [outcome for outcome in outcomes if isinstance(outcome, NeedsEnrichment)]
    if needed:
        inputs: dict[EnrichmentInput, None] = {}
        for outcome in needed:
            inputs.update(dict.fromkeys(outcome.inputs))

        return NeedsEnrichment(_joined(needed), inputs=tuple(inputs))

    degraded = [outcome for outcome in outcomes if isinstance(outcome, Degraded)]
    if degraded:
        return Degraded(_joined(degraded))

    # A pass can carry what it assumed, and that has to survive as well.
    commented = [
        outcome for outcome in outcomes if isinstance(outcome, Pass) and outcome.message
    ]

    return Pass(_joined(commented))


def _joined(
    outcomes: Sequence[Pass | Degraded | Fail | MissingFeature | NeedsEnrichment],
) -> str:
    """
    Join the messages of several outcomes of one kind

    Parameters
    ----------
    outcomes
        The outcomes

    Returns
    -------
    :
        Their messages, in the order the lineages were judged, or `""` for none
    """
    return "; ".join(outcome.message for outcome in outcomes)
