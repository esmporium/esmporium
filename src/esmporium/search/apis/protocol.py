"""
Search API protocol class
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Protocol

from tenacity import Retrying

from esmporium.search.apis.request import Request


class SearchAPI(Protocol):
    """
    A search API endpoint we can query

    These interfaces are low-level.
    They that mirror the ESGF search APIs directly.
    It is extremely easy to make invalid queries using interfaces of this type.
    If you want to make queries, we recommend using instances of
    [esmporium.search.search_api_facade.SearchAPIFacade][]'s instead
    because of their more robust query creation, result parsing and error handling.
    """

    host: str
    """The host that provides this API, e.g. `esgf.nci.org.au`"""

    retrying: Retrying
    """The retry policy to use when hitting this API"""

    timeout: float = 30.0
    """
    How long to wait on a single request to this host, in seconds

    Most hosts reply quickly e.g. 5 seconds.
    The slowest hosts take around 30 seconds.
    In general, you want to make this as short as possible
    because waiting for a reply that will never come
    can make your retries take forever.
    """

    scheme: str = "https"
    """
    The URL scheme to reach this host over
    """

    def build_search_request(
        self,
        facet_values: Mapping[str, tuple[str, ...]],
        limit: int,
    ) -> Request:
        """
        Build a search request to this API

        Parameters
        ----------
        facet_values
            Facet values to use in the search

        limit
            The PAGE size to ask for,
            i.e. the maximum number of records in one response.

            This is not the total number of matches;
            that comes back in the response itself
            and is what [get_search_result_count][(c).get_search_result_count] is for.

        Returns
        -------
        :
            The request to send
        """
        ...

    def get_search_result_count(self, raw: dict[str, Any]) -> int:
        """
        Get the total number of search results out of a raw response

        Parameters
        ----------
        raw
            The response to read, i.e. the answer to a
            [build_search_request][(c).build_search_request]

        Returns
        -------
        :
            The number of search results

        Raises
        ------
        NoResultCountReturned
            `raw` does not report a count we can read
        """
        # TODO: rename NoResultCountReturned to NoSearchResultCountReturned
        ...

    def build_get_facet_values_request(self, facets: set[str]) -> Request:
        """
        Build a request which lists the values of the given facets

        Parameters
        ----------
        facets
            The facets to list the values of.

        Returns
        -------
        :
            The request to send
        """
        ...

    def parse_facet_values(
        self, raw: dict[str, Any], facets: set[str]
    ) -> dict[str, set[str]]:
        """
        Read the available facet values out of a raw response

        Parameters
        ----------
        raw
            The response to read, i.e. the answer to a request built with
            [build_get_facet_values_request][(c).build_get_facet_values_request].

        facets
            The facets we asked about.

        Returns
        -------
        :
            The values which are available, keyed by the facet name.

            A facet whose values the API does not enumerate is left out
            (higher level functions are left to decide what to do
            about facets which are requested but not returned by this parsing).

        Raises
        ------
        NoFacetValuesReturned
            The response does not enumerate facet values at all
        """
        ...

    def parse_facet_patterns(
        self, raw: dict[str, Any], facets: set[str]
    ) -> dict[str, re.Pattern[str]]:
        """
        Read the supported facet patterns out of a raw response

        The counterpart to [parse_facet_values][(c).parse_facet_values],
        for the facets an API describes by their form rather than by listing them.
        A facet should be described one way or the other, never both,
        so the two should never report the same facet.

        Parameters
        ----------
        raw
            The response to read, i.e. the answer to a request built with
            [build_get_facet_values_request][(c).build_get_facet_values_request].

        facets
            The facets we asked about.

        Returns
        -------
        :
            The values which are available, keyed by the facet name.

            A facet this response does not describe with a pattern is left out
            (higher level functions are left to decide what to do
            about facets which are requested but not returned by this parsing).
        """
        ...
