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

Solr cases also get a `-files` recording, from the `type=File` request
that is the only way to get per-file URLs out of ESGF-1.
See `FILE_CASES` for why that is not every case.
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

FILE_CASES = (
    "esgf1-solr-cmip5",
    "esgf1-solr-cmip6",
)
"""
Which of [CASES][(m).CASES] to follow up with a `type=File` request

Only the Solr cases, and only the fast ones. ESGF-NG needs no follow-up at all --
a STAC item's `assets` are its files, so they are already in its `-search` recording.
The ESGF-1.5 bridge is left out because its `type=File` answers take minutes
(a QTime of well over two of them has been seen), which is too slow to put in
anyone's way when refreshing these.
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
    # West is recorded alongside east because the two are not guaranteed to be
    # identical.
    (
        "esgf-ng-stac-cmip6-west",
        INBUILT_SEARCH_API_FACADE_STORE.get_api_facade_for_project_from_host(
            "CMIP6",
            "search.west.esgf.io",
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


class NoFilesRecordedError(RuntimeError):
    """Raised when a Solr case's `type=File` request answers with nothing"""

    def __init__(self, name: str, dataset_ids: list[str]) -> None:
        super().__init__(
            f"Asking about {len(dataset_ids)} dataset(s) for the {name!r} case "
            "returned no file documents, so there is nothing worth recording. "
            "The datasets asked about were: "
            f"{dataset_ids}. "
            "Note that a dataset document existing does not guarantee its file "
            "documents are still indexed -- a superseded version's files are "
            "reindexed away while its dataset document lingers -- so this can mean "
            "the search picked up only stale versions rather than that anything is "
            "broken."
        )


def record_files(
    client: httpx.Client,
    name: str,
    facade: Any,
    canonical: Any,
    search: dict[str, Any],
) -> None:
    """
    Record the `type=File` answer for the datasets in a Solr search response

    ESGF-1 publishes no per-file URLs on a dataset document, so a second request is
    what gets them. The datasets asked about are taken from the response just
    recorded, rather than hard-coded, so that a refresh stays internally consistent:
    the `-files` recording is always about datasets in the `-search` recording beside
    it.

    *All* of them are asked about in one request, both because that is what
    `esmporium.db.record_solr_file_access` really does -- so the recording exercises
    the id OR-ing too -- and because it is more robust: a dataset document can
    outlive the indexing of its files, so asking only about the first one can come
    back empty while a later one in the same response has files.

    The variable to narrow by comes from the *query*, not from the documents. A CMIP5
    dataset document lists every variable in its table -- 56 of them, in the
    recording this was first written against -- so reading it off a document would
    ask about all of them at once and get nothing back.

    Parameters
    ----------
    client
        The HTTP client to send with

    name
        The case's name

    facade
        The case's facade

    canonical
        The case's query, canonicalised, to read the variable asked for out of

    search
        The search response just recorded, to take the dataset ids out of

    Raises
    ------
    NoFilesRecordedError
        None of those datasets have file documents to record
    """
    dataset_ids = [
        doc["id"] for doc in facade.search_api.extract_result_documents(search)
    ]
    variable_field = facade.parameters.get_mapping_to_api_facet_names({"variable"})[
        "variable"
    ]
    asked_for = facade.parameters.get_search_request_facet_values(canonical)

    raw = fire(
        client,
        facade.search_api,
        facade.search_api.build_file_search_request(
            dataset_ids, LIMIT, {variable_field: asked_for[variable_field]}
        ),
    )
    if not facade.search_api.extract_result_documents(raw):
        raise NoFilesRecordedError(name, dataset_ids)

    write(f"{name}-files", raw)


def main() -> None:
    """Record a search response and a facets response for every case"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with httpx.Client(follow_redirects=True, timeout=TIMEOUT) as client:
        for name, facade, query in CASES:
            canonical = to_canonical(query)

            search = fire(
                client,
                facade.search_api,
                facade.build_search_request(canonical, LIMIT),
            )
            write(f"{name}-search", search)
            if name in FILE_CASES:
                record_files(client, name, facade, canonical, search)
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
