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
    Request,
    SearchAPIESGF1Solr,
    SearchAPIESGF15BridgeSolr,
    SearchAPIESGFNGSTAC,
    SearchAPIFacade,
    SolrCMIP5Parameters,
    SolrCMIP6Parameters,
    STACCMIP6Parameters,
    STACCMIP7Parameters,
    build_transient_retrying,
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


def facets_to_list(facade: SearchAPIFacade) -> set[str]:
    """
    Work out which facets to record the values of, for one facade

    Everything the facade's vocabulary can express, rather than a fixed few.

    Parameters
    ----------
    facade
        The facade whose vocabulary to read

    Returns
    -------
    :
        The facets to ask about, named the way they are asked for
    """
    return set(facet_spec(facade.query_style).expressible_facets)


def _facade(query_style: Any, search_api: Any) -> SearchAPIFacade:
    """Pair a query style with a search API, retrying transient failures twice"""
    return SearchAPIFacade(query_style=query_style, search_api=search_api)


CASES = (
    (
        "esgf1-solr-cmip5",
        _facade(
            SolrCMIP5Parameters,
            SearchAPIESGF1Solr("esgf.nci.org.au", build_transient_retrying(2)),
        ),
        QueryCMIP5(experiment="historical", variable="tas", time_frequency="mon"),
    ),
    (
        "esgf1-solr-cmip6",
        _facade(
            SolrCMIP6Parameters,
            SearchAPIESGF1Solr("esgf.nci.org.au", build_transient_retrying(2)),
        ),
        QueryCMIP6(experiment_id="historical", variable_id="tas", frequency="mon"),
    ),
    (
        "esgf15-bridge-cmip6",
        _facade(
            SolrCMIP6Parameters,
            SearchAPIESGF15BridgeSolr(
                "esgf-node.ornl.gov", build_transient_retrying(2)
            ),
        ),
        QueryCMIP6(experiment_id="historical", variable_id="tas", frequency="mon"),
    ),
    (
        "esgf-ng-stac-cmip6",
        _facade(
            STACCMIP6Parameters,
            SearchAPIESGFNGSTAC("search.east.esgf.io", build_transient_retrying(2)),
        ),
        QueryCMIP6(experiment_id="historical", variable_id="tas", frequency="mon"),
    ),
    (
        "esgf-ng-stac-cmip7",
        _facade(
            STACCMIP7Parameters,
            SearchAPIESGFNGSTAC("search.east.esgf.io", build_transient_retrying(2)),
        ),
        QueryCMIP7(variable_id="tas"),
    ),
)
"""What to record: a name, the facade to ask with, and the query"""


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
        for name, facade, query in CASES:
            canonical = to_canonical(query)
            host = facade.search_api.host

            write(
                f"{name}-search",
                fetch(client, host, facade.build_search_request(canonical, LIMIT)),
            )
            write(
                f"{name}-facets",
                fetch(
                    client,
                    host,
                    facade.build_get_facet_values_request(
                        canonical, facets_to_list(facade)
                    ),
                ),
            )


if __name__ == "__main__":
    main()
