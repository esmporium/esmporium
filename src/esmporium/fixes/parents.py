"""
Corrected parent information, for the simulations whose published metadata is wrong

Reading a header carefully is not always enough, because some files simply do not say
what they need to. CMIP5 supplies the clearest cases: `inmcm4` publishes no
`parent_experiment_rip` at all, and `CMCC-CM` publishes the string `"N/A"` for one, so
neither can name the *variant* of the run it branched from however well its header is
read. A correction has to come from somewhere else.

**What is built here is the mechanism, not a database of fixes.** There is a
[ParentFixes][(m).ParentFixes] seam, a [ParentFix][(m).ParentFix] that cannot be stated
without saying why it is believed, and [PARENT_FIXES][(m).PARENT_FIXES] holding the
handful we have evidence for. A file format for fixes -- something a user could edit,
or that could be published for others to use -- is deliberately **not** here yet: a
format is the one part that is hard to change once people have written files against
it, and the shape of the evidence is still being learned.

**Why `evidence` is a required field.** Because the obvious way to write a fix is also
wrong. If a model publishes exactly one `piControl` variant and a child points at a
different one, it is tempting to call the single option the answer. It is not: the real
parent may simply never have been published. A fix is only *not* an assumption when
either the modelling centre has confirmed it, or the data has been compared and lines
up. Anything else is a guess, and has to be able to say so.

A fix is written as a [FIX][esmporium.db.schema.ParentInfoSource.FIX] row, which is a
separate row from the header that it corrects --
[SOURCE_PRECEDENCE][esmporium.db.SOURCE_PRECEDENCE] puts a fix above a header and a
header above the search index. So a correction survives every re-ingest and every
re-read of the file it corrects, and the thing it disagrees with stays visible beside
it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from esmporium.db import ParentSimulation
from esmporium.search import ParentInfo

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True)
class ParentFix:
    """
    One correction to what a simulation says its parent is, and why we believe it
    """

    info: ParentInfo
    """
    The parent as it should be recorded

    A whole [ParentInfo][esmporium.search.ParentInfo] rather than only the fields being
    corrected, because this is written as its own source and a source says everything
    it knows. The merge rule is the same as for every other source -- asserted wins,
    silence changes nothing -- so a field left `None` here keeps whatever the header
    said rather than erasing it.
    """

    reason: str
    """
    What is wrong with what was published

    e.g. `"the header publishes no parent_experiment_rip"`. Says what the fix is *for*,
    which is what makes it reviewable later when the published metadata may have
    changed.
    """

    evidence: str
    """
    Why the correction is believed, including when it is only an assumption

    **Required, and required to be honest.** The one thing that must not happen is a
    guess recorded as a certainty: a fix outranks the file itself, so an unfounded one
    is worse than no fix at all. If the grounds are "this is the only variant
    published", say so *and* say that it is an assumption -- the real parent may never
    have been published at all.
    """


class ParentFixes(Protocol):
    """
    Where a corrected parent comes from, for a simulation whose published one is wrong

    A seam rather than a lookup, so that where fixes *live* can change without anything
    that uses them changing. Today the only implementation is
    [fixes_from_mapping][(m).fixes_from_mapping] over
    [PARENT_FIXES][(m).PARENT_FIXES]; a later one reading a file a user can edit will
    satisfy the same protocol and need no caller to be touched.
    """

    def __call__(self, simulation: ParentSimulation) -> ParentFix | None:
        """
        Get the correction for one simulation, if there is one

        Parameters
        ----------
        simulation
            The simulation whose parent may need correcting

        Returns
        -------
        :
            The fix, or `None` if this simulation needs none

            `None` is the overwhelmingly common answer and is not a failure: almost
            every simulation's published parent is fine.
        """


def no_parent_fixes(simulation: ParentSimulation) -> ParentFix | None:
    """
    Correct nothing, whatever is asked about

    The default, and a valid [ParentFixes][(m).ParentFixes]. Means "trust the headers",
    which is right until a specific simulation has been shown to need otherwise.

    Parameters
    ----------
    simulation
        The simulation being asked about. Ignored.

    Returns
    -------
    :
        Always `None`
    """
    return None


def fixes_from_mapping(fixes: Mapping[ParentSimulation, ParentFix]) -> ParentFixes:
    """
    Build a [ParentFixes][(m).ParentFixes] from a mapping of them

    Parameters
    ----------
    fixes
        The corrections, keyed by the simulation each corrects

    Returns
    -------
    :
        A lookup over `fixes`

    Examples
    --------
    >>> simulation = ParentSimulation(
    ...     project="CMIP5",
    ...     model="INM-CM4",
    ...     experiment="abrupt4xCO2",
    ...     variant_label="r1i1p1",
    ... )
    >>> lookup = fixes_from_mapping(PARENT_FIXES)
    >>> lookup(simulation).info.parent_variant_label
    'r1i1p1'

    A simulation with nothing wrong with it gets nothing back:

    >>> print(
    ...     lookup(ParentSimulation("CMIP6", "UKESM1-0-LL", "historical", "r1i1p1f2"))
    ... )
    None
    """

    def look_up(simulation: ParentSimulation) -> ParentFix | None:
        return fixes.get(simulation)

    return look_up


def _missing_cmip5_variant_label(
    *, model: str, experiment: str, variant_label: str, parent_experiment: str
) -> tuple[ParentSimulation, ParentFix]:
    """
    Build the fix for a CMIP5 run whose header does not name its parent's variant

    The one shape of CMIP5 fix we have so far, and a factory rather than four copied
    literals so that the *reason* and the *evidence* are stated once and cannot drift
    apart between entries.

    Parameters
    ----------
    model
        The child's model, as the search index spells it

    experiment
        The child's experiment

    variant_label
        The child's variant label

    parent_experiment
        The parent experiment the header does name

    Returns
    -------
    :
        The simulation and its fix, ready to put in a mapping
    """
    simulation = ParentSimulation(
        project="CMIP5",
        model=model,
        experiment=experiment,
        variant_label=variant_label,
    )

    return simulation, ParentFix(
        info=ParentInfo(
            parent_project="CMIP5",
            parent_activity=None,
            parent_model=model,
            parent_experiment=parent_experiment,
            # The correction itself: the child's own variant label, which is the only
            # one this model published.
            parent_variant_label=variant_label,
            parent_time_units=None,
        ),
        reason=(
            "the CMIP5 header names the parent experiment but not the parent's "
            "variant: it publishes no usable parent_experiment_rip, so the parent "
            "cannot be narrowed to one run"
        ),
        evidence=(
            f"ASSUMPTION, not confirmed. {model} publishes a single real variant "
            f"({variant_label}) of {parent_experiment} on ESGF, so it is the only "
            "candidate among published data -- which is not the same as being the "
            "parent, since the actual parent run may never have been published. "
            "Checked 2026-11-05. To settle it, confirm with the modelling centre, or "
            "compare the two runs' values across the branch and show they line up."
        ),
    )


PARENT_FIXES: Mapping[ParentSimulation, ParentFix] = dict(
    [
        # `inmcm4` -- spelled `INM-CM4` by CMIP5's search index, see
        # `esmporium.db.read_cmip5_parent_header` -- publishes no
        # `parent_experiment_rip` attribute at all, in either of these experiments.
        _missing_cmip5_variant_label(
            model="INM-CM4",
            experiment="abrupt4xCO2",
            variant_label="r1i1p1",
            parent_experiment="piControl",
        ),
        _missing_cmip5_variant_label(
            model="INM-CM4",
            experiment="rcp85",
            variant_label="r1i1p1",
            parent_experiment="historical",
        ),
        # `CMCC-CM` publishes the literal string `"N/A"` instead.
        _missing_cmip5_variant_label(
            model="CMCC-CM",
            experiment="rcp85",
            variant_label="r1i1p1",
            parent_experiment="historical",
        ),
    ]
)
"""
The corrections we have, keyed by the simulation each corrects

Three, all of them CMIP5, and all of them the same shape: a run whose header names its
parent experiment but not the parent's variant. Every one is marked an **assumption**
in its [evidence][esmporium.fixes.ParentFix.evidence], because the grounds are "this is
the only variant published" and that is not proof -- see this module's docstring.

Deliberately short. A table of fixes is worth building up and perhaps publishing one
day, and the way to build it is from cases that have actually been met and checked, not
by filling in everything that looks plausible.
"""
