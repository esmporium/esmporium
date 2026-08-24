"""
Test the opt-in facet value/typo checker without touching the network

"""

from __future__ import annotations

import importlib
import re

import httpx
from tenacity import Retrying, stop_after_attempt

from esmporium.query import QueryCMIP5, QueryCMIP6, to_canonical
from esmporium.search import (
    Cmip7CvVocabularySource,
    FacetFinding,
    SolrVocabularySource,
    ValueReport,
    check_query_values,
    check_query_values_low,
    compare_values,
    facets_the_user_set,
    vocabulary_source_for,
)
from esmporium.search.search_api import SOLR_CMIP6, SearchAPI

# The module object itself, for monkeypatching its globals (`httpx`,
# `vocabulary_source_for`). We resolve it through importlib rather than
# `import ... as` because the `esmporium.search` package re-exports a *function*
# of the same name, which shadows the submodule for a plain import.
checker = importlib.import_module("esmporium.search.check_query_values")


def canonical_cmip6(**facets):
    """A canonical CMIP6 query, built from keyword facet values."""
    return to_canonical(QueryCMIP6(**facets))


def test_exact_match_is_not_a_finding():
    """A value that is spelt exactly right raises nothing."""
    canonical = canonical_cmip6(experiment_id="historical")
    assert compare_values(canonical, {"experiment": {"historical", "piControl"}}) == ()


def test_wrong_case_only_is_a_case_finding():
    """A value that differs only in case is a 'case' finding, suggesting the real casing."""  # noqa: E501
    canonical = canonical_cmip6(experiment_id="Historical")

    (finding,) = compare_values(canonical, {"experiment": {"historical"}})

    assert finding == FacetFinding("experiment", "Historical", "case", ("historical",))


def test_near_miss_is_a_typo_finding_with_ranked_suggestions():
    """A close-but-wrong spelling is a 'typo' finding carrying the closest real values."""  # noqa: E501
    canonical = canonical_cmip6(experiment_id="abrupt4xco2")

    (finding,) = compare_values(
        canonical,
        {"experiment": {"abrupt-4xCO2", "abrupt-2xCO2", "historical"}},
    )

    assert finding.facet == "experiment"
    assert finding.value == "abrupt4xco2"
    assert finding.kind == "typo"
    # The closest real spelling is offered, and it is offered first.
    assert finding.suggestions
    assert finding.suggestions[0] == "abrupt-4xCO2"


def test_nonsense_is_an_unknown_finding_with_no_suggestion():
    """A value nothing is close to is 'unknown', with no did-you-mean."""
    canonical = canonical_cmip6(experiment_id="zzzzzzzz")

    (finding,) = compare_values(canonical, {"experiment": {"historical", "piControl"}})

    assert finding == FacetFinding("experiment", "zzzzzzzz", "unknown", ())


def test_suggestions_are_capped():
    """No more than MAX_SUGGESTIONS did-you-means are offered, however many match."""
    canonical = canonical_cmip6(experiment_id="ssp")
    allowed = {"experiment": {f"ssp{n}" for n in range(10)}}

    (finding,) = compare_values(canonical, allowed)

    assert len(finding.suggestions) <= checker.MAX_SUGGESTIONS


def test_a_facet_not_in_available_is_left_untouched():
    """compare_values only judges facets it was given values for; the
    rest are silent.
    """
    canonical = canonical_cmip6(experiment_id="Historical", variable_id="tas")

    # We give values for `variable` only; the wrong-cased `experiment` is not judged.
    findings = compare_values(canonical, {"variable": {"tas"}})

    assert findings == ()


def test_a_facet_can_carry_several_bad_values():
    """Every value a multi-value facet holds is checked, in order."""
    canonical = canonical_cmip6(experiment_id=("Historical", "abrupt4xco2", "tas"))

    findings = compare_values(canonical, {"experiment": {"historical", "abrupt-4xCO2"}})

    kinds = {(f.value, f.kind) for f in findings}
    assert kinds == {
        ("Historical", "case"),
        ("abrupt4xco2", "typo"),
        ("tas", "unknown"),
    }


def test_findings_are_ordered_by_facet():
    """Output is deterministic: facets are visited in sorted order."""
    canonical = canonical_cmip6(experiment_id="Xhistorical", variable_id="Ytas")

    findings = compare_values(
        canonical, {"variable": {"tas"}, "experiment": {"historical"}}
    )

    assert [f.facet for f in findings] == ["experiment", "variable"]


def test_facets_the_user_set_is_the_populated_canonical_facets_minus_project():
    """Only facets the user actually filled in count, and `project` never does."""
    canonical = canonical_cmip6(experiment_id="historical", variable_id="tas")

    assert facets_the_user_set(canonical) == {"experiment", "variable"}


def test_facets_the_user_set_includes_query_specific_facets():
    """A facet with no canonical home (CMIP5 `product`) is still reported, as unchecked."""  # noqa: E501
    canonical = to_canonical(QueryCMIP5(experiment="historical", product="output1"))

    assert facets_the_user_set(canonical) == {"experiment", "product"}


def solr_client_for(handler):
    """A one-shot httpx.Client whose requests are answered by `handler`."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def solr_api(host="node.example"):
    """A CMIP6/Solr SearchAPI that retries once and never sleeps."""
    once = Retrying(stop=stop_after_attempt(1), reraise=True)
    return SearchAPI(host, SOLR_CMIP6, once)


def test_solr_source_lists_values_keyed_by_canonical_facet(monkeypatch):
    """The source asks the node for its facets and hands values back canonically keyed."""  # noqa: E501
    seen = {}

    def handler(request):
        seen["facets"] = request.url.params.get("facets")
        # A Solr facet_fields blob: values interleaved with their counts, under
        # the API's OWN names (experiment_id, variable_id for CMIP6).
        return httpx.Response(
            200,
            json={
                "facet_counts": {
                    "facet_fields": {
                        "experiment_id": ["historical", 5, "abrupt-4xCO2", 2],
                        "variable_id": ["tas", 9],
                    }
                }
            },
        )

    # Build the mock client BEFORE patching, so building it does not itself hit
    # the patched httpx.Client (which would recurse).
    client = solr_client_for(handler)
    monkeypatch.setattr(checker.httpx, "Client", lambda **kw: client)

    source = SolrVocabularySource(solr_api())
    canonical = canonical_cmip6(experiment_id="historical", variable_id="tas")
    available = source.allowed_values(canonical, {"experiment", "variable"})

    assert available == {
        "experiment": {"historical", "abrupt-4xCO2"},
        "variable": {"tas"},
    }
    # The request named the facets under the API's spelling, sorted for determinism.
    assert seen["facets"] == "experiment_id,variable_id"


def test_solr_source_returns_empty_when_the_node_never_answers(monkeypatch):
    """A node that only errors leaves us with nothing to check against, not a crash."""
    client = solr_client_for(lambda request: httpx.Response(503))
    monkeypatch.setattr(checker.httpx, "Client", lambda **kw: client)

    source = SolrVocabularySource(solr_api())
    canonical = canonical_cmip6(experiment_id="historical")

    assert source.allowed_values(canonical, {"experiment"}) == {}


def test_solr_source_describes_itself_by_host():
    """The report names where values came from; for Solr that is the host."""
    source = SolrVocabularySource(solr_api("esgf.example.org"))
    assert source.description == "esgf.example.org"


# The real controlled-vocabulary (CV) describes variant_label with a named-group regex;
# a trimmed copy of it, used both here and in the render_form tests below.
CV_VARIANT_PATTERN = (
    r"^r(?P<realization_index>\d+)i(?P<initialization_index>(\d{4}\d{2}[abcde]?|\d+))"
    r"p(?P<physics_index>\d+)f(?P<forcing_index>\d+)$"
)

CV_SCHEMA = {
    "definitions": {
        "item_fields": {
            "properties": {
                "cmip7:experiment_id": {
                    "enum": ["abrupt-4xCO2", "historical", "piControl"]
                },
                "cmip7:variable_id": {"enum": ["tas", "pr"]},
                # variant_label is generated, so the CV describes it with a
                # pattern rather than an enum -- it must not be reported as a value.
                "cmip7:variant_label": {
                    "type": "string",
                    "pattern": CV_VARIANT_PATTERN,
                },
            }
        }
    }
}


def test_cv_source_pulls_enums_keyed_by_canonical_facet():
    """Each canonical facet maps to its `cmip7:` enum, keyed back canonically."""
    values = Cmip7CvVocabularySource._values_from_schema(
        CV_SCHEMA, {"experiment", "variable"}
    )

    assert values == {
        "experiment": {"abrupt-4xCO2", "historical", "piControl"},
        "variable": {"tas", "pr"},
    }


def test_cv_source_omits_a_facet_the_cv_does_not_enumerate():
    """A facet the CV describes without an enum (a pattern) is left out, not empty."""
    values = Cmip7CvVocabularySource._values_from_schema(
        CV_SCHEMA, {"experiment", "variant_label"}
    )

    assert set(values) == {"experiment"}


def test_cv_source_omits_a_facet_absent_from_the_cv():
    """A facet the CV says nothing about is simply left out."""
    values = Cmip7CvVocabularySource._values_from_schema(CV_SCHEMA, {"activity"})

    assert values == {}


def test_cv_source_fails_soft_when_the_cv_cannot_be_fetched(monkeypatch):
    """If the CV cannot be fetched, we get {} rather than an exception."""

    unreachable = httpx.ConnectError("no network")

    def boom(*args, **kwargs):
        raise unreachable

    monkeypatch.setattr(checker.httpx, "get", boom)

    source = Cmip7CvVocabularySource()
    canonical = to_canonical(QueryCMIP6(experiment_id="historical"))

    assert source.allowed_values(canonical, {"experiment"}) == {}


def test_cv_source_reads_the_variant_label_pattern():
    """A facet the CV describes with a regex comes back as a compiled pattern."""
    source = Cmip7CvVocabularySource()
    source._cache["schema"] = CV_SCHEMA  # avoid the network fetch

    pattern = source.facet_pattern("variant_label")

    assert pattern is not None
    assert pattern.fullmatch("r1i1p1f1")
    assert not pattern.fullmatch("r1i1pf1")  # the missing-digit case


def test_cv_source_has_no_pattern_for_an_enumerated_facet():
    """A facet described by an enum (not a regex) has no grammar to hand back."""
    source = Cmip7CvVocabularySource()
    source._cache["schema"] = CV_SCHEMA

    assert source.facet_pattern("experiment") is None


def test_cmip5_and_cmip6_route_to_the_selectors_first_node():
    """CMIP5/6 check against the very node the search would have hit first."""
    picked = solr_api("first.node.example")
    selector = lambda canonical, attempt: picked if attempt == 0 else None  # noqa: E731

    canonical = to_canonical(QueryCMIP5(experiment="historical"))
    source = vocabulary_source_for(canonical, selector)

    assert isinstance(source, SolrVocabularySource)
    assert source.api is picked


def test_cmip7_routes_to_the_controlled_vocabulary():
    """CMIP7 checks against the CV, because ESGF-NG cannot list its facet values."""
    canonical = to_canonical(QueryCMIP6(experiment_id="historical")).model_copy(
        update={"project": ("CMIP7",)}
    )

    assert isinstance(vocabulary_source_for(canonical), Cmip7CvVocabularySource)


def test_a_project_with_no_source_routes_to_none():
    """A project we have no vocabulary for gets no source (everything is unchecked)."""
    canonical = to_canonical(QueryCMIP6(experiment_id="historical")).model_copy(
        update={"project": ("CMIP99",)}
    )

    assert vocabulary_source_for(canonical) is None


def test_cmip5_routes_to_none_when_the_selector_is_exhausted():
    """If the selector has no node to offer, there is nothing to check against."""
    canonical = to_canonical(QueryCMIP5(experiment="historical"))

    assert vocabulary_source_for(canonical, lambda c, a: None) is None


class StubSource:
    """
    A vocabulary source that returns the facet values (and patterns) it
    was handed.
    """

    def __init__(self, values, description="stub-source", patterns=None):
        self._values = values
        self.description = description
        self._patterns = patterns or {}

    def allowed_values(self, canonical, facets):
        return {facet: self._values[facet] for facet in facets if facet in self._values}

    def facet_pattern(self, facet):
        return self._patterns.get(facet)


# TODO: I need to double check what the high/low tiering is
# referring to, to re-name these tests
def test_low_tiers_findings_and_reports_the_rest_as_unchecked():
    """check_query_values_low tiers what it can and lists what it could not check."""
    canonical = canonical_cmip6(experiment_id="Historical", variable_id="tas")
    source = StubSource({"experiment": {"historical"}}, description="a-node")

    report = check_query_values_low(canonical, source)

    assert report.project == "CMIP6"
    assert report.source == "a-node"
    # experiment was checked (wrong case); variable had no values to check against.
    assert report.findings == (
        FacetFinding("experiment", "Historical", "case", ("historical",)),
    )
    assert report.unchecked == ("variable",)
    assert not report.ok()


def test_low_reports_ok_when_everything_matches():
    """A clean query yields no findings and ok() is True."""
    canonical = canonical_cmip6(experiment_id="historical")
    source = StubSource({"experiment": {"historical"}})

    report = check_query_values_low(canonical, source)

    assert report.findings == ()
    assert report.ok()


def test_high_with_no_source_marks_every_set_facet_unchecked():
    """When routing finds no source, nothing is judged and every facet is unchecked."""
    report = check_query_values(
        QueryCMIP5(experiment="abrupt-4xco2", variable="tas"),
        selector=lambda canonical, attempt: None,  # exhausted -> no Solr source
    )

    assert report == ValueReport(
        project="CMIP5", source="", findings=(), unchecked=("experiment", "variable")
    )


def test_high_routes_and_checks_via_a_stubbed_source(monkeypatch):
    """check_query_values wires routing to tiering: a wrong case comes back as a finding."""  # noqa: E501
    monkeypatch.setattr(
        checker,
        "vocabulary_source_for",
        lambda canonical, selector: StubSource(
            {"experiment": {"historical"}}, description="routed"
        ),
    )

    report = check_query_values(QueryCMIP6(experiment_id="Historical"))

    assert report.source == "routed"
    assert report.findings == (
        FacetFinding("experiment", "Historical", "case", ("historical",)),
    )


# Below are testing the pattern format of variant_label
def test_render_form_turns_named_groups_into_a_template():
    """A regex with named groups renders as a readable `r{...}i{...}...` template."""
    assert checker.render_form(CV_VARIANT_PATTERN) == (
        "r{realization}i{initialization}p{physics}f{forcing}"
    )


def test_render_form_falls_back_to_the_raw_regex_without_named_groups():
    """A pattern that names nothing (a STAC summary) is handed back unchanged."""
    raw = r"^r\d+i\d+p\d+f\d+$"
    assert checker.render_form(raw) == raw


def test_sample_values_is_sorted_and_capped():
    """The sample is sorted and no bigger than MAX_SUGGESTIONS."""
    got = checker.sample_values({"r2i1p1f1", "r1i1p1f1", "r1i1p2f1", "r3i1p1f1"})

    assert got == ("r1i1p1f1", "r1i1p2f1", "r2i1p1f1")


def test_sample_values_orders_numerically_and_drops_junk():
    """Numeric order by `r`, e.g. from r1 to r2 before r10"""
    got = checker.sample_values({"1", "r100i1p1f1", "r2i1p1f1", "r1i1p1f1"})

    assert got == ("r1i1p1f1", "r2i1p1f1", "r100i1p1f1")


def test_variant_label_absent_from_a_list_source_is_reported_with_examples():
    """A list source can only speak to presence, so a missing label shows real ones."""
    canonical = canonical_cmip6(variant_label="r1i1pf1")
    source = StubSource({"variant_label": {"r1i1p1f1", "r1i1p2f1", "r2i1p1f1"}})

    report = check_query_values_low(canonical, source)

    (finding,) = report.findings
    assert finding.facet == "variant_label"
    # Not "malformed": a list source cannot tell a typo from an unproduced run.
    assert finding.kind == "absent"
    assert finding.suggestions == ("r1i1p1f1", "r1i1p2f1", "r2i1p1f1")
    assert "variant_label" not in report.unchecked


def test_variant_label_present_in_a_list_source_is_not_flagged():
    """A label the source lists is fine, and counts as checked (not unchecked)."""
    canonical = canonical_cmip6(variant_label="r1i1p1f1")
    source = StubSource({"variant_label": {"r1i1p1f1", "r2i1p1f1"}})

    report = check_query_values_low(canonical, source)

    assert report.findings == ()
    assert "variant_label" not in report.unchecked


def test_variant_label_malformed_against_a_grammar_source_shows_the_form():
    """A grammar source flags a bad shape and hands back the expected form."""
    canonical = canonical_cmip6(variant_label="r1i1pf1")
    source = StubSource({}, patterns={"variant_label": re.compile(CV_VARIANT_PATTERN)})

    report = check_query_values_low(canonical, source)

    (finding,) = report.findings
    assert finding.facet == "variant_label"
    assert finding.kind == "malformed"
    assert finding.suggestions == (
        "r{realization}i{initialization}p{physics}f{forcing}",
    )
    assert "variant_label" not in report.unchecked


def test_variant_label_well_formed_against_a_grammar_source_is_silent():
    """A well-formed label passes silently: we cannot say whether that run exists.

    `r5i1p1f1` matches the grammar even if nobody produced it. A grammar source
    can only judge form, so the honest outcome is no finding -- and crucially
    *not* `unchecked`, because we did check the one thing we can.
    """
    canonical = canonical_cmip6(variant_label="r5i1p1f1")
    source = StubSource({}, patterns={"variant_label": re.compile(CV_VARIANT_PATTERN)})

    report = check_query_values_low(canonical, source)

    assert report.findings == ()
    assert "variant_label" not in report.unchecked


def test_variant_label_is_unchecked_when_the_source_offers_neither():
    """A source with no list and no grammar leaves the label honestly unchecked."""
    canonical = canonical_cmip6(variant_label="r1i1pf1")
    source = StubSource({})  # no values, no patterns

    report = check_query_values_low(canonical, source)

    assert report.findings == ()
    assert report.unchecked == ("variant_label",)


def test_vocabulary_and_variant_label_are_checked_side_by_side():
    """A vocabulary facet and a variant label are checked by their own rules at once."""
    canonical = canonical_cmip6(experiment_id="Historical", variant_label="r1i1pf1")
    source = StubSource({"experiment": {"historical"}, "variant_label": {"r1i1p1f1"}})

    report = check_query_values_low(canonical, source)

    assert {(f.facet, f.kind) for f in report.findings} == {
        ("experiment", "case"),
        ("variant_label", "absent"),
    }
