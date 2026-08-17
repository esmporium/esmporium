"""
Test the query value checker end-to-end against the live APIs
"""

from __future__ import annotations

import re

import pytest

from esmporium.query import QueryCMIP5, QueryCMIP6, QueryCMIP7, QueryProtocol
from esmporium.search import (
    FacetFinding,
    FindingKind,
    NoSourceWouldAnswerError,
    ValueReport,
    check_query_values,
)

pytestmark = pytest.mark.hits_esgf_search_api


def only_report(query: QueryProtocol) -> ValueReport:
    """
    Check a query and pull out the one report, or skip if no endpoint answered

    These tests ask about the values one API holds, so they take the default
    `stop_at_first_result=True` and there is exactly one report to read.

    A node being down says nothing about the checker,
    so it is a skip rather than a failure.
    """
    try:
        outcome = check_query_values(query)
    except NoSourceWouldAnswerError:
        pytest.skip("no endpoint answered today, so there is nothing to assert")

    reports = outcome.reports
    assert len(reports) == 1, f"expected one report, got {sorted(reports)}"

    return next(iter(reports.values()))


def finding_for(report: ValueReport, facet: str) -> FacetFinding:
    """
    Pull out the finding for one facet, or skip if the source could not answer

    A facet in `failed_to_check` means the source does not list that facet's
    values or describe their form, which is not something these tests can
    control, so we skip. A facet that is neither flagged nor in
    `failed_to_check` means the source served our "wrong" value as a real one --
    that is a genuine failure, so we let the missing-finding assertion fire.
    """
    if facet in report.failed_to_check:
        pytest.skip(
            f"{facet!r} could not be checked against {report.source or 'any source'} "
            "today, so there is nothing to assert"
        )

    matches = [finding for finding in report.findings if finding.facet == facet]
    assert matches, (
        f"expected {facet!r} to be flagged, but the report did not mention it: {report}"
    )
    # One finding per value, and every query here sets one value per facet, so
    # more than one match means we are about to assert against an arbitrary pick
    # of several -- worth failing on rather than silently taking the first.
    assert len(matches) == 1, (
        f"expected exactly one finding for {facet!r}, got {len(matches)}: {matches}"
    )

    return matches[0]


def test_cmip5_experiment_typo_is_matched_to_the_real_spelling():
    report = only_report(QueryCMIP5(experiment="abrupt-4xco2", variable="tas"))

    finding = finding_for(report, "experiment")

    assert finding.kind is FindingKind.TYPO
    assert "abrupt4xCO2" in finding.suggestions


def test_cmip6_experiment_case_slip_is_matched_to_the_real_spelling():
    report = only_report(
        QueryCMIP6(
            # uppercase, which is the typo
            experiment_id="Historical",
            variable_id="tas",
        )
    )

    finding = finding_for(report, "experiment")

    assert finding.kind is FindingKind.CASE
    assert finding.suggestions == ("historical",)


def test_cmip7_experiment_typo_is_matched_against_the_apis_own_values():
    report = only_report(
        QueryCMIP7(
            # Missing the CO2 suffix
            experiment_id="abrupt-4x"
        )
    )

    finding = finding_for(report, "experiment")

    assert finding.kind is FindingKind.TYPO
    assert "abrupt-4xCO2" in finding.suggestions


def test_cmip6_variant_label_typo_is_flagged_against_the_published_values():
    report = only_report(
        QueryCMIP6(experiment_id="historical", variant_label="r1i1pf1")
    )

    finding = finding_for(report, "variant_label")

    assert finding.kind in (FindingKind.TYPO, FindingKind.UNKNOWN)


def test_cmip7_variant_label_bad_form_is_matched_against_the_apis_pattern():
    report = only_report(
        QueryCMIP7(experiment_id="historical", variant_label="r1i1pf1")
    )

    finding = finding_for(report, "variant_label")

    assert finding.kind is FindingKind.MALFORMED
    (pattern,) = finding.suggestions
    assert re.compile(pattern).fullmatch("r1i1p1f1")
    assert not re.compile(pattern).fullmatch("r1i1pf1")


def test_cmip7_well_formed_variant_label_passes_silently():
    report = only_report(
        QueryCMIP7(
            experiment_id="historical",
            # A value that may never be produced, but is well-formed
            variant_label="r5i10p12f25",
        )
    )

    assert "variant_label" not in {finding.facet for finding in report.findings}
