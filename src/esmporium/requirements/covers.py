"""
Covers: one dataset in a lineage has to span another

A check which needs more than the
[Catalogue][esmporium.requirements.Catalogue] protocol provides -- time ranges and
branch points -- so it defines [CatalogueWithTimeInfo][(m).CatalogueWithTimeInfo]
beside itself and is a `Constraint[CatalogueWithTimeInfo]`. See
[esmporium.requirements.constraints][] for the pattern.

Every position here is a whole number of steps on a stated
[TimeAxis][esmporium.datasets.TimeAxis], never a fractional year, and every range is
half-open ([TimeRange][esmporium.datasets.TimeRange]). Resolving a length such as
"twenty years" against an axis is [esmporium.datasets.calendars][]'s job.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, model_validator

from esmporium.datasets import (
    MONTHS_PER_YEAR,
    BranchPoint,
    NoEpochError,
    Span,
    TimeAxis,
    TimeRange,
    TimeStep,
    UnknownCalendarError,
    UnknownProjectTimeAxisError,
    VersionTimeRange,
    advance,
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


# A note for developers, recording a decision so that step 14 does not optimise it
# away. `Covers` and the `SameTimeRange` which is still to come look like one check
# with a flag, and they are not:
#
#   Covers, align="branch"      ancestry. The axes do *not* align, and the branch
#                               point is what resolves that. No branch point means
#                               `NeedsEnrichment`, not a failure.
#   Covers, align="calendar"    the axes are compared directly, and never asks for a
#                               branch point at all.
#   SameTimeRange               siblings, or any pair on one axis. There *is* no
#                               branch point to ask for, so axes which do not line up
#                               are a loud `Fail`: it is a question we can answer and
#                               the answer is no.
#
# So `align="calendar"` is not "SameTimeRange spelled differently", and merging the
# two would lose the distinction between "misaligned, which is expected here" and
# "misaligned, which means something is wrong with the data".


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

    def get_branch_time_in_parent(self, entry: CatalogueEntry) -> BranchPoint | None:
        """
        Get where a dataset branched from its parent

        Parameters
        ----------
        entry
            Dataset to get the branch point of

        Returns
        -------
        :
            Where the start of `entry`'s data falls on its *parent's* axis, with the
            axis it is a position on -- or `None` if nothing has recorded it

            A CMIP5 branch time is published without units and so cannot be
            interpreted as it stands; such a dataset answers `None` until a fix
            records the interpreted value.
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
    `Covers(role="control", align="branch", mode="containment", pad=(Span(0,
    TimeStep.YEARS), Span(0, TimeStep.YEARS)))`
    requires each dataset's control to cover the dataset's span, which is what an
    anomaly from the control needs.

    Attach it to the leaf whose lineage it judges, or above it.

    **Nothing is defaulted.** `align`, `mode` and `pad` all have to be stated, every
    time. That follows the rule
    [Requirement.group_by][esmporium.requirements.Requirement.group_by] already sets
    -- a decision is stated rather than guessed -- and it is forced anyway, because
    containment cannot be written as a number: nobody knows a dataset's extent until
    it has been recorded.
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

    align: Literal["branch", "calendar"]
    """
    How to line up the time axes of `role` and `target`

    With `"branch"`, `target`'s span is mapped into `role`'s axis through the branch
    points, one branch at a time, so `role` has to be an ancestor of `target`.
    Coverage is then judged along the whole series: the run `role` holds has to span
    `target` *and* every parent between the two. With `"calendar"`, the two ranges are
    compared directly and no branch point is ever asked for.

    For example, ZEC experiment branches off 1pctCO2 branches of piControl. Coverage
    has to span from ZEC to piControl in a multi-hop parent coverage requirement.
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
            `role` or `target` is not in a lineage, or `align` is `"branch"` and
            `role` is not an ancestor of `target`. Both are mistakes in the
            requirement, so they stop rather than being reported.
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
        # Only the methods this check will actually call: `align="calendar"` never
        # asks for a branch point, so a catalogue which knows ranges and not branch
        # points can serve it.
        needed = ["get_time_range"]
        if self.align == "branch":
            needed.append("get_branch_time_in_parent")

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
            `target` under `align="branch"`
        """
        members = lineage.members
        target_index = lineage.index_of(self.target)
        role_index = lineage.index_of(self.role)
        covering = members[role_index]

        if self.align == "branch":
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
            covering=covering,
            have=ranges[-1].range,
            anchor=aligned.anchor,
            covered_end=aligned.covered_end,
            subject=subject,
        )

    def _involved(
        self, members: Sequence[CatalogueEntry], target_index: int, role_index: int
    ) -> tuple[CatalogueEntry, ...]:
        """
        Get the datasets this check reads, covered one first and covering one last

        Under `align="branch"` that is the chain from the covered dataset up to the
        covering one, which is the series coverage is judged along. A member of the
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

            Just the pair under `align="calendar"`, which compares two ranges
            directly and so never looks at what is between them.
        """
        if self.align == "calendar":
            return (members[target_index], members[role_index])

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
                f"With align='branch', {self.role!r} must be an ancestor "
                f"of {self.target!r}"
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

        **After mapping, the series the covered run belongs to starts at the alignment
        point.** That is the one piece of arithmetic coverage needs, and it is what
        lets [_covers][(m)._covers] answer containment and overlap with one
        comparison, swapping only the upper bound.

        Parameters
        ----------
        catalogue
            Catalogue, asked for one branch point per hop under `align="branch"`

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
        axis = ranges[-1].range.axis
        covered = ranges[0]
        if self.align == "calendar":
            if not covered.range.axis.is_comparable_with(axis):
                return Fail(_mismatched(subject, covered.range.axis, axis))

            return _Alignment(covered.range.start, covered.range.end_exclusive)

        branches = self._branch_points(catalogue, involved, subject)
        if isinstance(branches, NeedsEnrichment):
            return branches

        axes = self._check_the_axes(involved, ranges, branches, subject)
        if axes is not None:
            return axes

        joins = self._check_the_joins(involved, ranges, branches, subject)
        if joins is not None:
            return joins

        return _aligned_through(ranges, branches)

    def _branch_points(
        self,
        catalogue: CatalogueWithTimeInfo,
        involved: Sequence[CatalogueEntry],
        subject: str,
    ) -> tuple[BranchPoint, ...] | NeedsEnrichment:
        """
        Get where each dataset involved branched from the next one up

        Parameters
        ----------
        catalogue
            Catalogue to ask

        involved
            The datasets this check reads, as [_involved][(c).Covers._involved] gives
            them

        subject
            Role path being judged, used in the message

        Returns
        -------
        :
            One branch point per hop, so one fewer than there are datasets, each a
            position on the axis of the dataset above it -- or
            [NeedsEnrichment][esmporium.requirements.NeedsEnrichment] naming every
            dataset whose branch point is not recorded
        """
        points: list[BranchPoint] = []
        unrecorded: list[CatalogueEntry] = []
        for child in involved[:-1]:
            point = catalogue.get_branch_time_in_parent(child)
            if point is None:
                unrecorded.append(child)
            else:
                points.append(point)

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
                "them up, so there is nothing to compare without it",
                unrecorded,
                MissingTimeFact.BRANCH_POINT,
            )

        return tuple(points)

    def _check_the_axes(
        self,
        involved: Sequence[CatalogueEntry],
        ranges: Sequence[VersionTimeRange],
        branches: Sequence[BranchPoint],
        subject: str,
    ) -> Outcome | None:
        """
        Check each hop's branch point and the two runs it joins are on one axis

        Parameters
        ----------
        involved
            The datasets this check reads, as [_involved][(c).Covers._involved] gives
            them

        ranges
            Their ranges, in the same order

        branches
            Where each of them branched from the next one up

        subject
            Role path being judged, used in every message

        Returns
        -------
        :
            Why a hop cannot be mapped, or `None` when every one can
        """
        for index, branch in enumerate(branches):
            child, parent = involved[index], involved[index + 1]
            child_axis = ranges[index].range.axis
            parent_axis = ranges[index + 1].range.axis

            # A control and an experiment on different calendars fail here,
            # because the calendar check is strict.
            # That may need loosening for real data at scale:
            # see the note in `TimeAxis.is_comparable_with`.
            if not branch.parent_axis.is_comparable_with(parent_axis):
                # The branch point and the parent's own data disagree about the axis
                # the parent is on, which is the data disagreeing with itself rather
                # than a question about alignment. Loud, per settled decision 11.
                return Fail(
                    f"{subject}: the branch point of #{child.id} is a position on a "
                    f"{branch.parent_axis.units} axis in calendar "
                    f"{branch.parent_axis.calendar!r}, but #{parent.id}'s own data is "
                    f"on a {parent_axis.units} axis in calendar "
                    f"{parent_axis.calendar!r}. The two have to be the same axis for "
                    "the branch point to mean anything."
                )

            if child_axis.units.step is not parent_axis.units.step:
                return MissingFeature(
                    f"{subject}: #{child.id} reports every "
                    f"{child_axis.units.step.value.rstrip('s')} and #{parent.id} "
                    f"every {parent_axis.units.step.value.rstrip('s')}, and comparing "
                    "a run against a parent at a different frequency is not supported "
                    "yet. It is a fair thing to want -- the two spans are the same "
                    "lengths written in different units -- so please say you wanted "
                    f"it at {ISSUE_TRACKER_URL}."
                )

        return None

    def _check_the_joins(
        self,
        involved: Sequence[CatalogueEntry],
        ranges: Sequence[VersionTimeRange],
        branches: Sequence[BranchPoint],
        subject: str,
    ) -> Outcome | None:
        """
        Check each run in between branched where its own parent has data

        Coverage is judged along the whole series, so if there is a multi-hop parent
        chain required for the analysis, the runs in between have to join
        up: one which branched before its parent's data starts, or after it ends,
        leaves a hole no amount of covering can fill. The hop onto the covering
        dataset itself is left out, because whether *it* holds data at the branch point
        is the coverage question [_compare][(c)._compare] answers, with the window it
        needed in the message.

        Parameters
        ----------
        involved
            The datasets this check reads, as [_involved][(c).Covers._involved] gives
            them

        ranges
            Their ranges, in the same order

        branches
            Where each of them branched from the next one up

        subject
            Role path being judged, used in the message

        Returns
        -------
        :
            Why the series does not join up, or `None` when it does
        """
        broken = []
        for index in range(len(branches) - 1):
            child, parent = involved[index], involved[index + 1]
            held = ranges[index + 1].range
            at = branches[index].in_parent

            if not held.start <= at < held.end_exclusive:
                broken.append(
                    f"#{child.id} branched from #{parent.id} at {at}, which is "
                    f"outside the [{held.start}, {held.end_exclusive}) #{parent.id} "
                    "holds"
                )

        if not broken:
            return None

        return Fail(
            f"{subject}: {readable_list(broken, quote=False)}. Coverage is judged "
            "along the whole series from the covering dataset down, so a run which "
            "branched where its parent has no data breaks it."
        )

    def _compare(
        self,
        *,
        covering: CatalogueEntry,
        have: TimeRange,
        anchor: float,
        covered_end: float,
        subject: str,
    ) -> Outcome:
        """
        Compare what the covering dataset holds against what is needed

        Parameters
        ----------
        covering
            The covering dataset, named in every message

        have
            What it holds

        anchor
            Where the two runs line up, on `have`'s axis

        covered_end
            The covered run's mapped `end_exclusive`, on `have`'s axis

        subject
            Role path being judged, used in every message

        Returns
        -------
        :
            `Fail` if the hard requirement is not met, `Degraded` if the ideal is not,
            `Pass` otherwise -- or `NeedsEnrichment` if the comparison needs a finer
            axis than the data is recorded on
        """
        if self.pad is not None:
            met = _covers(have, anchor, covered_end, self.min_overlap, self.pad)
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
                    f"{self._described(subject, covering, have, met)}, with padding "
                    f"{_described_pad(self.pad)}"
                )

        if self.ideal_min_overlap is None and self.ideal_pad is None:
            return Pass()

        # An unstated ideal falls back to the hard requirement, so the soft check is
        # never looser than the hard one -- an `ideal_min_overlap` on its own would
        # otherwise quietly drop the padding which *is* required.
        ideal_overlap = self.ideal_min_overlap or self.min_overlap
        ideal_pad = self.ideal_pad
        if ideal_pad is None:
            ideal_pad = self.pad or NO_PADDING

        ideally = _covers(have, anchor, covered_end, ideal_overlap, ideal_pad)
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
                f"{self._described(subject, covering, have, ideally)}, ideal padding "
                f"{_described_pad(ideal_pad)} not met"
            )

        return Pass()

    def _described(
        self,
        subject: str,
        covering: CatalogueEntry,
        have: TimeRange,
        needed: _Window,
    ) -> str:
        """
        Describe what the covering dataset holds against what was wanted

        One spelling, used by both the hard and the soft message, because two would be
        free to drift and a reader comparing a `Fail` with a `Degraded` is comparing
        the same two numbers.

        Parameters
        ----------
        subject
            Role path being judged

        covering
            The covering dataset

        have
            What it holds

        needed
            What was wanted

        Returns
        -------
        :
            The description, e.g.
            `tas: #2 covers [0, 200) and needs [90, 260), in years`
        """
        return (
            f"{subject}: #{covering.id} covers "
            f"[{have.start}, {have.end_exclusive}) and needs "
            f"[{needed.start}, {needed.end_exclusive}), in {have.axis.units}"
        )


@dataclass(frozen=True)
class _Alignment:
    """
    Where two runs line up, once the covered one has been mapped

    A type rather than a pair of ints so that
    [_align][(c).Covers._align] can answer either this or an outcome and the caller
    can tell which it got by name.
    """

    anchor: float
    """
    Where the series line up, on the covering dataset's axis

    Under `align="branch"` that is where the run directly below the covering dataset
    branched off it, which is the first step of its data the series below it runs
    alongside.
    """

    covered_end: float
    """The covered run's mapped `end_exclusive`, on the same axis"""


# TODO future:
# Other anchors are coherent and not supported. "piControl has to overlap my scenario,
# and I do not care about the historical run it came through" is anchored at the covered
# run's own mapped start rather than at the bottom of the chain, and `min_overlap`
# could be wanted from either. Saying which is a field on `Covers`, so both wait for
# somebody to ask for one.
def _aligned_through(
    ranges: Sequence[VersionTimeRange], branches: Sequence[BranchPoint]
) -> _Alignment:
    """
    Map the covered run onto the covering dataset's axis, one branch at a time

    ```text
                     BP
                      │
      covering |──────┼──────────────────────────────|
                      │
      in between      |═══════════════════|
      covered                     |═══════════════|
    ```

    `BP` is where the run directly below the covering dataset branched off it, and it
    is the alignment point. Each run in between then contributes how far into its own
    parent it branched, counted from that parent's own start, so the covered run's
    start lands wherever the chain of branches puts it.

    Parameters
    ----------
    ranges
        Ranges of the datasets involved, covered one first and covering one last

    branches
        Where each of them branched from the next one up, one per hop

    Returns
    -------
    :
        The alignment point and the covered run's mapped `end_exclusive`
    """
    # TODO (PR R2 in docs/further-background/enrichment-storage.md):
    # this maps through each run's own first step,
    # which assumes its data starts exactly at its branch
    # and shifts everything above it when a leading file is missing.
    # Map through `branch_time_in_child` instead,
    # checking the axes of what is combined at each step.
    anchor = branches[-1].in_parent

    position = anchor
    for index in reversed(range(len(branches) - 1)):
        position += branches[index].in_parent - ranges[index + 1].range.start

    return _Alignment(anchor, position + ranges[0].range.extent.count)


@dataclass(frozen=True)
class _Window:
    """
    The window a comparison asked for, and whether it was covered

    A type rather than a tuple so that the three things it carries are named where
    they are read, which is in two message formats and one `if`.
    """

    start: float
    """First step which has to be covered"""

    end_exclusive: float
    """First step after the last which has to be covered"""

    covered: bool
    """Whether the covering dataset holds all of it"""


def _covers(
    have: TimeRange,
    anchor: float,
    covered_end: float,
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

    anchor
        Where the two runs line up, on `have`'s axis

    covered_end
        The covered run's mapped `end_exclusive`, on `have`'s axis

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

    Notes
    -----
    The window is **anchored** at the alignment point rather than free to slide inside
    the overlap. A sliding window passes cases where the covering run does not reach
    the alignment point at all, and there is no data there to take an anomaly against.
    """
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

    # In overlap mode there also has to *be* that much data to overlap with, which is
    # written as positions so that a length never has to be turned into a count. What
    # has to reach the far edge of the window is the series from the anchor down, so
    # with a run or two in between it is not the covered run's own extent.
    enough_to_overlap = min_overlap is None or covered_end >= needed_end

    return _Window(
        start=start,
        end_exclusive=end_exclusive,
        covered=(
            enough_to_overlap
            and have.start <= start
            and have.end_exclusive >= end_exclusive
        ),
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


def _mismatched(subject: str, left: TimeAxis, right: TimeAxis) -> str:
    """
    Say that two ranges which have to be on one axis are not

    Parameters
    ----------
    subject
        Role path being judged

    left
        Axis of the covered range

    right
        Axis of the covering range

    Returns
    -------
    :
        The message
    """
    return (
        f"{subject}: these two are not on comparable axes -- {left.units} in calendar "
        f"{left.calendar!r} against {right.units} in calendar {right.calendar!r} -- so "
        "with align='calendar' there is nothing to compare. Calendar alignment "
        "compares the two ranges directly, so they have to be on one axis already; "
        "align='branch' is how two runs on different axes are lined up."
    )


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

    return Pass()


def _joined(
    outcomes: Sequence[Degraded | Fail | MissingFeature | NeedsEnrichment],
) -> str:
    """
    Join the messages of several outcomes of one kind

    Parameters
    ----------
    outcomes
        The outcomes, at least one

    Returns
    -------
    :
        Their messages, in the order the lineages were judged
    """
    return "; ".join(outcome.message for outcome in outcomes)
