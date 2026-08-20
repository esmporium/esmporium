"""
Record real responses from the live search APIs, for use as test fixtures

The recorded responses let the unit tests parse something an API really sent,
without those tests needing a network connection.
See `tests/unit/search/test_recorded_responses.py` for what is done with them.

They go stale, which is the point:
refresh them when an API changes and read the diff.
The cases here mirror those in
`tests/integration/search/test_esgf_generations_live.py`;
the unit tests fail loudly if a recording it expects is missing,
so the two cannot drift apart silently.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from esmporium.query import QueryCMIP5, QueryCMIP6, QueryCMIP7, to_canonical
from esmporium.search import (
    ESGF1Solr,
    ESGF15Bridge,
    ESGFNGStac,
    Request,
    SolrCMIP5Parameters,
    SolrCMIP6Parameters,
    StacCMIP6Parameters,
    StacCMIP7Parameters,
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

FACETS_TO_LIST = {"variable", "reporting_interval", "model"}
"""The facets to record the values of"""

CASES = (
    (
        "esgf1-solr-cmip5",
        "esgf.nci.org.au",
        ESGF1Solr(params=SolrCMIP5Parameters),
        QueryCMIP5(experiment="historical", variable="tas", time_frequency="mon"),
    ),
    (
        "esgf1-solr-cmip6",
        "esgf.nci.org.au",
        ESGF1Solr(params=SolrCMIP6Parameters),
        QueryCMIP6(experiment_id="historical", variable_id="tas", frequency="mon"),
    ),
    (
        "esgf15-bridge-cmip6",
        "esgf-node.ornl.gov",
        ESGF15Bridge(params=SolrCMIP6Parameters),
        QueryCMIP6(experiment_id="historical", variable_id="tas", frequency="mon"),
    ),
    (
        "esgf-ng-stac-cmip6",
        "search.east.esgf.io",
        ESGFNGStac(params=StacCMIP6Parameters),
        QueryCMIP6(experiment_id="historical", variable_id="tas", frequency="mon"),
    ),
    (
        "esgf-ng-stac-cmip7",
        "search.east.esgf.io",
        ESGFNGStac(params=StacCMIP7Parameters),
        QueryCMIP7(variable_id="tas"),
    ),
)
"""What to record: a name, the host to ask, the generation, and the query"""


def fetch(client: httpx.Client, host: str, request: Request) -> dict[str, Any]:
    """
    Send a request to a host and hand back the JSON it answered with

    Parameters
    ----------
    client
        The client to send with

    host
        The host to send to

    request
        The request to send, as built by a generation

    Returns
    -------
    :
        The raw JSON the host answered with
    """
    response = client.request(
        request.method,
        f"https://{host}{request.path}",
        params=request.params,
        json=request.json_body,
    )
    response.raise_for_status()

    res: dict[str, Any] = response.json()

    return res


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
        for name, host, generation, query in CASES:
            canonical = to_canonical(query)

            write(
                f"{name}-search",
                fetch(client, host, generation.build_request(canonical, LIMIT)),
            )
            write(
                f"{name}-facets",
                fetch(
                    client,
                    host,
                    generation.build_facets_request(canonical, FACETS_TO_LIST),
                ),
            )


if __name__ == "__main__":
    main()
