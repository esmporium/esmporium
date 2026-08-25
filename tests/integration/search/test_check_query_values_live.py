"""
Test the query value checker end-to-end against the live APIs
"""

from __future__ import annotations

import pytest

from esmporium.query import QueryCMIP5, QueryCMIP6, QueryCMIP7
from esmporium.search import FacetFinding, ValueReport, check_query_values

pytestmark = pytest.mark.hits_esgf_search_api


def finding_for(report: ValueReport, facet: str) -> FacetFinding:
    """
    Pull out the finding for one facet, or skip if the source could not answer

    A facet in `unchecked` means the source was unreachable (or does not list
    that facet's values), which is not something these tests can control, so we
    skip. A facet that is neither flagged nor unchecked means the source served
    our "wrong" value as a real one -- that is a genuine failure, so we let the
    missing-finding assertion fire.
    """
    if facet in report.unchecked:
        pytest.skip(
            f"{facet!r} could not be checked against {report.source or 'any source'} "
            "today, so there is nothing to assert"
        )

    matches = [finding for finding in report.findings if finding.facet == facet]
    assert matches, (
        f"expected {facet!r} to be flagged, but the report did not mention it: {report}"
    )
    return matches[0]


def test_cmip5_experiment_typo_is_matched_to_the_real_spelling():
    report = check_query_values(QueryCMIP5(experiment="abrupt-4xco2", variable="tas"))

    finding = finding_for(report, "experiment")

    assert finding.kind == "typo"
    assert "abrupt4xCO2" in finding.suggestions


def test_cmip6_experiment_case_slip_is_matched_to_the_real_spelling():
    report = check_query_values(
        QueryCMIP6(
            # uppercase, which is the typo
            experiment_id="Historical",
            variable_id="tas",
        )
    )

    finding = finding_for(report, "experiment")

    assert finding.kind == "case"
    assert finding.suggestions == ("historical",)


def test_cmip7_experiment_typo_is_matched_via_the_controlled_vocabulary():
    report = check_query_values(
        QueryCMIP7(
            # Missing the CO2 suffix
            experiment_id="abrupt-4x"
        )
    )

    finding = finding_for(report, "experiment")

    assert finding.kind == "typo"
    assert "abrupt-4xCO2" in finding.suggestions


def test_cmip6_variant_label_typo_is_reported_as_absent_with_examples():
    report = check_query_values(
        QueryCMIP6(experiment_id="historical", variant_label="r1i1pf1")
    )

    finding = finding_for(report, "variant_label")

    assert finding.kind == "absent"
    assert finding.suggestions
    assert all(suggestion.startswith("r") for suggestion in finding.suggestions)


def test_cmip7_variant_label_bad_form_is_matched_against_the_cv_grammar():
    report = check_query_values(
        QueryCMIP7(experiment_id="historical", variant_label="r1i1pf1")
    )

    finding = finding_for(report, "variant_label")

    assert finding.kind == "malformed"
    # Suggestion is the regular expression, not a list of allowed values
    # because those are not given by the STAC API schema for varient label
    # TODO: why is this what we get back rather than what is in the CVs
    # i.e. https://github.com/WCRP-CMIP/WCRP-universe/blob/main/variant_label/ripf.json
    assert finding.suggestions == (
        "r{realization}i{initialization}p{physics}f{forcing}",
    )


def test_cmip7_well_formed_variant_label_passes_silently():
    report = check_query_values(
        QueryCMIP7(
            experiment_id="historical",
            # A value that may never be produced, but is well-formed
            variant_label="r5i10p12f25",
        )
    )

    assert "variant_label" not in {finding.facet for finding in report.findings}
