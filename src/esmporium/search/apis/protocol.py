"""
Search API protocol class
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Protocol

from tenacity import Retrying

from esmporium.search.apis.request import Request


class LimitOutOfRangeError(ValueError):
    """
    Raised when a page size is one the search APIs will not accept
    """

    def __init__(self, limit: int, min_limit: int, max_limit: int) -> None:
        """
        Initialise the error

        Parameters
        ----------
        limit
            The page size which was asked for

        min_limit
            Minimum value of `limit` supported by the API

        max_limit
            Maximum value of `limit` supported by the API
        """
        self.limit = limit
        self.min_limit = min_limit
        self.max_limit = max_limit
        super().__init__(
            f"limit must be between {min_limit} and {max_limit}, received {limit}. "
            "If you want more records than that, paginate."
        )


class NoSearchResultNumberOfMatchesReturned(ValueError):
    """
    Raised when a search response does not say how many records matched a search
    """

    def __init__(self, raw: dict[str, Any], expected_at: str) -> None:
        """
        Initialise the error

        Parameters
        ----------
        raw
            The response we could not read a count out of

        expected_at
            Where in `raw` we looked for the number of records that matched the search
        """
        self.raw = raw
        self.expected_at = expected_at

        keys = ", ".join(sorted(raw)) if raw else "nothing at all"
        super().__init__(
            "This response does not report how many records matched the search. "
            f"We expected to read the count from {expected_at!r}, "
            # TODO: make this more robust as the issue might be for a key
            # lower than the top level.
            f"but the response's top-level keys are: {keys}."
        )


class NoFacetValuesReturned(ValueError):
    """
    Raised when a response does not enumerate facet values at all

    We don't expect this to happen,
    but if we expect to get facet values and don't, we want to be loud about it.
    """

    def __init__(self, raw: dict[str, Any], expected_at: str) -> None:
        """
        Initialise the error

        Parameters
        ----------
        raw
            The response we could not read facet values out of

        expected_at
            Where in `raw` we looked for the facet values
        """
        self.raw = raw
        self.expected_at = expected_at

        keys = ", ".join(sorted(raw)) if raw else "nothing at all"
        super().__init__(
            "This response does not report facet values. "
            f"We expected to read the facet values from {expected_at!r}, "
            # TODO: make this more robust as the issue might be for a key
            # lower than the top level.
            f"but the response's top-level keys are: {keys}."
        )


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

        Raises
        ------
        LimitOutOfRangeError
            `limit` is outside the range this search API accepts
        """
        ...

    def get_search_result_n_matches(self, raw: dict[str, Any]) -> int:
        """
        Get the number of records that matched a search from a raw response

        Note: this is not necessarily the same as the number of results in `raw`.
        Some search APIs will only return a limited number of results.
        This method should return the total number of records which matched the search,
        which can be much higher than the number of results returned in `raw`.

        Parameters
        ----------
        raw
            The response to read, i.e. the answer to a
            [build_search_request][(c).build_search_request]

        Returns
        -------
        :
            The number of records that matched the search

        Raises
        ------
        NoSearchResultNumberOfMatchesReturned
            `raw` does not report the number of records that matched the search
        """
        # TODO: ensure implmentations raise NoSearchResultNumberOfMatchesReturned
        # rather than NoResultCountReturned
        ...

    def build_get_facet_values_for_project_request(
        self, facets: set[str], project: str
    ) -> Request:
        """
        Build a request which lists the facet values that appear in a project

        This gets the facet values (e.g. historical, piControl),
        not the facet names (e.g. experiment_id).

        Parameters
        ----------
        facets
            The (names of the) facets to list the values of.

        project
            The project to get facet values for.

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
