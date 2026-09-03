"""
Search API protocol class
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Protocol

from tenacity import Retrying

from esmporium.search.apis.request import Request


def _describe_where_we_looked(expected_at: tuple[str, ...]) -> str:
    """
    Describe where we looked for a value in a response

    Parameters
    ----------
    expected_at
        The paths in the response at which we looked,
        each one a dot-separated path

    Returns
    -------
    :
        The description of where we looked.

        This is written to follow on from "We expected to read ... from ".
    """
    if len(expected_at) == 1:
        return repr(expected_at[0])

    quoted = [repr(path) for path in expected_at]

    return f"one of {', '.join(quoted[:-1])} or {quoted[-1]}"


def _explain_why_we_could_not_read_path(raw: dict[str, Any], path: str) -> str:
    """
    Explain why we could not read a value at a single path in a response

    Parameters
    ----------
    raw
        The response we could not read the value out of

    path
        Where in `raw` we looked, as a dot-separated path

    Returns
    -------
    :
        The explanation of what we found at `path` instead.

    Raises
    ------
    AssertionError
        There is in fact a value at `path` in `raw`,
        so there is nothing to explain and we should not have been asked.
    """
    parts = path.split(".")
    current_level: Any = raw
    for level, part in enumerate(parts):
        if isinstance(current_level, Mapping) and part in current_level:
            # We have this part, keep looking
            current_level = current_level[part]
            continue

        path_that_exists = ".".join(parts[:level])
        where = repr(path_that_exists) if level else "the response's top level"
        if not isinstance(current_level, Mapping):
            found = f"we found {current_level!r} rather than keys at this path"
        elif current_level:
            keys = ", ".join(f"{key!r}" for key in sorted(current_level))
            found = f"there is only: {keys}"
        else:
            found = "there are no keys at this path"

        return f"{part!r} is not in {where}, {found}"

    if current_level:
        path_rep = "".join(f"[{part}]" for part in parts)
        msg = f"{path} is in {raw}, raw{path_rep}={current_level}"
        raise AssertionError(msg)

    return f"we found {current_level!r} at {path!r}"


def _explain_why_we_could_not_read(
    raw: dict[str, Any], expected_at: tuple[str, ...]
) -> str:
    """
    Explain why we could not read a value out of a response

    Every path we looked at is reported,
    because any of them could have been the one which answered us.

    Parameters
    ----------
    raw
        The response we could not read the value out of

    expected_at
        The paths in `raw` at which we looked,
        each one a dot-separated path

    Returns
    -------
    :
        The explanation of what we found instead.

        This is written to follow on from
        "We expected to read ... from <where we looked>, ".

    Raises
    ------
    AssertionError
        There is in fact a value at one of `expected_at` in `raw`,
        so there is nothing to explain and we should not have been asked.
    """
    if not raw:
        # No path can be explained any further than this.
        return "but the response is empty."

    explanations = [
        _explain_why_we_could_not_read_path(raw, path) for path in expected_at
    ]
    if len(explanations) == 1:
        return f"but {explanations[0]}."

    return f"but: {'; '.join(explanations)}."


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

    def __init__(self, raw: dict[str, Any], expected_at: str | tuple[str, ...]) -> None:
        """
        Initialise the error

        Parameters
        ----------
        raw
            The response we could not read a count out of

        expected_at
            Where in `raw` we looked for the number of records that matched the search

            Some APIs report the count in more than one place,
            in which case pass every path we looked at
            and the error will report on all of them.
        """
        self.raw = raw
        self.expected_at = (
            (expected_at,) if isinstance(expected_at, str) else expected_at
        )

        super().__init__(
            "This response does not report how many records matched the search. "
            "We expected to read the count from "
            f"{_describe_where_we_looked(self.expected_at)}, "
            f"{_explain_why_we_could_not_read(raw, self.expected_at)}"
        )


class NoFacetValuesReturnedError(ValueError):
    """
    Raised when a response does not enumerate facet values at all

    We don't expect this to happen,
    but if we expect to get facet values and don't, we want to be loud about it.
    """

    def __init__(self, raw: dict[str, Any], expected_at: str | tuple[str, ...]) -> None:
        """
        Initialise the error

        Parameters
        ----------
        raw
            The response we could not read facet values out of

        expected_at
            Where in `raw` we looked for the facet values

            Some APIs enumerate their facet values in more than one place,
            in which case pass every path we looked at
            and the error will report on all of them.
        """
        self.raw = raw
        self.expected_at = (
            (expected_at,) if isinstance(expected_at, str) else expected_at
        )

        super().__init__(
            "This response does not report facet values. "
            "We expected to read the facet values from "
            f"{_describe_where_we_looked(self.expected_at)}, "
            f"{_explain_why_we_could_not_read(raw, self.expected_at)}"
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
