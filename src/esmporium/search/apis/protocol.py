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
    Raised when a page size is one a search API will not accept
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


class NoSearchResultNumberOfMatchesReturnedError(ValueError):
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

        expected_at_split = expected_at.split(".")
        current_level = raw
        current_level_keys = sorted(current_level)
        for level, part in enumerate(expected_at_split):
            if part in current_level_keys:
                # We have this part, keep looking
                current_level = current_level[part]
                current_level_keys = sorted(current_level)
                continue

            missing_part = part
            path_that_exists = ".".join(expected_at_split[:level])
            keys_at_path_that_exists = sorted(current_level)
            break

        else:
            found_value = current_level[part]
            expected_at_rep = "".join(f"[{part}]" for part in expected_at.split("."))
            msg = f"{expected_at} is in {raw}, raw{expected_at_rep}={found_value}"
            raise AssertionError(msg)

        if raw:
            if keys_at_path_that_exists:
                tmp = ", ".join(f"{v!r}" for v in sorted(keys_at_path_that_exists))
                keys_at_path_that_exists_string = f"there is only: {tmp}"
            else:
                keys_at_path_that_exists_string = "there are no keys at this path"

            explanation = (
                f"but {missing_part!r} is not in {path_that_exists!r}, "
                f"{keys_at_path_that_exists_string}."
            )
        else:
            explanation = "but the response is empty."

        super().__init__(
            "This response does not report how many records matched the search. "
            f"We expected to read the count from {expected_at!r}, "
            f"{explanation}"
        )


class NoFacetValuesReturnedError(ValueError):
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


class UncompilableFacetPatternError(ValueError):
    """
    Raised when an API describes a facet with a pattern we cannot compile
    """

    def __init__(self, facet: str, pattern: str) -> None:
        """
        Initialise the error

        Parameters
        ----------
        facet
            The facet the pattern was given for

        pattern
            The pattern we could not compile
        """
        self.facet = facet
        self.pattern = pattern
        super().__init__(
            f"The pattern given for {facet!r} is not a valid regular expression, "
            f"so we cannot check values against it: {pattern!r}"
        )


class SearchAPI(Protocol):
    """
    A search API endpoint we can query

    These interfaces are low-level.
    They mirror the ESGF search APIs directly.
    It is extremely easy to make invalid queries using interfaces of this type.
    If you want to make queries, we recommend using instances of
    [esmporium.search.search_api_facade.SearchAPIFacade][] instead
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
            that comes back in the response itself and is what
            [get_search_result_n_matches][(c).get_search_result_n_matches] reads.

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
        NoSearchResultNumberOfMatchesReturnedError
            `raw` does not report the number of records that matched the search
        """
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
            [build_get_facet_values_for_project_request][(c).build_get_facet_values_for_project_request].

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
        NoFacetValuesReturnedError
            The response does not describe facets at all.
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
            [build_get_facet_values_for_project_request][(c).build_get_facet_values_for_project_request].

        facets
            The facets we asked about.

        Returns
        -------
        :
            The pattern which is supported, keyed by the facet name.

            A facet this response does not describe with a pattern is left out
            (higher level functions are left to decide what to do
            about facets which are requested but not returned by this parsing).

        Raises
        ------
        NoFacetValuesReturnedError
            The response does not describe facets at all.

        UncompilableFacetPatternError
            A pattern specified for a given facet name
            is not able to be compiled as a regular expression.
        """
        ...
