"""
[Covers][(m).Covers]: one dataset in a lineage has to span another

The first constraint esmporium ships,
and the worked example of a constraint which needs more than the
[Catalogue][esmporium.requirements.Catalogue] protocol provides:
it needs time ranges and branch times,
so it defines [CatalogueWithTimeInfo][(m).CatalogueWithTimeInfo] beside itself
and is a `Constraint[CatalogueWithTimeInfo]`.
See [esmporium.requirements.constraints][] for the pattern.

All times are fractional years.
Calendars are not handled:
a 360-day year and a Gregorian one are both one year here.
"""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from esmporium.requirements.catalogue import Catalogue, CatalogueEntry
from esmporium.requirements.constraints import (
    ConstraintView,
    Degraded,
    Fail,
    Outcome,
    Pass,
)
from esmporium.requirements.relations import Lineage


class CatalogueWithTimeInfo(Catalogue, Protocol):
    """
    A catalogue which can say when a dataset's data is and where it branched from

    What [Covers][(m).Covers] needs.
    Neither [InMemoryCatalogue][esmporium.requirements.InMemoryCatalogue]
    nor [DatabaseCatalogue][esmporium.db.DatabaseCatalogue] provides it yet,
    because nothing records the time range of a dataset yet.

    This is also the place to correct recorded branch times,
    which often have bugs:
    wrap a catalogue and override
    [get_branch_time_in_parent][(m).CatalogueWithTimeInfo.get_branch_time_in_parent].
    """

    # @Anna work for you: think about whether returning fractional years is the best
    # or whether it would be better to have
    # multiple methods e.g. get_time_range_years and get_time_range_datetime
    # to differentiate whether the user wants years as floats or datetimes
    # (maybe even cf datetimes so the calendar can be specified too).
    def get_time_range(self, entry: CatalogueEntry) -> tuple[float, float]:
        """
        Get the span of a dataset's data

        Parameters
        ----------
        entry
            Dataset to get the span of

        Returns
        -------
        :
            `(start, end)` in fractional years of the dataset's own time axis

            `end` is exclusive.
        """
        ...

    def get_branch_time_in_parent(self, entry: CatalogueEntry) -> float:
        """
        Get where a dataset branched from its parent

        Parameters
        ----------
        entry
            Dataset to get the branch time of

        Returns
        -------
        :
            The time in the parent's time axis (fractional years)
            which corresponds to the start of `entry`'s data,
            i.e. to the start of its
            [time range][(m).CatalogueWithTimeInfo.get_time_range]
        """
        ...


class Covers(BaseModel):
    """
    One dataset in a lineage has to cover the span of another

    For example, `Covers(role="control", target="self")` requires each dataset's control
    to cover the dataset's span,
    which is what an anomaly from the control needs.

    Attach it to the leaf whose lineage it judges, or above it.
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
    """The covered dataset, named the same way as `role`"""

    align: Literal["branch", "calendar"] = "branch"
    """
    How to line up the time axes of `role` and `target`

    With `"branch"`, the spans of `target` and every dataset
    between `target` and `role` in the lineage
    are mapped into `role`'s time axis using the branch times.
    `role` therefore has to be an ancestor of `target`.

    With `"calendar"`, the time ranges are compared directly.
    """

    pad_years: tuple[float, float] | None = (0.0, 0.0)
    """
    Years `role` must cover before and after the span it is compared against

    If `None`, nothing is required,
    and the span is only checked against
    [ideal_pad_years][(m).Covers.ideal_pad_years].
    """

    ideal_pad_years: tuple[float, float] | None = None
    """
    Years `role` should ideally cover before and after the span it is compared against

    If this is not met but [pad_years][(m).Covers.pad_years] is,
    the outcome is [Degraded][esmporium.requirements.Degraded].
    """

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
            Catalogue, used to get time ranges and branch times

        Returns
        -------
        :
            [Fail][esmporium.requirements.Fail] on the first lineage
            which does not meet `pad_years`,
            otherwise [Degraded][esmporium.requirements.Degraded]
            if any lineage does not meet `ideal_pad_years`,
            otherwise [Pass][esmporium.requirements.Pass]

        Raises
        ------
        ValueError
            `role` or `target` is not in a lineage,
            or `align` is `"branch"` and `role` is not an ancestor of `target`
        """
        if not view.lineages:
            return Fail("Covers needs a lineage, but none was resolved")

        degraded: list[str] = []
        for leaf_role, lineages in sorted(view.lineages.items()):
            for lineage in lineages:
                need_start, need_end = self._needed_span(lineage, catalogue)
                covering = lineage.members[lineage.index_of(self.role)]
                have_start, have_end = catalogue.get_time_range(covering)

                description = (
                    f"{view.prefix}{leaf_role}: #{covering.id} covers "
                    f"[{have_start:g}, {have_end:g}), "
                    f"needed [{need_start:g}, {need_end:g})"
                )
                if self.pad_years is not None and not _covers(
                    have_start, have_end, need_start, need_end, self.pad_years
                ):
                    return Fail(f"{description} with padding {self.pad_years}")

                if self.ideal_pad_years is not None and not _covers(
                    have_start, have_end, need_start, need_end, self.ideal_pad_years
                ):
                    degraded.append(
                        f"{description}, ideal padding {self.ideal_pad_years} not met"
                    )

        if degraded:
            return Degraded("; ".join(degraded))

        return Pass()

    def _needed_span(
        self, lineage: Lineage, catalogue: CatalogueWithTimeInfo
    ) -> tuple[float, float]:
        """
        Get the span `role` has to cover, in `role`'s time axis

        Parameters
        ----------
        lineage
            The lineage being judged

        catalogue
            Catalogue, used to get time ranges and branch times

        Returns
        -------
        :
            `(start, end)`, before any padding

        Raises
        ------
        ValueError
            `align` is `"branch"` and `role` is not an ancestor of `target`
        """
        # Note: we'll have to make the time handling clearer.
        # In particular to make it easy for users to see how to override
        # given branch time values (these often have bugs).
        # For now, the way to do it is to wrap the catalogue
        # (see `CatalogueWithTimeInfo`).
        members = lineage.members
        target_index = lineage.index_of(self.target)
        role_index = lineage.index_of(self.role)
        if self.align == "calendar":
            return catalogue.get_time_range(members[target_index])

        if target_index >= role_index:
            msg = (
                f"With align='branch', {self.role!r} must be an ancestor "
                f"of {self.target!r}"
            )
            raise ValueError(msg)

        # Every dataset from `target` up to (not including) `role` has to be covered,
        # not just `target`:
        # e.g. a ZEC run branches from 1pctCO2, which branches from the control,
        # and the analysis needs the control over both.
        starts: list[float] = []
        ends: list[float] = []
        for member_index in range(target_index, role_index):
            start, end = catalogue.get_time_range(members[member_index])
            # Map this member's span up the lineage, one parent at a time,
            # until it is in the role's time axis
            for step in range(member_index, role_index):
                step_start, _ = catalogue.get_time_range(members[step])
                branch = catalogue.get_branch_time_in_parent(members[step])
                start, end = branch + (start - step_start), branch + (end - step_start)

            starts.append(start)
            ends.append(end)

        return min(starts), max(ends)


def _covers(
    have_start: float,
    have_end: float,
    need_start: float,
    need_end: float,
    pad: tuple[float, float],
) -> bool:
    """
    Determine whether one span covers another, with padding

    Parameters
    ----------
    have_start
        Start of the covering span

    have_end
        End of the covering span

    need_start
        Start of the span to cover

    need_end
        End of the span to cover

    pad
        Years to additionally cover before and after the span to cover

    Returns
    -------
    :
        `True` if the covering span covers the padded span
    """
    return have_start <= need_start - pad[0] and have_end >= need_end + pad[1]
