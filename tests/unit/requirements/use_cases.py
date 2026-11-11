"""
The real analyses the requirement tree can express, as requirements

One place rather than one per test, because `test_use_cases.py` is parametrised over
`USE_CASES` and so checks every entry here without anyone writing a test for it.

They are kept together, and keyed by their number in the requirements plan, because
what makes a use case worth having is that somebody wants the answer: each is an
analysis we mean to run, written out in full, which is a much harder test of the tree
than any example invented to exercise one feature.
"""

from __future__ import annotations

from esmporium.query import Query
from esmporium.requirements import (
    Ancestors,
    Covers,
    Requirement,
    all_of,
    leaf,
    requirement,
)

# -------------------------------------------------------------- shared vocabulary

CONTROL = ("piControl", "esm-piControl")
"""
The pre-industrial controls, as an alias: both spellings mean "the unforced run"

Which of the two a model published depends on whether the run is concentration- or
emissions-driven, and an analysis which wants the control wants whichever there is.
"""

SCENARIOS = ("ssp126", "ssp245", "ssp370", "ssp585")
"""The core scenarios, which an analysis repeats over rather than needs together"""

PATTERN_SCALING_FIELDS = (
    "tas",
    "tasmax",
    "tasmin",
    "huss",
    "pr",
    "sfcWind",
    "ps",
    "rsds",
    "rlds",
)
"""The fields pattern scaling can be done on, any one of which will do"""

TO_CONTROL = Ancestors(until=Query(experiment=CONTROL), role="control")
"""
Trace a dataset back to the control its simulation branched from

Shared because it is the same relation in every analysis which needs an unforced
baseline, and because the role it files the control under -- `control` -- is the name
checks will refer to.
"""

CONTROL_COVERS = Covers(role="control", target="self", ideal_pad_years=(20.0, 20.0))
"""
The control has to cover the dataset, and ideally 20 years either side of it

What an anomaly from the control needs.
"""

CONTROL_IDEALLY_COVERS = Covers(
    role="control", target="self", pad_years=None, ideal_pad_years=(0.0, 0.0)
)
"""
The control should cover the dataset, but there are workarounds if it does not
"""

MONTHLY = Query(reporting_interval="mon")
"""Monthly data, which is what most of the analyses below use"""

FAN_OUT_OVER_EXPERIMENT = ("model", "variant_label", "experiment")
"""Grouping for an analysis which is repeated for each experiment"""

# -------------------------------------------------------------------- the use cases

TCR = requirement(
    name="tcr",
    tree=(
        leaf(Query(variable="tas"), "tas")
        .where(Query(experiment="1pctCO2"))
        .with_lineage(TO_CONTROL)
        .with_constraints(CONTROL_COVERS)
    ),
    where=MONTHLY,
    group_by=("model", "variant_label"),
)
"""
Transient climate response: warming in 1pctCO2, relative to the control

The first use case with a constraint:
the warming is an anomaly from the control,
so the control has to cover the years of 1pctCO2 which are used.
"""

TCRE_FLAT10 = requirement(
    name="tcre-flat10",
    tree=(
        leaf(Query(variable="tas"), "tas")
        .where(Query(experiment="esm-flat10"))
        .with_lineage(TO_CONTROL)
        .with_constraints(CONTROL_COVERS)
    ),
    where=MONTHLY,
    group_by=("model", "variant_label"),
)
"""
Transient climate response to cumulative emissions, from esm-flat10

The emissions are prescribed, so warming is all that is needed.
"""

TCRE_1PCT = requirement(
    name="tcre-1pct",
    tree=(
        all_of(
            leaf(Query(variable="tas"), "tas"),
            leaf(Query(variable="fgco2"), "fgco2"),
            leaf(Query(variable="nbp"), "nbp"),
        )
        .where(Query(experiment="1pctCO2"))
        .with_lineage(TO_CONTROL)
        .with_constraints(CONTROL_COVERS)
    ),
    where=MONTHLY,
    group_by=("model", "variant_label"),
)
"""
Transient climate response to cumulative emissions, from 1pctCO2

Concentrations are prescribed, so the emissions are inferred from the carbon fluxes,
which are needed together with warming.
One `.with_constraints()` above the `all_of` reaches every leaf,
so each of the three is checked against its own control.
"""

ZEC_FLAT10 = requirement(
    name="zec-flat10",
    tree=(
        leaf(Query(variable="tas"), "tas")
        .where(Query(experiment="esm-flat10-zec"))
        .with_lineage(TO_CONTROL)
        .with_constraints(CONTROL_COVERS)
    ),
    where=MONTHLY,
    group_by=("model", "variant_label"),
)
"""
Zero emissions commitment, from esm-flat10-zec

esm-flat10-zec branches from esm-flat10, not from the control,
so the lineage passes through esm-flat10 on its way back,
and the control has to cover both.
"""

TAS_SCENARIO_ANOMALIES = requirement(
    name="tas-scenario-anomalies",
    tree=(
        leaf(Query(variable="tas"), "tas")
        .where(Query(experiment=SCENARIOS))
        .with_lineage(TO_CONTROL)
        .with_constraints(CONTROL_IDEALLY_COVERS)
    ),
    where=MONTHLY,
    group_by=FAN_OUT_OVER_EXPERIMENT,
)
"""
Warming in each scenario, relative to the control

The scenarios branch from historical,
so the control would ideally cover both,
but an analysis can work around it if it does not,
hence only ideally.
"""


PATTERN_EFFECT = requirement(
    name="pattern-effect",
    tree=leaf(
        Query(variable="tas", experiment="historical", reporting_interval="mon"),
        "tas",
    ),
    group_by=("model", "variant_label"),
)
"""
The simplest use case there is: one dataset, in one role

Historical near-surface air temperature, one model and variant at a time. There
is nothing optional about it, nothing to choose between, and no second dataset
for it to relate to.
"""

PATTERN_SCALING = requirement(
    name="pattern-scaling",
    tree=(
        leaf(Query(variable=PATTERN_SCALING_FIELDS), "field")
        .where(Query(experiment=SCENARIOS))
        .with_lineage(TO_CONTROL)
    ),
    where=Query(reporting_interval="mon"),
    group_by=("model", "variant_label", "experiment", "variable"),
)
"""
Scale a field against global mean temperature, relative to the control

The use case which shows that a role is not a variable. Nine fields, and the analysis
wants one at a time rather than all nine together, so they are **one leaf** whose query
is an "or" and whose role says what the dataset is *for*. `variable` is in `group_by`,
which is what turns the nine into nine groups.

The lineage is the other half of it: each field is an anomaly *from* something, so every
chosen dataset is traced back to its own control, filling `control.field` beside
`field`.
"""

ETCCDI = requirement(
    name="etccdi",
    tree=(
        all_of(
            leaf(Query(variable="tasmax"), "tasmax"),
            leaf(Query(variable="tasmin"), "tasmin"),
            leaf(Query(variable="pr"), "pr"),
        )
        .where(Query(experiment=SCENARIOS))
        .with_lineage(TO_CONTROL)
    ),
    where=Query(reporting_interval="day"),
    group_by=("model", "variant_label", "experiment"),
)
"""
The ETCCDI climate extremes indices, each against its own control

Three variables the analysis needs *together*, so three leaves, and the counterpart to
`PATTERN_SCALING` on exactly that point.

What it adds is that one `.with_lineage()` above an `all_of` reaches every leaf below
it: each of the three is traced back to its own control, giving `control.tasmax`,
`control.tasmin` and `control.pr`, rather than the three sharing one.
"""

USE_CASES: dict[str, Requirement] = {
    "01-tcr": TCR,
    "03-tcre-flat10": TCRE_FLAT10,
    "04-tcre-1pct": TCRE_1PCT,
    "05-zec-flat10": ZEC_FLAT10,
    "11-tas-scenario-anomalies": TAS_SCENARIO_ANOMALIES,
    "14-pattern-effect": PATTERN_EFFECT,
    "15-pattern-scaling": PATTERN_SCALING,
    "17-etccdi": ETCCDI,
}
"""
The use cases the tree can express, by their number in the requirements plan

Gaps in the numbering are the analyses whose requirements need something the tree
cannot say yet, so the keys are deliberately not consecutive.
"""
