"""
Integration tests of the search API
"""

from __future__ import annotations

import re

import pytest

from esmporium.query import (
    Query,
    QueryCMIP5,
    QueryCMIP6,
    QueryCMIP7,
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
        "{'reporting_interval': 'frequency', 'model': 'source_id'}. "
        "Either set 'reporting_interval' and 'model' via the query "
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


def test_search_other_terms_clash_alongside_a_query_specific_facet():
    """
    A query-specific facet does not get in the way of explaining a clash

    A query-specific facet (`sub_experiment_id` here) has no canonical
    equivalent, so it cannot be translated into an API name.
    The clash is on `source_id`, which can be,
    and it is that mapping the message has to show.
    """
    query = QueryCMIP6(
        source_id="ACCESS-CM2",
        sub_experiment_id="s1960",
        other_terms={"source_id": "other"},
    )

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


def test_search_other_terms_clash_on_a_query_specific_facet():
    """
    A clash on a query-specific facet says so without a translation to show
    """
    query = QueryCMIP6(
        sub_experiment_id="s1960", other_terms={"sub_experiment_id": "other"}
    )

    error_msg = re.escape(
        "('esgf.nci.org.au', 'SearchAPIESGF1Solr', 'ESGF1CMIP6ParametersQueryStyle'): "
        "`other_terms` facet 'sub_experiment_id' clashes with the query's facet names, "
        "once the query's facet names are translated to the API's names. "
        "The relevant mapping from query names to API names is: "
        "{'sub_experiment_id': 'sub_experiment_id'}. "
        "Either set 'sub_experiment_id' via the query "
        "or set 'sub_experiment_id' via `other_terms`, don't do both. "
        "query=QueryCMIP6"
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


def test_search_other_terms_clash_alongside_a_query_specific_facet_cmip5():
    """
    The same holds for a query style with its own project-specific facet

    CMIP5's `product` is query-specific, and the clash is on `model`,
    which the query calls `model` too.
    """
    query = QueryCMIP5(
        model="ACCESS1.0", product="output1", other_terms={"model": "other"}
    )

    error_msg = re.escape(
        "('esgf.nci.org.au', 'SearchAPIESGF1Solr', 'ESGF1CMIP5ParametersQueryStyle'): "
        "`other_terms` facet 'model' clashes with the query's facet names, "
        "once the query's facet names are translated to the API's names. "
        "The relevant mapping from query names to API names is: {'model': 'model'}. "
        "Either set 'model' via the query or set 'model' via `other_terms`, "
        "don't do both. query=QueryCMIP5"
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


def test_search_other_terms_clash_alongside_a_query_specific_facet_stac():
    """
    A query-specific facet is no obstacle for a prefixing API either

    CMIP7's `region` is query-specific.
    The clash is on `source_id`, which STAC spells `cmip7:source_id`,
    so that is the mapping the message shows.
    """
    query = QueryCMIP7(
        source_id="ACCESS-CM2",
        region="global",
        other_terms={"cmip7:source_id": "other"},
    )

    error_msg = re.escape(
        "('search.east.esgf.io', 'SearchAPIESGFNGSTAC', 'ESGFNGCMIP7ParametersQueryStyle'): "  # noqa: E501
        "`other_terms` facet 'cmip7:source_id' clashes with the query's facet names, "
        "once the query's facet names are translated to the API's names. "
        "The relevant mapping from query names to API names is: "
        "{'source_id': 'cmip7:source_id'}. "
        "Either set 'source_id' via the query "
        "or set 'cmip7:source_id' via `other_terms`, "
        "don't do both. query=QueryCMIP7"
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


def test_search_other_terms_clash_generic_and_query_specific_facet_cmip5():
    """
    A clash on both a generic and a query-specific facet renders sensibly too
    """
    query = QueryCMIP5(
        model="ACCESS1.0",
        product="output1",
        other_terms={"model": "other", "product": "output2"},
    )

    error_msg = re.escape(
        "('esgf.nci.org.au', 'SearchAPIESGF1Solr', 'ESGF1CMIP5ParametersQueryStyle'): "
        "`other_terms` facets 'model' and 'product' clash with the query's facet names, "  # noqa: E501
        "once the query's facet names are translated to the API's names. "
        "The relevant mapping from query names to API names is: "
        "{'model': 'model', 'product': 'product'}. "
        "Either set 'model' and 'product' via the query "
        "or set 'model' and 'product' via `other_terms`, don't do both. "
        "query=QueryCMIP5"
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


def test_search_other_terms_clash_generic_and_query_specific_facet_stac():
    """
    A clash on both a generic and a query-specific facet renders sensibly too
    """
    query = QueryCMIP7(
        source_id="ACCESS-CM2",
        region="global",
        other_terms={"cmip7:source_id": "other", "cmip7:region": "sh"},
    )

    error_msg = re.escape(
        "('search.east.esgf.io', 'SearchAPIESGFNGSTAC', 'ESGFNGCMIP7ParametersQueryStyle'): "  # noqa: E501
        "`other_terms` facets 'cmip7:region' and 'cmip7:source_id' "
        "clash with the query's facet names, "
        "once the query's facet names are translated to the API's names. "
        "The relevant mapping from query names to API names is: "
        "{'region': 'cmip7:region', 'source_id': 'cmip7:source_id'}. "
        "Either set 'region' and 'source_id' via the query "
        "or set 'cmip7:region' and 'cmip7:source_id' via `other_terms`, "
        "don't do both. query=QueryCMIP7"
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


def test_search_other_terms_clash_on_a_facet_the_facade_injects():
    """
    A clash on a facet the query does not name is still explained, not a crash

    The STAC facade puts `collection` in the request itself,
    working it out from the query's project rather than from a facet,
    so `collection` is in the request's facet values
    while being no facet of the query.
    There is no query name to offer as the alternative here,
    so the message says where the value came from instead.
    """
    query = QueryCMIP6(source_id="ACCESS-CM2", other_terms={"collection": "CMIP6"})

    error_msg = re.escape(
        "('search.east.esgf.io', 'SearchAPIESGFNGSTAC', 'ESGFNGCMIP6ParametersQueryStyle'): "  # noqa: E501
        "`other_terms` facet 'collection' clashes with the query's facet names, "
        "once the query's facet names are translated to the API's names. "
        "'collection' is set by the facade itself "
        "rather than by a facet of the query, "
        "so it cannot be set via `other_terms`. "
        "query=QueryCMIP6"
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


def test_search_other_terms_clash_on_a_query_facet_and_an_injected_facet():
    """
    A clash on both kinds of facet at once explains each one in its own terms
    """
    query = QueryCMIP6(
        source_id="ACCESS-CM2",
        other_terms={"collection": "CMIP6", "cmip6:source_id": "other"},
    )

    error_msg = re.escape(
        "('search.east.esgf.io', 'SearchAPIESGFNGSTAC', 'ESGFNGCMIP6ParametersQueryStyle'): "  # noqa: E501
        "`other_terms` facets 'cmip6:source_id' and 'collection' "
        "clash with the query's facet names, "
        "once the query's facet names are translated to the API's names. "
        "The relevant mapping from query names to API names is: "
        "{'source_id': 'cmip6:source_id'}. "
        "Either set 'source_id' via the query "
        "or set 'cmip6:source_id' via `other_terms`, don't do both. "
        "'collection' is set by the facade itself "
        "rather than by a facet of the query, "
        "so it cannot be set via `other_terms`. "
        "query=QueryCMIP6"
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
