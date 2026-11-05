"""
Tests of the fixes themselves, and of looking one up

Pure: nothing here touches a database or a network. How a fix gets *applied* is the
database layer's business and is tested in `tests/unit/db/test_parent_headers.py`,
beside the provider that applies it.

Mostly about the mechanism rather than about the three corrections, which is the point:
a fix is a claim about the world and will be reviewed by a person, where the machinery
around one has to be right regardless of what is in it.
"""

from __future__ import annotations

from esmporium.db import (
    ParentSimulation,
)
from esmporium.fixes import (
    PARENT_FIXES,
    fixes_from_mapping,
    no_parent_fixes,
)

NODE = "esgf.example.org"
INMCM4 = ParentSimulation(
    project="CMIP5", model="INM-CM4", experiment="abrupt4xCO2", variant_label="r1i1p1"
)


class TestTheShippedFixes:
    def test_every_fix_says_why_it_is_believed(self):
        """
        The cheap test that stops a silent guess being added later

        A fix outranks the file itself, so an unfounded one is worse than no fix. The
        required field is only worth having if something checks it was filled in.
        """
        for simulation, fix in PARENT_FIXES.items():
            assert fix.reason.strip(), simulation
            assert fix.evidence.strip(), simulation

    def test_a_fix_resting_on_an_assumption_says_so(self):
        """
        "This is the only variant published" is not proof that it is the parent

        The real parent may never have been published. Every fix we have rests on
        exactly that reasoning, so every one has to admit it -- and if one ever stops
        being an assumption, this test is where that gets noticed.
        """
        for simulation, fix in PARENT_FIXES.items():
            assert "ASSUMPTION" in fix.evidence, simulation

    def test_each_fix_corrects_the_thing_its_reason_names(self):
        """
        All three are the same shape: a parent variant the header does not give
        """
        for fix in PARENT_FIXES.values():
            assert fix.info.parent_variant_label is not None
            assert fix.info.parent_experiment is not None

    def test_a_fix_keys_on_the_child_not_the_parent(self):
        """
        A correction is aimed at the dataset that got it wrong
        """
        assert INMCM4 in PARENT_FIXES
        assert PARENT_FIXES[INMCM4].info.parent_experiment == "piControl"


class TestLookingOneUp:
    def test_a_simulation_with_a_fix_gets_it(self):
        assert fixes_from_mapping(PARENT_FIXES)(INMCM4) is PARENT_FIXES[INMCM4]

    def test_a_simulation_without_one_gets_nothing(self):
        """
        Which is the overwhelmingly common answer, and not a failure
        """
        healthy = ParentSimulation(
            project="CMIP6",
            model="UKESM1-0-LL",
            experiment="historical",
            variant_label="r1i1p1f2",
        )

        assert fixes_from_mapping(PARENT_FIXES)(healthy) is None

    def test_correcting_nothing_is_a_valid_choice(self):
        assert no_parent_fixes(INMCM4) is None
