"""
Choosing which of a parent simulation's datasets is a given child's parent

A table of cases with no database in sight, which is the point of the matchers being
pure: what they decide is a function of the child and the candidates and nothing else.
"""

from __future__ import annotations

import pytest

from esmporium.db import (
    Dataset,
    DatasetVersion,
    MatchOutcome,
    UnknownProjectMatcherError,
    choose_parent_version,
    match_cmip5_parent,
    match_cmip6_parent,
    match_on_facets,
    matcher_for_project,
)


def dataset(  # noqa: PLR0913 - one argument per facet a case may want to vary
    experiment: str,
    variable: str = "tas",
    reporting_interval: str = "mon",
    processing_id: str = "Amon",
    grid_label: str | None = "gn",
    institution: str = "MOHC",
    variant_label: str = "r1i1p1f1",
    project: str = "CMIP6",
    dataset_id: int | None = None,
) -> Dataset:
    """Build an unsaved dataset row; the matchers never touch a session."""
    return Dataset(
        id=dataset_id,
        id_project_specific=(
            f"{project}.{experiment}.{variant_label}.{variable}"
            f".{processing_id}.{grid_label}.{institution}"
        ),
        project=project,
        model="UKESM1-0-LL",
        institution=institution,
        experiment=experiment,
        variant_label=variant_label,
        variable=variable,
        reporting_interval=reporting_interval,
        grid_label=grid_label,
        processing_id=processing_id,
    )


def test_the_parents_experiment_and_variant_are_never_compared():
    """
    A parent is in a different experiment from its child, and often a different
    variant

    So comparing those child-to-candidate would reject every real parent. Picking the
    right simulation is the query's job; this only picks within it.
    """
    child = dataset("abrupt-4xCO2", variant_label="r2i1p1f2")
    parent = dataset("piControl", variant_label="r1i1p1f1")

    assert match_cmip6_parent(child, [parent]).parent_dataset is parent


def test_the_interval_and_table_decide_between_two_of_one_variable():
    """
    One simulation publishes `tas` more than once

    `CanESM5 piControl r1i1p1f1 tas` is both `Amon`/`mon` and `day`/`day`. Matching on
    variable alone would pick between them by accident.
    """
    child = dataset("abrupt-4xCO2", reporting_interval="mon", processing_id="Amon")
    monthly = dataset("piControl", reporting_interval="mon", processing_id="Amon")
    daily = dataset("piControl", reporting_interval="day", processing_id="day")

    outcome = match_cmip6_parent(child, [daily, monthly])

    assert outcome.parent_dataset is monthly
    assert not outcome.is_ambiguous


def test_a_different_grid_is_not_the_same_dataset():
    """Two grids of one variable are two datasets, for a project which has grids."""
    child = dataset("abrupt-4xCO2", grid_label="gn")
    other_grid = dataset("piControl", grid_label="gr")

    outcome = match_cmip6_parent(child, [other_grid])

    assert outcome.parent_dataset is None
    assert not outcome.is_ambiguous
    assert "grid_label" in (outcome.reason or "")


def test_a_cmip5_match_works_with_no_grid_on_either_side():
    """
    CMIP5 has no concept of a grid, so both sides are `None`

    Two absences are an agreement. The CMIP5 matcher leaves the facet out entirely
    rather than relying on that, so the rule says what it means.
    """
    child = dataset(
        "abrupt4xCO2", project="CMIP5", grid_label=None, variant_label="r1i1p1"
    )
    parent = dataset(
        "piControl", project="CMIP5", grid_label=None, variant_label="r1i1p1"
    )

    assert match_cmip5_parent(child, [parent]).parent_dataset is parent


def test_only_the_cmip5_matcher_ignores_the_grid():
    """
    The facets are spelled out per project, so the difference is in the matchers

    Asserted through what they decide rather than against a shared constant: a
    candidate differing only in grid label is the same dataset to CMIP5, which has no
    grids, and a different one to CMIP6, which does.
    """
    child = dataset("abrupt-4xCO2", project="CMIP5", grid_label="gn")
    other_grid = dataset("piControl", project="CMIP5", grid_label="gr")

    assert match_cmip5_parent(child, [other_grid]).parent_dataset is other_grid
    assert match_cmip6_parent(child, [other_grid]).parent_dataset is None


def test_a_parent_in_another_institution_is_still_the_parent():
    """
    The case that would break if institution were required

    A `UCSB` CMIP6 `historical` branches from an `E3SM-Project` `piControl`; no source
    publishes the parent's institution, so it cannot be constrained.
    """
    child = dataset("historical", institution="UCSB")
    parent = dataset("piControl", institution="E3SM-Project")

    assert match_cmip6_parent(child, [parent]).parent_dataset is parent


def test_the_childs_institution_breaks_a_tie():
    """When two candidates are otherwise identical, prefer the child's institution."""
    child = dataset("historical", institution="E3SM-Project")
    same = dataset("piControl", institution="E3SM-Project", dataset_id=1)
    other = dataset("piControl", institution="UCSB", dataset_id=2)

    assert match_cmip6_parent(child, [other, same]).parent_dataset is same


def test_two_indistinguishable_candidates_are_reported_not_picked():
    """
    Picking the lowest id is what the first version of this did, silently

    An answer nobody could later tell from a real one is worse than a reported
    ambiguity, which is something an injected matcher can then resolve.
    """
    child = dataset("historical", institution="MOHC")
    first = dataset("piControl", institution="UCSB", dataset_id=1)
    second = dataset("piControl", institution="LLNL", dataset_id=2)

    outcome = match_cmip6_parent(child, [first, second])

    assert outcome.parent_dataset is None
    assert outcome.is_ambiguous
    assert list(outcome.candidates) == [first, second]
    assert "UCSB" in (outcome.reason or "") and "LLNL" in (outcome.reason or "")


def test_nothing_matching_is_not_the_same_as_ambiguous():
    """
    The two no-parent answers are different facts

    Nothing matched means this variable has a hole at this ancestor. Several matched
    means the rule could not choose. A caller acts differently on each.
    """
    child = dataset("abrupt-4xCO2", variable="rsdt")
    only_tas = dataset("piControl", variable="tas")

    outcome = match_cmip6_parent(child, [only_tas])

    assert outcome.parent_dataset is None
    assert not outcome.is_ambiguous
    assert outcome.candidates == ()


def test_no_candidates_at_all_says_so():
    """An empty candidate list is the commonest no-match, and must not raise."""
    outcome = match_cmip6_parent(dataset("abrupt-4xCO2"), [])

    assert outcome.parent_dataset is None
    assert not outcome.is_ambiguous
    assert "0 dataset(s)" in (outcome.reason or "")


def test_the_institution_tie_break_can_be_turned_off():
    """Injecting a stricter rule is the point of the engine being parameterised."""
    child = dataset("historical", institution="E3SM-Project")
    same = dataset("piControl", institution="E3SM-Project", dataset_id=1)
    other = dataset("piControl", institution="UCSB", dataset_id=2)

    outcome = match_on_facets(child, [same, other], prefer_same_institution=False)

    assert outcome.is_ambiguous


def test_each_project_gets_its_own_matcher():
    """
    CMIP6 and CMIP7 compare the same facets today and are still named separately

    `processing_id` is a CMOR table for CMIP6 and a branding suffix for CMIP7, so the
    two agreeing is a coincidence of CMIP7's design rather than something to rely on.
    """
    assert matcher_for_project("CMIP5") is match_cmip5_parent
    assert matcher_for_project("CMIP6") is match_cmip6_parent
    assert matcher_for_project("CMIP7") is not match_cmip6_parent


def test_an_unknown_project_falls_back_or_refuses_on_request():
    """A project we have not met is more likely CMIP6-shaped than unmatchable."""
    assert matcher_for_project("CORDEX-CMIP6") is match_on_facets

    with pytest.raises(UnknownProjectMatcherError, match="CORDEX-CMIP6"):
        matcher_for_project("CORDEX-CMIP6", use_default=False)


def test_an_injected_matcher_overrides_a_shipped_one():
    """The whole point of the mapping being a parameter."""

    def always_first(child, candidates):
        return MatchOutcome(parent_dataset=candidates[0] if candidates else None)

    found = matcher_for_project("CMIP6", {"CMIP6": always_first})

    assert found is always_first


def a_version(version: str, dataset_id: int = 1) -> DatasetVersion:
    """Build an unsaved version row; the chooser never touches a session."""
    return DatasetVersion(
        dataset_id=dataset_id, version=version, is_latest=False, retracted=False
    )


def test_the_childs_own_version_is_the_one_chosen():
    """
    The only signal in the data, so it is the one used

    A version string is a publication datestamp, so a parent published in the same
    batch as its child is the one that generation of the child was produced against.
    """
    child = a_version("20261008")
    candidates = [a_version("20200101"), a_version("20261008"), a_version("20300101")]

    outcome = choose_parent_version(child, candidates)

    assert outcome.parent_version is candidates[1]
    assert not outcome.is_ambiguous


def test_one_version_is_the_answer_whatever_it_is_called():
    """
    A parent published once is the parent, matching string or not

    The commonest case by far, and the one that would wrongly be reported as a choice
    if the version string had to agree.
    """
    child = a_version("20261008")
    only = a_version("20110615")

    outcome = choose_parent_version(child, [only])

    assert outcome.parent_version is only
    assert not outcome.is_ambiguous


def test_several_versions_and_none_the_childs_is_handed_back():
    """
    Nothing published says which version a child branched from

    So the rule stops rather than picking the newest, which would record our default
    as though it were the published answer.
    """
    child = a_version("20261008")
    candidates = [a_version("20110615"), a_version("20300101")]

    outcome = choose_parent_version(child, candidates)

    assert outcome.parent_version is None
    assert outcome.is_ambiguous
    assert list(outcome.candidates) == candidates
    reason = outcome.reason or ""
    assert "20110615" in reason and "20300101" in reason and "20261008" in reason


def test_no_versions_at_all_is_not_an_ambiguity():
    """
    Holding none of a dataset's versions is a different answer from holding several

    One is "nothing to choose from" and the other is "a choice nobody can make", and
    only the second is worth putting in front of a person.
    """
    outcome = choose_parent_version(a_version("20261008"), [])

    assert outcome.parent_version is None
    assert not outcome.is_ambiguous
    assert outcome.candidates == ()
