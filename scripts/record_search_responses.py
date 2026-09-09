"""
Record real responses from the live search APIs, for use as test fixtures

The recorded responses let the unit tests parse something an API really sent,
without those tests needing a network connection.
See `tests/unit/search/test_recorded_responses.py` for what is done with them.

They go stale, which is the point:
refresh them when an API changes and read the diff.

The cases here mirror `RECORDED_CASES` in
`tests/unit/search/test_recorded_responses.py`,
and those tests fail loudly if a recording they expect is missing,
so the two cannot drift apart silently.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from esmporium.query import (
    QueryCMIP5,
    QueryCMIP6,
    QueryCMIP7,
    facet_spec,
    to_canonical,
)
from esmporium.search import (
    INBUILT_SEARCH_API_FACADE_STORE,
    fire,
)

OUT_DIR = Path(__file__).parents[1] / "tests" / "test-data" / "search"
"""Where the recorded responses are written"""

TIMEOUT = 60.0
"""How long to wait for a node, in seconds"""

LIMIT = 2
"""
How many records to ask for

Enough to see the shape of a record, few enough to keep the files reviewable.
"""


CASES = (
    (
        "esgf1-solr-cmip5",
        INBUILT_SEARCH_API_FACADE_STORE.get_api_facade_for_project_from_host(
            "CMIP5",
            "esgf.nci.org.au",
        ),
        QueryCMIP5(experiment="historical", variable="tas", time_frequency="mon"),
    ),
    (
        "esgf1-solr-cmip6",
        INBUILT_SEARCH_API_FACADE_STORE.get_api_facade_for_project_from_host(
            "CMIP6",
            "esgf.nci.org.au",
        ),
        QueryCMIP6(experiment_id="historical", variable_id="tas", frequency="mon"),
    ),
    (
        "esgf15-bridge-cmip6",
        INBUILT_SEARCH_API_FACADE_STORE.get_api_facade_for_project_from_host(
            "CMIP6",
            "esgf-node.ornl.gov",
        ),
        QueryCMIP6(experiment_id="historical", variable_id="tas", frequency="mon"),
    ),
    (
        "esgf-ng-stac-cmip6-east",
        INBUILT_SEARCH_API_FACADE_STORE.get_api_facade_for_project_from_host(
            "CMIP6",
            "search.east.esgf.io",
        ),
        QueryCMIP6(experiment_id="historical", variable_id="tas", frequency="mon"),
    ),
    (
        "esgf-ng-stac-cmip7-east",
        INBUILT_SEARCH_API_FACADE_STORE.get_api_facade_for_project_from_host(
            "CMIP7",
            "search.east.esgf.io",
        ),
        QueryCMIP7(variable_id="tas"),
    ),
    # West is recorded alongside east because the two are not identical:
    # e.g. they disagree on where the match count lives
    # so a real west response is worth parsing against.
    (
        "esgf-ng-stac-cmip7-west",
        INBUILT_SEARCH_API_FACADE_STORE.get_api_facade_for_project_from_host(
            "CMIP7",
            "search.west.esgf.io",
        ),
        QueryCMIP7(variable_id="tas"),
    ),
)
"""What to record: a name, the facade to ask with, and the query"""


def write(name: str, raw: dict[str, Any]) -> None:
    """
    Write one recorded response

    Parameters
    ----------
    name
        The name to write it under

    raw
        The response to write
    """
    path = OUT_DIR / f"{name}.json"
    # Sorted and indented so that a refresh produces a diff a human can read.
    path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n")
    print(f"{path.name}: {path.stat().st_size:,} bytes")


def main() -> None:
    """Record a search response and a facets response for every case"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with httpx.Client(follow_redirects=True, timeout=TIMEOUT) as client:
        for name, facade, query in CASES:
            canonical = to_canonical(query)

            write(
                f"{name}-search",
                fire(
                    client,
                    facade.search_api,
                    facade.build_search_request(canonical, LIMIT),
                ),
            )
            write(
                f"{name}-facets",
                fire(
                    client,
                    facade.search_api,
                    facade.build_get_facet_values_request(
                        canonical,
                        set(
                            facet_spec(
                                facade.parameters.base_query_style
                            ).expressible_facets
                        ),
                    ),
                ),
            )


if __name__ == "__main__":
    main()
