"""
Show what the ESGF-NG STAC API answers with and without a `collection` facet

`SearchAPIESGFNGSTAC.build_search_request` treats `collection` as just another
facet: if the caller passes one it becomes an `in` clause, and if they don't,
no clause is written and nothing complains. The older `ESGFNGStac` generation
refused that second case outright (`OneProjectRequiredError`), so the question
this script answers is what the live API actually does with each shape.

For each case it prints the request body we build, then the response: the
number of matches, and which collections the returned records came from. That
last line is the interesting one - it says whether an unscoped search really
does range across collections.

It hits the live node, so it needs a network.

Run it:  uv run python scripts/esgfng_collection_facet_demo.py
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx

from esmporium.search.apis.esgfng import SearchAPIESGFNGSTAC
from esmporium.search.apis.protocol import NoSearchResultNumberOfMatchesReturned
from esmporium.search.retry import build_transient_retrying

API = SearchAPIESGFNGSTAC(
    host="search.east.esgf.io",
    retrying=build_transient_retrying(2),
)
"""The API to demo, i.e. the class under discussion pointed at a real node"""

LIMIT = 5
"""
How many records to ask for

Enough to see which collections answered, few enough to keep the output short.
"""

QUERY_FACETS: dict[str, tuple[str, ...]] = {
    # The caller puts the prefix on, as `build_search_request` assumes.
    "cmip6:experiment_id": ("historical",),
    "cmip6:variable_id": ("tas",),
    "cmip6:frequency": ("mon",),
}
"""The facets both cases search on, i.e. everything except the collection"""

CASES: tuple[tuple[str, Mapping[str, tuple[str, ...]]], ...] = (
    # ("with a collection facet", {"collection": ("CMIP6",), **QUERY_FACETS}),
    # ("without a collection facet", QUERY_FACETS),
    ("CMIP7 only", {"collection": ("CMIP7",)}),
)
"""What to demo: a description, and the facet values to search with"""


def describe_response(raw: dict[str, Any]) -> None:
    """
    Print what came back from a search

    Parameters
    ----------
    raw
        The response to describe
    """
    try:
        n_matches = str(API.get_search_result_n_matches(raw))
    except NoSearchResultNumberOfMatchesReturned as exc:
        n_matches = f"not reported ({exc})"

    features = raw.get("features", [])
    collections = sorted({f.get("collection", "<none>") for f in features})

    print(f"  matches: {n_matches}")
    print(f"  records returned: {len(features)}")
    print(f"  collections in those records: {', '.join(collections) or '<none>'}")


def run_case(
    client: httpx.Client, description: str, facet_values: Mapping[str, tuple[str, ...]]
) -> None:
    """
    Build, send and describe one search

    Parameters
    ----------
    client
        The client to send with

    description
        What this case is, for the reader

    facet_values
        The facet values to search with
    """
    request = API.build_search_request(facet_values, limit=1050)

    print(f"\n=== Search {description} ===")
    print("Request body:")
    print(json.dumps(request.json_body, indent=2))

    response = client.request(
        request.method,
        f"{API.scheme}://{API.host}{request.path}",
        params=request.params,
        json=request.json_body,
    )

    print(f"Response ({response.status_code}):")
    if response.is_error:
        # An error body is the answer here, not a reason to stop:
        # a rejected filter is exactly the sort of thing we want to see.
        print(f"  {response.text[:500]}")
        return

    describe_response(response.json())
    print(sorted(set(v["id"] for v in response.json()["features"])))
    tmp = []
    for v in response.json()["features"]:
        model = v["id"].split(".")[4]
        experiment = v["id"].split(".")[5]
        tmp.append((model, experiment))

    print(sorted(set(tmp)))


def main() -> None:
    """Run every case against the live node"""
    with httpx.Client(follow_redirects=True, timeout=API.timeout) as client:
        for description, facet_values in CASES:
            run_case(client, description, facet_values)


if __name__ == "__main__":
    main()
