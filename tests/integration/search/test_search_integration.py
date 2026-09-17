"""
Integration tests of the search API
"""

from __future__ import annotations

import re

import pytest

from esmporium.query import (
    Query,
    QueryCMIP6,
)
from esmporium.search import (
    INBUILT_SEARCH_API_FACADE_STORE,
    NoFacadeAnsweredError,
    build_list_selector,
    search,
)


def test_search_esgf1_request_raises_on_other_terms_clash():
    """
    The clash error message provides a clear message
    """
    query = QueryCMIP6(source_id="ACCESS-CM2", other_terms={"source_id": "other"})

    error_msg = re.escape(
        "('esgf.nci.org.au', 'SearchAPIESGF1Solr', 'ESGF1CMIP6ParametersQueryStyle'): "
        "`other_terms` facet 'source_id' clashes with the query's facet names, "
        "once the query's facet names are translated to the API's names. "
        "The relevant mapping from query names to API names is: "
        "{'source_id': 'source_id'}. "
        "Either set 'source_id' via the query or set 'source_id' via `other_terms`, "
        "don't do both. query=QueryCMIP6"
    )
    with pytest.raises(NoFacadeAnsweredError, match=error_msg):
        search(
            query,
            build_list_selector(
                [
                    INBUILT_SEARCH_API_FACADE_STORE.get_api_facade_for_project_from_host(
                        "CMIP6", "esgf.nci.org.au"
                    )
                ]
            ),
        )


def test_search_esgf1_request_raises_on_other_terms_clash_cross_project():
    """
    The clash error message provides a clear message
    """
    query = QueryCMIP6(source_id="ACCESS-CM2", other_terms={"model": "other"})

    error_msg = re.escape(
        "('esgf.nci.org.au', 'SearchAPIESGF1Solr', 'ESGF1CMIP5ParametersQueryStyle'): "
        "`other_terms` facet 'model' clashes with the query's facet names, "
        "once the query's facet names are translated to the API's names. "
        "The relevant mapping from query names to API names is: "
        "{'source_id': 'model'}. "
        "Either set 'source_id' via the query or set 'model' via `other_terms`, "
        "don't do both. query=QueryCMIP6"
    )
    with pytest.raises(NoFacadeAnsweredError, match=error_msg):
        search(
            query,
            build_list_selector(
                [
                    INBUILT_SEARCH_API_FACADE_STORE.get_api_facade_for_project_from_host(
                        "CMIP5", "esgf.nci.org.au"
                    )
                ]
            ),
        )


def test_search_esgfng_request_raises_on_other_terms_clash():
    """
    The clash error message provides a clear message
    """
    query = QueryCMIP6(source_id="ACCESS-CM2", other_terms={"cmip6:source_id": "other"})

    error_msg = re.escape(
        "('search.east.esgf.io', 'SearchAPIESGFNGSTAC', 'ESGFNGCMIP6ParametersQueryStyle'): "  # noqa: E501
        "`other_terms` facet 'cmip6:source_id' clashes with the query's facet names, "
        "once the query's facet names are translated to the API's names. "
        "The relevant mapping from query names to API names is: "
        "{'source_id': 'cmip6:source_id'}. "
        "Either set 'source_id' via the query "
        "or set 'cmip6:source_id' via `other_terms`, "
        "don't do both. query=QueryCMIP6"
    )
    with pytest.raises(NoFacadeAnsweredError, match=error_msg):
        search(
            query,
            build_list_selector(
                [
                    INBUILT_SEARCH_API_FACADE_STORE.get_api_facade_for_project_from_host(
                        "CMIP6", "search.east.esgf.io"
                    )
                ]
            ),
        )


def test_search_esgfng_request_raises_on_other_terms_clash_cross_project():
    """
    The clash error message provides a clear message
    """
    query = QueryCMIP6(
        table_id="Amon",
        other_terms={"cmip7:variable_branding_suffix": "other"},
        project="CMIP7",
    )

    error_msg = re.escape(
        "('search.east.esgf.io', 'SearchAPIESGFNGSTAC', 'ESGFNGCMIP7ParametersQueryStyle'): "  # noqa: E501
        "`other_terms` facet 'cmip7:variable_branding_suffix' "
        "clashes with the query's facet names, "
        "once the query's facet names are translated to the API's names. "
        "The relevant mapping from query names to API names is: "
        "{'table_id': 'cmip7:variable_branding_suffix'}. "
        "Either set 'table_id' via the query "
        "or set 'cmip7:variable_branding_suffix' via `other_terms`, "
        "don't do both. query=QueryCMIP6"
    )
    with pytest.raises(NoFacadeAnsweredError, match=error_msg):
        search(
            query,
            build_list_selector(
                [
                    INBUILT_SEARCH_API_FACADE_STORE.get_api_facade_for_project_from_host(
                        "CMIP7", "search.east.esgf.io"
                    )
                ]
            ),
        )


def test_search_raises_on_other_terms_multiple_clashes():
    """
    The clash error message provides a clear message
    """
    query = Query(
        model="ACCESS-CM2",
        reporting_interval="mon",
        project="CMIP6",
        other_terms={"source_id": "other", "frequency": "day"},
    )

    error_msg = re.escape(
        "('esgf.nci.org.au', 'SearchAPIESGF1Solr', 'ESGF1CMIP6ParametersQueryStyle'): "
        "`other_terms` facets 'frequency' and 'source_id' "
        "clash with the query's facet names, "
        "once the query's facet names are translated to the API's names. "
        "The relevant mapping from query names to API names is: "
        "{'model': 'source_id', 'reporting_interval': 'frequency'}. "
        "Either set 'model' and 'reporting_interval' via the query "
        "or set 'frequency' and 'source_id' via `other_terms`, don't do both. "
        "query=Query("
    )

    with pytest.raises(NoFacadeAnsweredError, match=error_msg):
        search(
            query,
            build_list_selector(
                [
                    INBUILT_SEARCH_API_FACADE_STORE.get_api_facade_for_project_from_host(
                        "CMIP6", "esgf.nci.org.au"
                    )
                ]
            ),
        )
