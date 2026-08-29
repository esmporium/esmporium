"""
Demonstration of the fact that you can, with the low level API, get results from multiple projects back from a single query
"""  # noqa: E501

from __future__ import annotations

import logging
from typing import Any

from esmporium.query import QueryCMIP7
from esmporium.search import build_list_selector, search
from esmporium.search.search_api import SOLR_CMIP7, SearchAPI, build_transient_retrying

# TODO: add this to the live search use cases
# This query gives you back results from multiple projects with ESG1.
EXAMPLE_MULTI_PROJECT_RESULT = QueryCMIP7(
    project=("CMIP6", "CMIP7"),
    variable_id="tas",
    frequency="mon",
    institution_id="EC-Earth-Consortium",
    experiment_id=("esm-scen7-vl", "ssp126"),
    # Default is to give back all results,
    # retrated and not, so we might not have to deal with this.
    # If we do, a question for the future is: how best to handle the retraction toggle?
    # Maybe like distrib? (I guess it depends whether retracted is a query concept
    # or an API generation specific concept).
    # It also seems like you can't ask for retracted true and false
    # explicitly in the same search with the SOLR API
    # (only by saying nothing about retraction do you get back everything).
    # This complicates things further...
    #
    # The SOLR API also returns the parsed terms in its response
    # under ["responseHeader"]["params"]["fq"].
    # We could use this to help warn or error if we get back
    # parsed parameters that differ from what we expect
    # (that was how I noticed the issue with trying to do retracted true and false)
    #
    # Example specification
    # other_terms={"retracted": "false"},
    # This doesn't work, you only get back false results
    # other_terms={"retracted": ("false", "true")},
    # Check what this does i.e. how much checking work we need to do for the API
    # other_terms={"retracted": "junk"},
    #
    # Have to check how NG handles retractions too.
    # This could be its own PR I fear
    # (probably best to make an issue and leave this for now,
    # the default behaviour means we don't get bitten right now).
    #
)
DKRZ_SOLR = SearchAPI("esgf-data.dkrz.de", SOLR_CMIP7, build_transient_retrying(2))

QUERIES_SEARCH_APIS = [
    (EXAMPLE_MULTI_PROJECT_RESULT, DKRZ_SOLR),
    (EXAMPLE_MULTI_PROJECT_RESULT.model_copy(update={"project": None}), DKRZ_SOLR),
    (EXAMPLE_MULTI_PROJECT_RESULT.model_copy(update={"project": "CMIP6"}), DKRZ_SOLR),
    (EXAMPLE_MULTI_PROJECT_RESULT.model_copy(update={"project": "CMIP7"}), DKRZ_SOLR),
    # You cannot get results for multiple projects from one query with NG,
    # because of the project-specific prefix
]


def node_count_summary(raw: dict[str, Any]) -> str:
    """Summarise a raw response's match count without knowing its generation."""
    if "response" in raw:  # Solr-shaped (ESGF1 esg-search or the ESGF-1.5 bridge)
        return f"numFound={raw['response'].get('numFound')}"
    return f"numberMatched={raw.get('numberMatched')}"


def main() -> None:
    """Search each example query, print match counts, then the recorded health."""
    esmporium_logger = logging.getLogger("esmporium")
    esmporium_logger.setLevel(logging.DEBUG)

    console_handler = logging.StreamHandler()
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s p=%(process)d t=%(thread)d %(name)s %(message)s"
    )
    console_handler.setFormatter(formatter)
    esmporium_logger.addHandler(console_handler)

    for query, search_api in QUERIES_SEARCH_APIS:
        print(f"\nquery: {query!r}, search_api: {search_api!r}")
        results = search(
            query, stop_at_first_result=True, selector=build_list_selector([search_api])
        ).results
        for host, raw in results.items():
            projects_in_response = set(
                vv
                for v in [tuple(v["project"]) for v in raw["response"]["docs"]]
                for vv in v
            )
            print(f"  {host:22} {projects_in_response=} {node_count_summary(raw)}")


if __name__ == "__main__":
    main()
