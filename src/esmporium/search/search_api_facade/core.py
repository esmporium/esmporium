"""
Definition of our core [SearchAPIFacade][(m).] class
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from esmporium.query import (
    FacetNotExpressibleError,
    QueryCanonical,
    QueryProtocol,
    facet_spec,
)
from esmporium.search.apis import Request, SearchAPI
from esmporium.search.search_api_facade.parameters import (
    FacadeParametersProtocol,
    OneProjectRequiredError,
    get_mapping_to_query_style_facet_names,
)


class UnaskableFacetError(AssertionError):
    """
    Raised when we ask for a facet we could never have asked an API about

    This error means a facets request was built
    and sent naming a facet the vocabulary has no name for,
    which [check_facets_expressible][(m).check_facets_expressible]
    exists to prevent.
    Raising this means something got past the checks in
    [check_facets_expressible][(m).check_facets_expressible]
    or a user bypassed
    [check_facets_expressible][(m).check_facets_expressible]
    in the first place.
    """

    def __init__(self, params: type[QueryProtocol], facets: set[str]) -> None:
        """
        Initialise the error

        Parameters
        ----------
        params
            The vocabulary the response is written in

        facets
            The facets which could not have been asked about,
            named as [get_mapping_to_query_style_facet_names][(m).] describes
        """
        self.params = params
        self.facets = facets
        named = ", ".join(sorted(facets))
        super().__init__(
            f"{params.__name__} has no name for {named}, "
            "so we cannot have asked the API about it and it cannot be in this "
            "response. This request should never have been built."
        )


def get_unexpressible_facets(
    query_style: type[QueryProtocol], facets: set[str]
) -> set[str]:
    """
    Work out which of the given facets a query style has no name for

    The name is slightly misleading.
    We consider a facet unexpressible if it is not in the result of
    [get_mapping_to_query_style_facet_names][(m).]
    (see [get_mapping_to_query_style_facet_names][(m).]'s docstring
    for which names it can map).

    Parameters
    ----------
    query_style
        The query style to check against

    facets
        The facets to check

    Returns
    -------
    :
        The facets which `query_style` cannot express
        (according to [get_mapping_to_query_style_facet_names][(m).])
    """
    return set(facets) - set(
        get_mapping_to_query_style_facet_names(query_style, facets)
    )


def check_facets_expressible(
    query_style: type[QueryProtocol], facets: set[str]
) -> None:
    """
    Check that a query style can express every facet being asked about

    The name is slightly misleading.
    We consider a facet expressible if it is not in the result of
    [get_unexpressible_facets][(m).]
    (see [get_unexpressible_facets][(m).]'s docstring
    for why the name is slightly misleading).

    Parameters
    ----------
    query_style
        The query style to check against

    facets
        The facets to check

    Raises
    ------
    FacetNotExpressibleError
        `query_style` cannot express one or more of `facets`
    """
    unexpressible = get_unexpressible_facets(query_style, facets)
    if unexpressible:
        raise FacetNotExpressibleError(unexpressible, facet_spec(query_style).name)


def check_facets_askable(query_style: type[QueryProtocol], facets: set[str]) -> None:
    """
    Check that every facet being read is one we could have asked about

    Unlike [check_facets_expressible][(m).],
    which guards the request we are about to build,
    this guards a response we have already been given.
    Getting here with a facet this vocabulary cannot express
    means a request was built and sent that never should have been,
    so the fault is ours rather than the caller's.

    The same caveats about which values of `facets` are supported
    that apply to [check_facets_expressible][(m).] also apply here.


    Parameters
    ----------
    query_style
        The query style the response is written in

    facets
        The facets to check

    Raises
    ------
    UnaskableFacetError
        `query_style` cannot express one of `facets`
    """
    unexpressible = get_unexpressible_facets(query_style, facets)
    if unexpressible:
        raise UnaskableFacetError(query_style, unexpressible)


def get_single_project(canonical: QueryCanonical) -> str:
    """
    Get a single project from a query

    Parameters
    ----------
    canonical
        Query from which to get the project

    Returns
    -------
    :
        Single project specified by `canonical`

    Raises
    ------
    OneProjectRequiredError
        `canonical` does not name exactly one project
    """
    if len(canonical.project) != 1:
        raise OneProjectRequiredError(canonical, canonical.project)

    return canonical.project[0]


@dataclass(frozen=True)
class SearchAPIFacade:
    """
    A search API facade

    This turns the search API from something which will accept queries for any project,
    into something which will only accept queries for a single project.
    This makes query creation, result parsing and error handling much more robust,
    at the price of having to make multiple queries
    if we want to search more than one project
    (in practice this is a tiny price to pay,
    so we deliberately make this tradeoff throughout the package).

    The facade owns the vocabulary translation.
    [build_search_request][(c).build_search_request] and
    [build_get_facet_values_request][(c).build_get_facet_values_request]
    turn a canonical query into a request in `search_api`'s wire format.
    [parse_facet_values][(c).parse_facet_values] and
    [parse_facet_patterns][(c).parse_facet_patterns]
    read the raw answers from the search APIs
    back into our canonical vocabulary.
    """

    parameters: FacadeParametersProtocol
    """
    The parameters that this facade uses
    """

    search_api: SearchAPI
    """
    The search API for which we are providing a facade
    """

    def askable_facets(self, facets: set[str]) -> set[str]:
        """
        Get the subset of `facets` this facade can handle

        The rest cannot be asked about (there is no name to ask them under).

        Parameters
        ----------
        facets
            The facets to filter, named canonically

        Returns
        -------
        :
            The facets `parameters` can express, named canonically
        """
        return set(self.parameters.get_mapping_to_api_facet_names(facets))

    def build_search_request(self, canonical: QueryCanonical, limit: int) -> Request:
        """
        Build a search request for a canonical query

        Parameters
        ----------
        canonical
            Query to render

        limit
            The page size to ask for,
            i.e. the maximum number of records in one response.

        Returns
        -------
        :
            The request to send to `search_api`

        Raises
        ------
        FacetNotExpressibleError
            `canonical` sets a facet this facade's vocabulary cannot express

        LimitOutOfRangeError
            `limit` is outside the range `search_api` accepts

        OneProjectRequiredError
            A STAC facade was given anything other than exactly one project

        ProjectPrefixMismatchError
            A STAC facade was given a project its vocabulary does not describe
        """
        facet_values = self.parameters.get_search_request_facet_values(canonical)
        return self.search_api.build_search_request(facet_values, limit)

    def build_get_facet_values_request(
        self, canonical: QueryCanonical, facets: set[str]
    ) -> Request:
        """
        Build a request which lists the values of the given facets

        Parameters
        ----------
        canonical
            The query whose project to scope to

        facets
            The facets to list the values of, named canonically.

            Every one has to be a facet this vocabulary can express,
            because there is no way to ask about one that is not.

        Returns
        -------
        :
            The request to send to `search_api`

        Raises
        ------
        FacetNotExpressibleError
            This facade's vocabulary cannot express one of `facets`

        OneProjectRequiredError
            A STAC facade was given anything other than exactly one project

        ProjectPrefixMismatchError
            A STAC facade was given a project its vocabulary does not describe
        """
        check_facets_expressible(self.parameters.base_query_style, facets)

        wire_facet_names = set(
            self.parameters.get_mapping_to_api_facet_names(facets).values()
        )
        project = get_single_project(canonical)

        return self.search_api.build_get_facet_values_for_project_request(
            wire_facet_names, project
        )

    def parse_facet_values(
        self, raw: dict[str, Any], facets: set[str]
    ) -> dict[str, set[str]]:
        """
        Read the available facet values out of a raw response

        Parameters
        ----------
        raw
            The response to read, i.e. the answer to a request built with
            [build_get_facet_values_request][(c).build_get_facet_values_request]

        facets
            The facets we asked about, named canonically

        Returns
        -------
        :
            The values which are available, keyed by the canonical facet name

            A facet whose values the API does not enumerate is left out.

        Raises
        ------
        NoFacetValuesReturned
            The response does not enumerate facet values at all

        UnaskableFacetError
            This facade's vocabulary cannot express one of `facets`,
            so this response was never going to answer the question
        """
        return self._read_back(self.search_api.parse_facet_values, raw, facets)

    def parse_facet_patterns(
        self, raw: dict[str, Any], facets: set[str]
    ) -> dict[str, re.Pattern[str]]:
        """
        Read the supported facet patterns out of a raw response

        The counterpart to [parse_facet_values][(c).parse_facet_values],
        for the facets an API describes by their form rather than by listing them.

        Parameters
        ----------
        raw
            The response to read, i.e. the answer to a request built with
            [build_get_facet_values_request][(c).build_get_facet_values_request]

        facets
            The facets we asked about, named canonically

        Returns
        -------
        :
            The pattern each facet's values must take, keyed by the canonical name

        Raises
        ------
        UncompilableFacetPatternError
            A pattern given for a facet is not a valid regular expression

        UnaskableFacetError
            This facade's vocabulary cannot express one of `facets`
        """
        return self._read_back(self.search_api.parse_facet_patterns, raw, facets)

    def _read_back(
        self,
        parse: Callable[[dict[str, Any], set[str]], dict[str, Any]],
        raw: dict[str, Any],
        facets: set[str],
    ) -> dict[str, Any]:
        """
        Parse a facet-values response and translate its keys back to canonical names

        `parse` reads `raw` keyed by the API names;
        this asks it about the API names for `facets`,
        then hands the answer back under the names they were asked for.
        """
        check_facets_askable(self.parameters.base_query_style, facets)

        api_name_map = self.parameters.get_mapping_to_api_facet_names(facets)
        asked_for_map = {
            api_name: canonical for canonical, api_name in api_name_map.items()
        }

        res_api_keyed = parse(raw, set(api_name_map.values()))

        res = {
            asked_for_map[api_name]: value
            for api_name, value in res_api_keyed.items()
            if api_name in asked_for_map
        }

        return res
