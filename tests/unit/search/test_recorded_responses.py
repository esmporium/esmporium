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
because a search API generation answers two kinds of question:
how to do searches
(currently only checked with how many results a search matched (`result_count`),
but this will expand when start parsing results to Datasets)
and which values a facet has (`parse_facet_values`).

Both are tested against the generation directly, rather than by mocking a
transport and going in through `search` or `check_query_values`.
What is under test here is the parsing, and the generation is the thing which
parses; the recording is already the API's own answer, so there is nothing left
to mock. Going in from the top would only add layers for a failure to be
reported through, and would leave the reader working out whether a broken
recording meant a broken parser or broken wiring.
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
    ESGF1Solr,
    ESGF15Bridge,
    ESGFNGStac,
    SolrCMIP5Parameters,
    SolrCMIP6Parameters,
    StacCMIP6Parameters,
    StacCMIP7Parameters,
    native_facet_names,
)

RECORDED_DIR = Path(__file__).parents[2] / "test-data" / "search"
"""Where the recorded responses live"""

PROBE_FACETS = {"variable", "reporting_interval", "model"}
"""
A few facets every project has, to check the values by eye against

`tas` is the probe because every project publishes it,
and it is what the recorded query asked for.
"""


def every_facet(generation):
    """
    Get every facet a generation's vocabulary can express

    This has to match `facets_to_list` in `scripts/record_search_responses.py`:
    asking here for something the recording never asked the API about
    would only prove that it is not in the file.

    Parameters
    ----------
    generation
        The generation whose vocabulary to read

    Returns
    -------
    :
        The facets it can express, named the way they are asked for
    """
    return set(facet_spec(generation.params).expressible_facets)


RECORDED_CASES = (
    pytest.param(
        "esgf1-solr-cmip5", ESGF1Solr(params=SolrCMIP5Parameters), id="esgf1-solr-cmip5"
    ),
    pytest.param(
        "esgf1-solr-cmip6", ESGF1Solr(params=SolrCMIP6Parameters), id="esgf1-solr-cmip6"
    ),
    pytest.param(
        "esgf15-bridge-cmip6",
        ESGF15Bridge(params=SolrCMIP6Parameters),
        id="esgf15-bridge-cmip6",
    ),
    pytest.param(
        "esgf-ng-stac-cmip6",
        ESGFNGStac(params=StacCMIP6Parameters),
        id="esgf-ng-stac-cmip6",
    ),
    pytest.param(
        "esgf-ng-stac-cmip7",
        ESGFNGStac(params=StacCMIP7Parameters),
        id="esgf-ng-stac-cmip7",
    ),
)
"""Each recording, with the generation which asked for it"""


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


@pytest.mark.parametrize("name, generation", RECORDED_CASES)
def test_result_count_of_a_recorded_search(name, generation):
    """Test that we can count the matches in a response an API really sent"""
    raw = load(f"{name}-search")

    assert generation.result_count(raw) > 0


@pytest.mark.parametrize("name, generation", RECORDED_CASES)
def test_parse_facet_values_of_a_recorded_response(name, generation):
    """Test that we can read the facet values out of a response an API really sent"""
    raw = load(f"{name}-facets")

    res = generation.parse_facet_values(raw, PROBE_FACETS)

    assert set(res) <= PROBE_FACETS, "we were told about a facet we did not ask about"
    assert "tas" in res["variable"]
    assert res["model"]
    assert res["reporting_interval"]


@pytest.mark.parametrize("name, generation", RECORDED_CASES)
def test_recorded_facet_values_are_well_formed(name, generation):
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
    facets = every_facet(generation)

    res = generation.parse_facet_values(raw, facets)

    assert res, "no facet was reported at all"
    assert set(res) <= facets, "we were told about a facet we did not ask about"
    for facet, values in res.items():
        assert values, f"{facet} was reported with no values"
        assert all(isinstance(value, str) and value for value in values), (
            f"{facet} was reported with a value which is not a non-empty string"
        )


STAC_RECORDED_CASES = tuple(case for case in RECORDED_CASES if "stac" in str(case.id))
"""The recorded cases whose API describes its facet values in a STAC collection"""


@pytest.mark.parametrize("name, generation", STAC_RECORDED_CASES)
def test_recorded_facets_which_are_not_enumerated_are_left_out(name, generation):
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
    facets = every_facet(generation)

    res = generation.parse_facet_values(raw, facets)

    prefix = f"{generation.params.prefix}:"
    asked_for = {
        native: asked
        for asked, native in native_facet_names(generation.params, facets).items()
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


@pytest.mark.parametrize("name, generation", STAC_RECORDED_CASES)
def test_recorded_variant_label_is_summarised_as_a_pattern(name, generation):
    """
    Test that the generated identifier we build on really is described as a pattern

    `variant_label` being a pattern rather than a list is the reason the value
    checker treats it differently from a controlled vocabulary,
    so it is worth pinning against real data rather than only against
    a collection we wrote ourselves.
    """
    raw = load(f"{name}-facets")

    (native,) = native_facet_names(generation.params, {"variant_label"}).values()
    summary = raw["summaries"][f"{generation.params.prefix}:{native}"]

    assert isinstance(summary, str), (
        f"{native} was summarised as a {type(summary).__name__}, not a pattern"
    )
    re.compile(summary)
