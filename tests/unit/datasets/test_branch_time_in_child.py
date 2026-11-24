"""
Tests of where each project's `branch_time_in_child` comes from
"""

from __future__ import annotations

import pytest

from esmporium.datasets.branch_time_in_child import (
    BRANCH_TIME_IN_CHILD_BY_PROJECT,
    BranchTimeInChild,
    UnknownProjectBranchTimeInChildError,
    branch_time_in_child_convention,
)


@pytest.mark.parametrize("project", ["CMIP6", "CMIP6Plus", "CMIP7"])
def test_the_projects_which_publish_it(project):
    """
    Their headers carry `branch_time_in_child`, so a branch point without one is
    unrecorded rather than something to stand in for
    """
    assert branch_time_in_child_convention(project) is BranchTimeInChild.PUBLISHED


def test_cmip5_is_assumed_to_start_at_its_branch():
    """
    The decision the module records: CMIP5 publishes nothing on the child's clock
    """
    assert (
        branch_time_in_child_convention("CMIP5")
        is BranchTimeInChild.ASSUMED_FIRST_TIME_BOUND
    )


def test_an_unknown_project_cannot_be_asked():
    """
    Either answer would be a claim we have no grounds for
    """
    with pytest.raises(UnknownProjectBranchTimeInChildError, match="MIP1"):
        branch_time_in_child_convention("MIP1")


def test_a_project_can_be_added_without_forking_anything():
    """
    The registry idiom, and the way through for a project we do not ship

    The error above names this in its own message, so it has to work.
    """
    conventions = {
        **BRANCH_TIME_IN_CHILD_BY_PROJECT,
        "MIP1": BranchTimeInChild.PUBLISHED,
    }

    assert (
        branch_time_in_child_convention("MIP1", conventions)
        is BranchTimeInChild.PUBLISHED
    )


def test_one_of_ours_can_be_overridden():
    """
    For a caller who knows their CMIP5 runs do not start at their branch, and would
    rather be told to record one than have it assumed
    """
    conventions = {
        **BRANCH_TIME_IN_CHILD_BY_PROJECT,
        "CMIP5": BranchTimeInChild.PUBLISHED,
    }

    assert (
        branch_time_in_child_convention("CMIP5", conventions)
        is BranchTimeInChild.PUBLISHED
    )
