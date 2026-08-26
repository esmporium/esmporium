"""
Test the opt-in facet value/typo checker without touching the network
"""

from __future__ import annotations

import re

import httpx
import pytest
from tenacity import Retrying, stop_after_attempt

from esmporium.query import QueryCMIP5, QueryCMIP6, to_canonical
from esmporium.search import (
    AllowedValues,
    CouldNotGetAllowedValuesError,
    FacetFinding,
    FindingKind,
    NoSourceWouldAnswerError,
    NotAFacetOfTheQueryError,
    SearchAPIValuesSource,
    check_query_values,
    check_query_values_low,
    compare_values,
    facets_the_user_set,
    values_set_for,
)
from esmporium.search.search_api import SOLR_CMIP6, STAC_CMIP6, SearchAPI


def canonical_cmip6(**facets):
    """A canonical CMIP6 query, built from keyword facet values."""
    return to_canonical(QueryCMIP6(**facets))


def test_exact_match_is_not_a_finding():
    """A value that is spelt exactly right raises nothing."""
    canonical = canonical_cmip6(experiment_id="historical")
    assert compare_values(canonical, {"experiment": {"historical", "piControl"}}) == ()


def test_wrong_case_only_is_a_case_finding():
    canonical = canonical_cmip6(experiment_id="Historical")

    (finding,) = compare_values(canonical, {"experiment": {"historical"}})

    assert finding == FacetFinding("experiment", "Historical", "case", ("historical",))


def test_near_miss_is_a_typo_finding_with_ranked_suggestions():
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
    canonical = canonical_cmip6(experiment_id="zzzzzzzz")

    (finding,) = compare_values(canonical, {"experiment": {"historical", "piControl"}})

    assert finding == FacetFinding("experiment", "zzzzzzzz", "unknown", ())


def test_suggestions_are_capped():
    """
    Test that a value close to many allowed ones is not answered with all of them

    Ten spellings are all equally close here, and three come back:
    the `n` the default matcher ships with. That number is worth pinning
    rather than bounding, because a change to it is a change to what users see.
    """
    canonical = canonical_cmip6(experiment_id="ssp")
    allowed = {"experiment": {f"ssp{n}" for n in range(10)}}

    (finding,) = compare_values(canonical, allowed)

    assert len(finding.suggestions) == 3


def test_a_facet_not_in_available_is_left_untouched():
    """
    compare_values only judges facets it was given values for; the rest are silent.
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


def test_facets_the_user_set_is_the_populated_canonical_facets():
    """
    Test that every filled-in facet counts
    """
    canonical = canonical_cmip6(experiment_id="historical", variable_id="tas")

    assert facets_the_user_set(canonical) == {"experiment", "project", "variable"}


def test_facets_the_user_set_leaves_out_what_was_never_filled_in():
    """A facet nobody set has no value to check, so it is not asked about."""
    canonical = canonical_cmip6(experiment_id="historical")

    assert "variable" not in facets_the_user_set(canonical)


def test_facets_the_user_set_includes_query_specific_facets():
    canonical = to_canonical(QueryCMIP5(experiment="historical", product="output1"))

    assert facets_the_user_set(canonical) == {"experiment", "product", "project"}


def client_for(handler):
    """Build an httpx client whose requests are answered by `handler`"""
    return httpx.Client(transport=httpx.MockTransport(handler))


def never_asked(request):
    """A handler for the tests in which nothing should be sent anywhere."""
    pytest.fail(f"unexpected request to {request.url}")


def once():
    """A retry policy which tries a single time and never sleeps."""
    return Retrying(stop=stop_after_attempt(1), reraise=True)


def solr_api(host="node.example"):
    """A CMIP6/Solr SearchAPI that retries once and never sleeps."""
    return SearchAPI(host, SOLR_CMIP6, once())


def test_solr_source_lists_values_keyed_by_canonical_facet():
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

    source = SearchAPIValuesSource(solr_api(), client_for(handler))
    canonical = canonical_cmip6(experiment_id="historical", variable_id="tas")
    allowed = source.allowed_values(canonical, {"experiment", "variable"})

    assert allowed.values == {
        "experiment": {"historical", "abrupt-4xCO2"},
        "variable": {"tas"},
    }
    # Solr enumerates its values. It never provides patterns.
    assert allowed.patterns == {}
    # The request named the facets under the API's spelling, sorted for determinism.
    assert seen["facets"] == "experiment_id,variable_id"


def test_a_node_which_never_answers_raises():
    """
    Test that a node we could not get values from is an error, not an empty answer

    Handing back "nothing to check against" would be indistinguishable from
    "we checked, and it was fine", which is the one thing this module
    must never say by accident.
    """
    source = SearchAPIValuesSource(
        solr_api("down.example"), client_for(lambda request: httpx.Response(503))
    )
    canonical = canonical_cmip6(experiment_id="historical")

    with pytest.raises(CouldNotGetAllowedValuesError, match=re.escape("down.example")):
        source.allowed_values(canonical, {"experiment"})


def test_solr_source_describes_itself_by_host():
    """The report names where values came from; for Solr that is the host."""
    source = SearchAPIValuesSource(
        solr_api("esgf.example.org"), client_for(never_asked)
    )
    assert source.description == "esgf.example.org"


# A variant_label pattern of the shape the STAC collection summaries really carry:
# no named groups, because that is what the APIs actually serve.
VARIANT_PATTERN = r"^r\d+i(\d{4}\d{2}[abcde]?|\d+)p\d+f\d+$"


class StubSource:
    """A source of allowed values which returns the values (and patterns) it was handed"""  # noqa: E501

    def __init__(self, values, description="stub-source", patterns=None):
        # `project` is a facet like any other, so every query carries it and
        # every source is asked about it. A stub which could not answer would
        # put `project` in `failed_to_check` for every test here, which would
        # say nothing about the test.
        self._values = {"project": {"CMIP5", "CMIP6", "CMIP7"}, **values}
        self.description = description
        self._patterns = patterns or {}

    def allowed_values(self, canonical, facets):
        return AllowedValues(
            values={
                facet: self._values[facet] for facet in facets if facet in self._values
            },
            patterns={
                facet: self._patterns[facet]
                for facet in facets
                if facet in self._patterns
            },
        )


def selector_yielding(*apis):
    """
    A selector which offers the given APIs in order, then runs out
    """
    return lambda canonical, attempt: apis[attempt] if attempt < len(apis) else None


def facet_values_from(experiments):
    """A handler answering any Solr facet request with the given experiments."""
    interleaved = [item for value in experiments for item in (value, 1)]

    return lambda request: httpx.Response(
        200,
        json={"facet_counts": {"facet_fields": {"experiment_id": interleaved}}},
    )


def by_host(handlers, otherwise=lambda request: httpx.Response(503)):
    """A handler which dispatches on the host the request went to."""
    return lambda request: handlers.get(request.url.host, otherwise)(request)


def test_low_tiers_findings_and_reports_the_rest_as_unchecked():
    """check_query_values_low tiers what it can and lists what it could not check."""
    canonical = canonical_cmip6(experiment_id="Historical", variable_id="tas")
    source = StubSource({"experiment": {"historical"}}, description="a-node")

    report = check_query_values_low(canonical, source)

    assert report.query.project == ("CMIP6",)
    assert report.source == "a-node"
    # experiment was checked (wrong case); variable had no values to check against.
    assert report.findings == (
        FacetFinding("experiment", "Historical", "case", ("historical",)),
    )
    assert report.failed_to_check == ("variable",)
    assert not report.ok()


def test_low_reports_ok_when_everything_matches():
    """A clean query yields no findings, nothing unchecked, and ok() is True."""
    canonical = canonical_cmip6(experiment_id="historical")
    source = StubSource({"experiment": {"historical"}})

    report = check_query_values_low(canonical, source)

    assert report.findings == ()
    assert report.failed_to_check == ()
    assert report.ok()


def test_a_report_which_could_not_check_something_is_not_ok():
    """
    Test that a facet we could not check prevents a report from being `ok`
    """
    canonical = canonical_cmip6(variable_id="tas")
    source = StubSource({})

    report = check_query_values_low(canonical, source)

    assert report.findings == ()
    assert report.failed_to_check == ("variable",)
    assert not report.ok()


def test_high_with_no_api_to_ask_reports_nothing():
    """
    Test that a selector with nothing to offer gives back no reports

    Not an error: there was nobody to have refused.
    An empty answer cannot be misread as a clean bill of health,
    because there is no report in it to call `ok()` on.
    """
    reports = check_query_values(
        QueryCMIP5(experiment="abrupt-4xco2", variable="tas"),
        selector=lambda canonical, attempt: None,
    )

    assert reports == {}


def test_high_routes_through_the_selector_to_the_api():
    """Test that the API the selector offers is the one asked, and reported under."""
    reports = check_query_values(
        QueryCMIP6(experiment_id="Historical"),
        selector=selector_yielding(solr_api("routed.example")),
        client=client_for(facet_values_from(["historical"])),
    )

    assert set(reports) == {"routed.example"}
    assert reports["routed.example"].findings == (
        FacetFinding("experiment", "Historical", "case", ("historical",)),
    )


def test_high_moves_on_to_the_next_api_when_one_will_not_answer():
    """
    Test that one endpoint refusing is not the end of the check

    This is what `search` does, and for the same reason:
    the nodes mirror one another closely enough that the next one along
    can usually answer the question the last one would not.
    """
    handler = by_host({"second.example": facet_values_from(["historical"])})

    reports = check_query_values(
        QueryCMIP6(experiment_id="Historical"),
        selector=selector_yielding(
            solr_api("down.example"), solr_api("second.example")
        ),
        client=client_for(handler),
    )

    assert set(reports) == {"second.example"}
    assert reports["second.example"].findings == (
        FacetFinding("experiment", "Historical", "case", ("historical",)),
    )


def test_high_asks_every_api_when_told_not_to_stop_at_the_first():
    """
    Test that every endpoint is asked, and kept, when the caller asks for that

    The nodes do not hold the same data. `abrupt-4xCO2` is published on one of
    these and unheard of on the other, so the same query is a typo on one and
    fine on the other -- which only comparing the reports can show.
    """
    handler = by_host(
        {
            "knows.example": facet_values_from(["historical", "abrupt-4xCO2"]),
            "does-not.example": facet_values_from(["historical"]),
        }
    )
    reports = check_query_values(
        QueryCMIP6(experiment_id="abrupt-4xCO2"),
        selector=selector_yielding(
            solr_api("knows.example"), solr_api("does-not.example")
        ),
        stop_at_first_result=False,
        client=client_for(handler),
    )

    assert set(reports) == {"knows.example", "does-not.example"}
    assert reports["knows.example"].findings == ()
    (finding,) = reports["does-not.example"].findings
    assert finding.value == "abrupt-4xCO2"
    assert finding.kind is FindingKind.UNKNOWN


def test_high_keeps_the_answers_it_got_when_only_some_apis_refuse():
    """A refusal alongside an answer is left out, not raised: we did check."""
    handler = by_host({"up.example": facet_values_from(["historical"])})

    reports = check_query_values(
        QueryCMIP6(experiment_id="historical"),
        selector=selector_yielding(solr_api("up.example"), solr_api("down.example")),
        stop_at_first_result=False,
        client=client_for(handler),
    )

    assert set(reports) == {"up.example"}


def test_high_raises_with_every_refusal_when_no_api_answers():
    """
    Test that a refusal from all of them is an error naming all of them

    Working through the endpoints must not turn "nobody would tell us" into
    "we checked and it was fine". Which endpoints refused is the interesting
    part, so every refusal is carried, not just the last.
    """
    with pytest.raises(NoSourceWouldAnswerError) as excinfo:
        check_query_values(
            QueryCMIP6(experiment_id="Historical"),
            selector=selector_yielding(
                solr_api("first.example"), solr_api("last.example")
            ),
            client=client_for(lambda request: httpx.Response(503)),
        )

    assert excinfo.value.described == ("first.example", "last.example")
    assert "first.example" in str(excinfo.value)
    assert "last.example" in str(excinfo.value)
    assert all(
        isinstance(refusal, CouldNotGetAllowedValuesError)
        for refusal in excinfo.value.refusals
    )


def test_a_value_matching_the_pattern_passes_silently():
    """
    Test that a value of the right form is not flagged

    A pattern says what a value may look like, never whether it exists.
    """
    canonical = canonical_cmip6(variant_label="r5i31p250f19")
    source = StubSource({}, patterns={"variant_label": re.compile(VARIANT_PATTERN)})

    report = check_query_values_low(canonical, source)

    assert report.findings == ()
    assert report.failed_to_check == ()
    assert report.ok()


def test_a_value_failing_the_pattern_is_malformed_and_shows_the_pattern():
    """
    Test that a value which cannot be right is flagged, with the form it must take

    `r1i1pf1` has no number after `p`, so no run could ever carry it.
    The suggestion is the pattern itself: a regular expression is a poor thing
    to show a user, but it is the only description of the form we have.
    """
    canonical = canonical_cmip6(variant_label="r1i1pf1")
    source = StubSource({}, patterns={"variant_label": re.compile(VARIANT_PATTERN)})

    report = check_query_values_low(canonical, source)

    (finding,) = report.findings
    assert finding.facet == "variant_label"
    assert finding.kind is FindingKind.MALFORMED
    assert finding.suggestions == (VARIANT_PATTERN,)
    assert report.failed_to_check == ()


def test_a_listed_facet_is_tiered_like_any_other_even_when_generated():
    """
    Test that a listed variant_label goes through the same tiering as anything else

    Where an API lists a facet's values, "is this one of them?" is the same
    question for the facet as for anything else.
    In other words, check that variant_label isn't special.
    """
    canonical = canonical_cmip6(variant_label="r1i1pf1")
    source = StubSource({"variant_label": {"r1i1p1f1", "r1i1p2f1", "r2i1p1f1"}})

    report = check_query_values_low(canonical, source)

    (finding,) = report.findings
    assert finding.facet == "variant_label"
    assert finding.kind in (FindingKind.TYPO, FindingKind.UNKNOWN)
    assert report.failed_to_check == ()


def test_a_listed_value_which_is_present_is_not_flagged():
    """A value the source lists is fine, and counts as checked."""
    canonical = canonical_cmip6(variant_label="r1i1p1f1")
    source = StubSource({"variant_label": {"r1i1p1f1", "r2i1p1f1"}})

    report = check_query_values_low(canonical, source)

    assert report.findings == ()
    assert report.ok()


def test_a_facet_the_source_says_nothing_about_could_not_be_checked():
    """A source with no list and no pattern leaves the facet honestly unchecked."""
    canonical = canonical_cmip6(variant_label="r1i1pf1")
    source = StubSource({})  # no values, no patterns

    report = check_query_values_low(canonical, source)

    assert report.findings == ()
    assert report.failed_to_check == ("variant_label",)
    assert not report.ok()


def test_a_query_specific_facet_is_checked_when_the_source_lists_it():
    """
    Test that a facet with no canonical home is checked like any other

    CMIP5 `product` has no attribute on the canonical query, but the API names it
    and can list its values, so there is no reason not to check it.
    """
    canonical = to_canonical(QueryCMIP5(experiment="historical", product="output3"))
    source = StubSource({"product": {"output1", "output2"}})

    report = check_query_values_low(canonical, source)

    (finding,) = report.findings
    assert finding.facet == "product"
    assert finding.value == "output3"
    assert finding.kind is FindingKind.TYPO
    assert "product" not in report.failed_to_check


def test_the_close_match_function_can_be_injected():
    """
    Test that how "close" is decided can be replaced

    difflib's ratio is a default, not a rule: a caller who knows their vocabulary
    can decide closeness some other way without reaching into module globals.
    """
    canonical = canonical_cmip6(experiment_id="hist")

    (finding,) = compare_values(
        canonical,
        {"experiment": {"historical", "piControl"}},
        close_matches=lambda value, allowed: ("everything-is-close",),
    )

    assert finding.kind is FindingKind.TYPO
    assert finding.suggestions == ("everything-is-close",)


def test_a_finding_can_be_named_the_way_the_user_wrote_it():
    """
    Test that a canonical facet reads back under the name the query used

    Someone who wrote `QueryCMIP6(experiment_id=...)` should see `experiment_id`,
    not the `experiment` the checking is done in.
    """
    canonical = canonical_cmip6(experiment_id="Historical")
    source = StubSource({"experiment": {"historical"}})

    report = check_query_values_low(canonical, source)

    (finding,) = report.findings
    assert finding.facet == "experiment"
    assert report.facet_as_asked(finding.facet) == "experiment_id"


def test_a_query_specific_facet_already_carries_the_users_name():
    """A facet only the query names has no other name to be shown under."""
    canonical = to_canonical(QueryCMIP5(experiment="historical", product="output3"))
    source = StubSource({"product": {"output1", "output2"}})

    report = check_query_values_low(canonical, source)

    (finding,) = report.findings
    assert report.facet_as_asked(finding.facet) == "product"


def test_the_close_match_function_reaches_the_report_layer():
    """
    Test that the injected matcher is the one a whole report is built with

    Being able to replace it in `compare_values` alone would be no use:
    nobody calls that directly, they call the function which builds a report.
    """
    canonical = canonical_cmip6(experiment_id="hist")
    source = StubSource({"experiment": {"historical", "piControl"}})

    report = check_query_values_low(
        canonical,
        source,
        close_matches=lambda value, allowed: ("everything-is-close",),
    )

    (finding,) = report.findings
    assert finding.suggestions == ("everything-is-close",)


def test_values_set_for_reads_a_facet_the_query_holds():
    """Both kinds of facet are readable, whichever kind the caller is holding."""
    canonical = to_canonical(QueryCMIP5(experiment="historical", product="output1"))

    assert values_set_for(canonical, "experiment") == ("historical",)
    assert values_set_for(canonical, "product") == ("output1",)


def test_values_set_for_a_facet_the_query_cannot_hold_raises():
    """
    Test that being asked about a facet the query has no room for is an error

    Every facet we check comes from `facets_the_user_set`, so getting here with
    something else means a source answered a question we never put to it.
    Handing back "no values" would hide that, and would look exactly like a
    facet the user left empty.
    """
    canonical = to_canonical(QueryCMIP5(experiment="historical"))

    with pytest.raises(NotAFacetOfTheQueryError, match="sub_experiment_id"):
        values_set_for(canonical, "sub_experiment_id")


def test_a_facet_the_apis_vocabulary_cannot_express_is_not_asked_about():
    """
    Test that we do not build a request asking about a facet the API has no name for

    E.g. project with STAC APIs. project is not a facet with the STAC APIs,
    it is a prefix within the collection instead.
    """
    client = client_for(
        lambda request: httpx.Response(
            200,
            json={"id": "CMIP6", "summaries": {"cmip6:experiment_id": ["historical"]}},
        )
    )

    source = SearchAPIValuesSource(
        SearchAPI("stac.example", STAC_CMIP6, once()), client
    )
    canonical = canonical_cmip6(experiment_id="Historical")

    report = check_query_values_low(canonical, source)

    assert report.findings == (
        FacetFinding("experiment", "Historical", "case", ("historical",)),
    )
    assert report.failed_to_check == ("project",)


def test_a_canonically_built_query_reads_back_canonically():
    """
    Test that a query with no source query is named canonically

    There is no other name to give it, so the canonical one is the honest answer
    rather than a guess at which dialect the caller had in mind.
    """
    canonical = canonical_cmip6(experiment_id="Historical").model_copy(
        update={"source_query": None}
    )
    source = StubSource({"experiment": {"historical"}})

    report = check_query_values_low(canonical, source)

    (finding,) = report.findings
    assert report.facet_as_asked(finding.facet) == "experiment"
