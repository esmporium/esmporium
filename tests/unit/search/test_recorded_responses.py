"""
Test our parsing against responses the live APIs really sent

The other unit tests parse responses we wrote ourselves,
which pins our behaviour but cannot tell us
whether we understood the API's shape correctly in the first place.
These parse recordings of real answers, and so can, without a network connection.

The recordings go stale.
That is the trade: they will not notice an API changing shape until they are
refreshed. Refresh them with `uv run python scripts/record_search_responses.py`
and read the diff.

Two kinds of recording are read here,
because a search API facade answers two kinds of question:
how to do searches
(currently only checked with how many results a search matched
(`get_search_result_n_matches`),
but this will expand when we start parsing results to Datasets)
and which values a facet has (`parse_facet_values`).

The count is read off the wire-format layer (`facade.search_api`) directly,
because it is keyed the same way whatever vocabulary asked for it.
The facet values are read through the facade, because reading them back into the
canonical vocabulary is the facade's job.
The wiring is covered on its own, with mocked responses we wrote, in
`test_search.py` and `test_check_query_values.py`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from esmporium.query import facet_spec
from esmporium.search import (
    ESGF1CMIP5ParametersQueryStyle,
    SearchAPIESGF1Solr,
    SearchAPIESGF15BridgeSolr,
    SearchAPIESGFNGSTAC,
    SearchAPIFacade,
    SolrCMIP6Parameters,
    STACCMIP6Parameters,
    STACCMIP7Parameters,
    build_transient_retrying,
    get_mapping_to_native_facet_names,
)

RECORDED_DIR = Path(__file__).parents[2] / "test-data" / "search"
"""Where the recorded responses live"""

PROBE_FACETS = {"variable", "reporting_interval", "model"}
"""
A few facets every project has, to check the values by eye against

`tas` is the probe because every project publishes it,
and it is what the recorded query asked for.
"""


def facade(query_style, search_api_cls, host="recorded.example") -> SearchAPIFacade:
    """
    Build a facade for parsing a recording

    The host and retry policy are irrelevant here (nothing is sent),
    so any values will do.
    """
    return SearchAPIFacade(
        parameters=query_style,
        search_api=search_api_cls(host, build_transient_retrying(1)),
    )


def every_facet(facade: SearchAPIFacade) -> set[str]:
    """
    Get every facet a facade's vocabulary can express

    This has to match `facets_to_list` in `scripts/record_search_responses.py`:
    asking here for something the recording never asked the API about
    would only prove that it is not in the file.

    Parameters
    ----------
    facade
        The facade whose vocabulary to read

    Returns
    -------
    :
        The facets it can express, named the way they are asked for
    """
    return set(facet_spec(facade.parameters).expressible_facets)


RECORDED_CASES = (
    pytest.param(
        "esgf1-solr-cmip5",
        facade(ESGF1CMIP5ParametersQueryStyle, SearchAPIESGF1Solr),
        id="esgf1-solr-cmip5",
    ),
    pytest.param(
        "esgf1-solr-cmip6",
        facade(SolrCMIP6Parameters, SearchAPIESGF1Solr),
        id="esgf1-solr-cmip6",
    ),
    pytest.param(
        "esgf15-bridge-cmip6",
        facade(SolrCMIP6Parameters, SearchAPIESGF15BridgeSolr),
        id="esgf15-bridge-cmip6",
    ),
    pytest.param(
        "esgf-ng-stac-cmip6",
        facade(STACCMIP6Parameters, SearchAPIESGFNGSTAC),
        id="esgf-ng-stac-cmip6",
    ),
    pytest.param(
        "esgf-ng-stac-cmip7",
        facade(STACCMIP7Parameters, SearchAPIESGFNGSTAC),
        id="esgf-ng-stac-cmip7",
    ),
)
"""Each recording, with the facade which asked for it"""


def load(name):
    """
    Load a recorded response

    Parameters
    ----------
    name
        The name it was recorded under

    Returns
    -------
    :
        The recorded response
    """
    path = RECORDED_DIR / f"{name}.json"
    assert path.exists(), (
        f"No recording at {path}. "
        "Record it with `uv run python scripts/record_search_responses.py`."
    )

    return json.loads(path.read_text())


@pytest.mark.parametrize("name, facade", RECORDED_CASES)
def test_result_count_of_a_recorded_search(name, facade):
    """Test that we can count the matches in a response an API really sent"""
    raw = load(f"{name}-search")

    assert facade.search_api.get_search_result_n_matches(raw) > 0


@pytest.mark.parametrize("name, facade", RECORDED_CASES)
def test_parse_facet_values_of_a_recorded_response(name, facade):
    """Test that we can read the facet values out of a response an API really sent"""
    raw = load(f"{name}-facets")

    res = facade.parse_facet_values(raw, PROBE_FACETS)

    assert set(res) <= PROBE_FACETS, "we were told about a facet we did not ask about"
    assert "tas" in res["variable"]
    assert res["model"]
    assert res["reporting_interval"]


@pytest.mark.parametrize("name, facade", RECORDED_CASES)
def test_recorded_facet_values_are_well_formed(name, facade):
    """
    Test the shape of what we hand back, on real data

    Every facet the vocabulary can express is asked about, so this covers the
    facets which the APIs describe in ways that are not a list of values,
    as well as those they do. It also covers the dialect-specific names
    (`product` on CMIP5, and so on), which are the ones we guessed at.

    A facet we report has to have at least one value:
    reporting a facet with nothing in it would be read as
    "this facet has no valid values", which is never what we mean.
    """
    raw = load(f"{name}-facets")
    facets = every_facet(facade)

    res = facade.parse_facet_values(raw, facets)

    assert res, "no facet was reported at all"
    assert set(res) <= facets, "we were told about a facet we did not ask about"
    for facet, values in res.items():
        assert values, f"{facet} was reported with no values"
        assert all(isinstance(value, str) and value for value in values), (
            f"{facet} was reported with a value which is not a non-empty string"
        )


STAC_RECORDED_CASES = tuple(case for case in RECORDED_CASES if "stac" in str(case.id))
"""The recorded cases whose API describes its facet values in a STAC collection"""


@pytest.mark.parametrize("name, facade", STAC_RECORDED_CASES)
def test_recorded_facets_which_are_not_enumerated_are_left_out(name, facade):
    """
    Test that a facet the API describes without listing its values is left out

    A STAC collection summarises some facets as a regular expression or as a
    range rather than as a list, because their values are generated rather than
    chosen from a vocabulary (`variant_label` is the standing example).

    What counts as "not enumerated" is read out of the recording itself rather
    than hard coded, so this keeps testing the right thing
    if the API starts listing something it used to describe as a pattern.
    """
    raw = load(f"{name}-facets")
    facets = every_facet(facade)

    res = facade.parse_facet_values(raw, facets)

    prefix = f"{facade.parameters.prefix}:"

    asked_for = {
        native: asked
        for asked, native in get_mapping_to_native_facet_names(
            facade.parameters, facets
        ).items()
    }
    not_enumerated = {
        asked
        for property_name, summary in raw["summaries"].items()
        if property_name.startswith(prefix)
        and (asked := asked_for.get(property_name[len(prefix) :])) is not None
        and not (
            isinstance(summary, list)
            and any(isinstance(value, str) for value in summary)
        )
    }

    assert not_enumerated, (
        "this recording enumerates every facet, so it cannot test that "
        "a non-enumerated one is left out"
    )
    assert not_enumerated.isdisjoint(res), (
        "a facet the collection did not enumerate was reported as having values"
    )


@pytest.mark.parametrize("name, facade", STAC_RECORDED_CASES)
def test_recorded_variant_label_is_summarised_as_a_pattern(name, facade):
    """
    Test that the generated identifier we build on really is described as a pattern

    `variant_label` being a pattern rather than a list is the reason the value
    checker treats it differently from a controlled vocabulary,
    so it is worth pinning against real data rather than only against
    a collection we wrote ourselves.
    """
    raw = load(f"{name}-facets")

    (native,) = get_mapping_to_native_facet_names(
        facade.parameters, {"variant_label"}
    ).values()
    summary = raw["summaries"][f"{facade.parameters.prefix}:{native}"]

    assert isinstance(summary, str), (
        f"{native} was summarised as a {type(summary).__name__}, not a pattern"
    )
    re.compile(summary)
