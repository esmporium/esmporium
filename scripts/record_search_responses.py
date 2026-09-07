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

import logging
from pathlib import Path

import httpx
import pandas as pd

from esmporium.query import (
    QueryCMIP7,
    to_canonical,
)
from esmporium.search import (
    ESGFNG_CMIP7_FACADE_PARAMETERS,
    SearchAPIESGFNGSTAC,
    SearchAPIFacade,
    build_transient_retrying,
    fire,
)

OUT_DIR = Path(__file__).parents[1] / "tests" / "test-data" / "search"
"""Where the recorded responses are written"""

TIMEOUT = 60.0
"""How long to wait for a node, in seconds"""

LIMIT = 10_000
"""
How many records to ask for

Enough to see the shape of a record, few enough to keep the files reviewable.
"""


CASES = (
    # (
    #     "esgf1-solr-cmip5",
    #     SearchAPIFacade(
    #         ESGF1_CMIP5_FACADE_PARAMETERS,
    #         SearchAPIESGF1Solr("esgf.nci.org.au", build_transient_retrying(2)),
    #     ),
    #     QueryCMIP5(experiment="historical", variable="tas", time_frequency="mon"),
    # ),
    # (
    #     "esgf1-solr-cmip6",
    #     SearchAPIFacade(
    #         ESGF1_CMIP6_FACADE_PARAMETERS,
    #         SearchAPIESGF1Solr("esgf.nci.org.au", build_transient_retrying(2)),
    #     ),
    #     QueryCMIP6(experiment_id="historical", variable_id="tas", frequency="mon"),
    # ),
    # (
    #     "esgf15-bridge-cmip6",
    #     SearchAPIFacade(
    #         ESGF1_CMIP6_FACADE_PARAMETERS,
    #         SearchAPIESGF15BridgeSolr(
    #             "esgf-node.ornl.gov", build_transient_retrying(2)
    #         ),
    #     ),
    #     QueryCMIP6(experiment_id="historical", variable_id="tas", frequency="mon"),
    # ),
    # (
    #     "esgf-ng-stac-cmip6-east",
    #     SearchAPIFacade(
    #         ESGFNG_CMIP6_FACADE_PARAMETERS,
    #         SearchAPIESGFNGSTAC("search.east.esgf.io", build_transient_retrying(2)),
    #     ),
    #     QueryCMIP6(),
    # ),
    (
        "esgf-ng-stac-cmip7-east",
        SearchAPIFacade(
            ESGFNG_CMIP7_FACADE_PARAMETERS,
            SearchAPIESGFNGSTAC("search.east.esgf.io", build_transient_retrying(2)),
        ),
        QueryCMIP7(),
    ),
    # West is recorded alongside east because the two are not identical:
    # they disagree on where the match count lives
    # (see `get_search_result_n_matches` in `esmporium.search.apis.esgfng`),
    # so a real west response is worth parsing against.
    (
        "esgf-ng-stac-cmip7-west",
        SearchAPIFacade(
            ESGFNG_CMIP7_FACADE_PARAMETERS,
            SearchAPIESGFNGSTAC("search.west.esgf.io", build_transient_retrying(2)),
        ),
        QueryCMIP7(),
    ),
)
"""What to record: a name, the facade to ask with, and the query"""


def main() -> None:
    """Record a search response and a facets response for every case"""
    logger = logging.getLogger("esmporium")
    logger.setLevel(logging.DEBUG)

    logger.propagate = False

    console_handler = logging.StreamHandler()
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s p=%(process)d t=%(thread)d %(name)s %(message)s"
    )
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    res = {}
    with httpx.Client(follow_redirects=True, timeout=TIMEOUT) as client:
        for name, facade, query in CASES:
            canonical = to_canonical(query)

            res[name] = fire(
                client,
                facade.search_api,
                facade.build_search_request(canonical, LIMIT),
            )

    summary = []
    for name, res_n in res.items():
        print(f"{name}: n={len(res_n['features'])}")
        for feature in res_n["features"]:
            tmp = {
                "model": feature["properties"]["cmip7:source_id"],
                "variable": feature["properties"]["cmip7:variable_id"],
                "variant_label": feature["properties"]["cmip7:variant_label"],
                "frequency": feature["properties"]["cmip7:frequency"],
                "experiment": feature["properties"]["cmip7:experiment_id"],
                "version": feature["properties"]["version"],
            }
            if tmp not in summary:
                summary.append(tmp)

    summary_df = pd.DataFrame(summary)
    print(summary_df["model"].unique())
    print(
        summary_df[
            (summary_df["variable"] == "tas") & (summary_df["frequency"] == "mon")
        ]
        .sort_values(by=["model", "experiment", "variant_label"])
        .set_index(["model", "experiment"])
    )


if __name__ == "__main__":
    main()
