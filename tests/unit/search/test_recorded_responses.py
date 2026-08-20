"""
Test our parsing against responses the live APIs really sent

The other unit tests parse responses we wrote ourselves,
which pins our behaviour but cannot tell us
whether we understood the API's shape correctly in the first place.
These parse recordings of real answers, and so can, without a network connection.

The recordings go stale.
That is the trade: they will not notice an API changing shape until they are
refreshed, whereas the integration tests notice immediately but only run when
the nodes are up. Refresh them with `uv run python scripts/record_search_responses.py`
and read the diff.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from esmporium.search import (
    ESGF1Solr,
    ESGF15Bridge,
    ESGFNGStac,
    SolrCMIP5Parameters,
    SolrCMIP6Parameters,
    StacCMIP6Parameters,
    StacCMIP7Parameters,
)

RECORDED_DIR = Path(__file__).parents[2] / "test-data" / "search"
"""Where the recorded responses live"""

FACETS = {"variable", "reporting_interval", "model"}
"""
The facets the recorded facets responses were asked for

This has to match `FACETS_TO_LIST` in `scripts/record_search_responses.py`:
asking here for something the recording never asked the API about
would only prove that it is not in the file.
"""

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
    """
    Test that we can read the facet values out of a response an API really sent

    `tas` is the probe because every project publishes it,
    and it is what the recorded query asked for.
    """
    raw = load(f"{name}-facets")

    res = generation.parse_facet_values(raw, FACETS)

    assert set(res) <= FACETS, "we were told about a facet we did not ask about"
    assert "tas" in res["variable"]
    assert res["model"]
    assert res["reporting_interval"]


@pytest.mark.parametrize("name, generation", RECORDED_CASES)
def test_recorded_facet_values_are_well_formed(name, generation):
    """
    Test the shape of what we hand back, on real data

    A facet we report has to have at least one value:
    reporting a facet with nothing in it would be read as
    "this facet has no valid values", which is never what we mean.
    """
    raw = load(f"{name}-facets")

    res = generation.parse_facet_values(raw, FACETS)

    for facet, values in res.items():
        assert values, f"{facet} was reported with no values"
        assert all(isinstance(value, str) and value for value in values), (
            f"{facet} was reported with a value which is not a non-empty string"
        )
