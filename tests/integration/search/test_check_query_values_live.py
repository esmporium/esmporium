"""
Test the facet value/typo checker end-to-end against the live vocabulary sources

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
    """`abrupt-4xco2` (hyphen + lower case) points back to CMIP5's `abrupt4xCO2`."""
    report = check_query_values(QueryCMIP5(experiment="abrupt-4xco2", variable="tas"))

    finding = finding_for(report, "experiment")

    assert finding.kind == "typo"
    assert "abrupt4xCO2" in finding.suggestions


def test_cmip6_experiment_case_slip_is_matched_to_the_real_spelling():
    """`Historical` differs from CMIP6's `historical` only in case."""
    report = check_query_values(
        QueryCMIP6(experiment_id="Historical", variable_id="tas")
    )

    finding = finding_for(report, "experiment")

    assert finding.kind == "case"
    assert finding.suggestions == ("historical",)


def test_cmip7_experiment_typo_is_matched_via_the_controlled_vocabulary():
    """`abrupt-4x` (truncated) points back to the CV's `abrupt-4xCO2`.

    CMIP7 is checked against the controlled vocabulary rather than a live index,
    because ESGF-NG is near-empty and cannot list facet values.
    """
    report = check_query_values(QueryCMIP7(experiment_id="abrupt-4x"))

    finding = finding_for(report, "experiment")

    assert finding.kind == "typo"
    assert "abrupt-4xCO2" in finding.suggestions


def test_cmip6_variant_label_typo_is_reported_as_absent_with_examples():
    """A malformed variant label isn't published, so Solr reports it absent + examples.

    Solr enumerates variant labels, so it can only speak to presence, not form:
    `r1i1pf1` is simply not in the list, and the honest help is a sample of real
    ones (all of the `r...` shape) to eyeball against.
    """
    report = check_query_values(
        QueryCMIP6(experiment_id="historical", variant_label="r1i1pf1")
    )

    finding = finding_for(report, "variant_label")

    assert finding.kind == "absent"
    assert finding.suggestions
    assert all(suggestion.startswith("r") for suggestion in finding.suggestions)


def test_cmip7_variant_label_bad_form_is_matched_against_the_cv_grammar():
    """The CV describes `variant_label` with a grammar, so a bad shape is caught.

    `r1i1pf1` (no number after `p`) does not match, and the suggestion is the
    expected *form* rendered from the CV pattern -- not a did-you-mean against
    real values, which for a generated identifier would just be noise.
    """
    report = check_query_values(
        QueryCMIP7(experiment_id="historical", variant_label="r1i1pf1")
    )

    finding = finding_for(report, "variant_label")

    assert finding.kind == "malformed"
    # The rendered form names the parts, so it reads like r{realization}i{...}...
    (form,) = finding.suggestions
    assert "{realization}" in form
    assert "{physics}" in form


def test_cmip7_well_formed_variant_label_passes_silently():
    """A well-formed variant label is neither flagged nor left `unchecked`.

    `r5i1p1f1` matches the CV grammar even if that run was never produced. The
    CV can only judge form, so the honest outcome is no finding -- and it counts
    as checked, because we checked the one thing we can.
    """
    report = check_query_values(
        QueryCMIP7(experiment_id="historical", variant_label="r5i1p1f1")
    )

    if "variant_label" in report.unchecked:
        pytest.skip("the CV could not be fetched today, so form was not checked")

    assert "variant_label" not in {finding.facet for finding in report.findings}
