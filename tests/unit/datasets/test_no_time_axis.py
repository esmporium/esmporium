"""
Tests of which of a project's datasets have no time axis at all
"""

from __future__ import annotations

import pytest

from esmporium.datasets.no_time_axis import (
    NO_TIME_AXIS_INTERVALS_BY_PROJECT,
    UnknownProjectTimeAxisError,
    has_no_time_axis,
    projects_with_a_known_time_axis_vocabulary,
)


@pytest.mark.parametrize("project", sorted(NO_TIME_AXIS_INTERVALS_BY_PROJECT))
def test_fx_means_no_time_axis_in_every_project_we_know(project):
    """
    Which is what `Covers` refuses against
    """
    assert has_no_time_axis(project, "fx")


@pytest.mark.parametrize("reporting_interval", ["mon", "yr", "day", "3hr", "monC"])
def test_everything_else_has_a_time_axis(reporting_interval):
    """
    Including a climatology, whose twelve values are still on a time axis
    """
    assert not has_no_time_axis("CMIP6", reporting_interval)


def test_an_unknown_project_cannot_be_asked_whether_it_has_a_time_axis():
    """
    `False` would be a claim we have no grounds for
    """
    with pytest.raises(UnknownProjectTimeAxisError, match="MIP1"):
        has_no_time_axis("MIP1", "mon")


def test_a_project_can_be_added_without_forking_anything():
    """
    The registry idiom, and the way through for a project we do not ship

    The error above names this in its own message, so it has to work.
    """
    intervals = {**NO_TIME_AXIS_INTERVALS_BY_PROJECT, "MIP1": frozenset({"const"})}

    assert has_no_time_axis("MIP1", "const", intervals=intervals)
    assert not has_no_time_axis("MIP1", "mon", intervals=intervals)


def test_the_projects_we_can_answer_for_are_the_ones_in_the_registry():
    """
    What a caller with no project in hand falls back on

    [Covers][esmporium.requirements.Covers] uses this when a leaf names a frequency
    and no project: refusing then means the frequency has no time axis in *every*
    project we know, so the list has to be exactly the registry's keys.
    """
    assert projects_with_a_known_time_axis_vocabulary() == frozenset(
        NO_TIME_AXIS_INTERVALS_BY_PROJECT
    )
    assert projects_with_a_known_time_axis_vocabulary(
        {"MIP1": frozenset({"fx"})}
    ) == frozenset({"MIP1"})
