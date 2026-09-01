"""
Search API facade

This contains our facades to search APIs.
These facades are introduced to add more robust
query creation, result parsing and error handling.
Complete documentation of this will be added in future.

A facade pairs a *parameter definition*
(the vocabulary a project is written in for a family of APIs,
e.g. [ESGF1_CMIP6_FACADE_PARAMETERS][(m).ESGF1_CMIP6_FACADE_PARAMETERS])
with a *search API*
(the format spoken by a family of endpoints,
e.g. [SearchAPIESGF1Solr][esmporium.search.apis.SearchAPIESGF1Solr]).
The parameter definition is the facade's concern:
it is the facade which turns a canonical query into the names
and shapes a search API speaks,
and which turns the answer back into the canonical vocabulary.
The search API (not the facade layer) knows nothing about canonical queries;
it only knows how to encode a request and decode a response for its own format.
Keeping the two layers visibly distinct is deliberate.
for example, every facade user reaches through to `facade.search_api.host` explicitly,
rather than the facade re-exposing it,
so it is always clear where things are coming from.
"""
# TODO: devs - add more complete docs in a follow up PR

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from tenacity import Retrying

from esmporium.query import (
    FacetNotExpressibleError,
    QueryCanonical,
    QueryProtocol,
    facet_spec,
)
from esmporium.search.apis import (
    Request,
    SearchAPI,
    SearchAPIESGF1Solr,
    SearchAPIESGF15BridgeSolr,
    SearchAPIESGFNGSTAC,
)
from esmporium.search.retry import build_transient_retrying
from esmporium.search.search_api_facade.parameters import (
    ESGF1_CMIP5_FACADE_PARAMETERS,
    ESGF1_CMIP6_FACADE_PARAMETERS,
    ESGF1_CMIP7_FACADE_PARAMETERS,
    ESGFNG_CMIP5_FACADE_PARAMETERS,
    ESGFNG_CMIP6_FACADE_PARAMETERS,
    ESGFNG_CMIP7_FACADE_PARAMETERS,
    ESGF1CMIP5ParametersQueryStyle,
    ESGF1CMIP6ParametersQueryStyle,
    ESGF1CMIP7ParametersQueryStyle,
    ESGFNGCMIP5ParametersQueryStyle,
    ESGFNGCMIP6ParametersQueryStyle,
    ESGFNGCMIP7ParametersQueryStyle,
    FacadeParametersProtocol,
    OneProjectRequiredError,
    ProjectPrefixMismatchError,
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


SearchAPIFacadeSelector = Callable[[QueryCanonical, int], SearchAPIFacade | None]
"""
Chooses which facade to try next

Given the canonical query and a 0-based attempt index,
returns the next
[SearchAPIFacade][esmporium.search.search_api_facade.SearchAPIFacade] to try,
or `None` to say that there is nothing to try for this query and attempt number.
"""


class SelectorOfferedNoAPIFacadeError(ValueError):
    """
    Raised when a selector offers no search API facade for a query from the very start

    Asking for a search is asking for it to happen.
    Handing back an empty answer would read as
    "we asked, and nobody had anything for you",
    when what happened is that nobody was asked at all:
    a selector with an empty list,
    or one whose rules rule out every endpoint for this query,
    is a bug in the calling code
    and is worth saying out loud rather than quietly returning nothing.

    This is only about having nobody to ask.
    Endpoints which were asked and did not answer are a different thing,
    and are reported as such.
    """

    def __init__(
        self, canonical: QueryCanonical, selector: SearchAPIFacadeSelector
    ) -> None:
        """
        Initialise the error

        Parameters
        ----------
        canonical
            The query we were going to ask about

        selector
            The selector which had nothing to offer for it
        """
        self.canonical = canonical
        self.selector = selector
        super().__init__(
            "No API facade was offered on the very first attempt, "
            "so there was nobody to ask. "
            f"The selector was: {selector}. The query was: {canonical!r}."
        )


def build_list_selector(facades: Sequence[SearchAPIFacade]) -> SearchAPIFacadeSelector:
    """
    Build a selector that yields search API facades in order

    Every query works through the same list.

    Parameters
    ----------
    facades
        The search API facades to yield, in order

    Returns
    -------
    :
        A selector over `facades`
    """

    def select(canonical: QueryCanonical, attempt: int) -> SearchAPIFacade | None:
        return facades[attempt] if attempt < len(facades) else None

    return select


def build_project_list_selector(
    project_lists: Mapping[str, Sequence[SearchAPIFacade]],
) -> SearchAPIFacadeSelector:
    """
    Build a selector that works through a project specific list of facades

    Parameters
    ----------
    project_lists
        The facades to yield for each project, in order

    Returns
    -------
    :
        A selector which yields facades in an order specific to the query's project
    """

    def select(canonical: QueryCanonical, attempt: int) -> SearchAPIFacade | None:
        """
        Select search API facade to use

        Parameters
        ----------
        canonical
            Query (in canonical form)

        attempt
            Search attempt

        Returns
        -------
        :
            [SearchAPIFacade][(m).] to use

            If we have run out of APIs to try, we return `None`

        Raises
        ------
        ValueError
            The query specifies a search that is not for exactly one project

        KeyError
            We do not have a list of facades to try for the input project
        """
        if len(canonical.project) != 1:
            msg = (
                "We can only unambiguously pick the SearchAPI list "
                "if there is exactly one project, "
                f"received: {canonical.project}"
            )
            raise ValueError(msg)

        project = canonical.project[0]

        apis = project_lists[project]

        return apis[attempt] if attempt < len(apis) else None

    return select


@dataclass(frozen=True)
class SearchAPIFacadeClassification:
    """
    Classification of a search API facade

    Provides extra classification information (i.e. metadata) which
    [SearchAPIFacade][(m).] doesn't hold.

    Note that these classifications are generally based on experience.
    If we were 100% sure about this metadata,
    we would adjust the underlying classes directly instead.
    """

    facade: SearchAPIFacade
    """
    Search API facade
    """

    projects: tuple[str, ...]
    """
    Projects which `facade` supports working with
    """


@dataclass(frozen=True)
class SearchAPIFacadeStore:
    """
    A store of search API facades

    This store helps manage a set of API facades
    and get them in more convenient ways than looking through lists.
    """

    classifications: tuple[SearchAPIFacadeClassification, ...]
    """
    Search API facade classifications
    """

    def get_api_facades_for_project(self, project: str) -> list[SearchAPIFacade]:
        """
        Get the API facades that can be used to search a specific project

        Parameters
        ----------
        project
            The project for which we want to get
            all the API facades that can be used to search the project.

        Returns
        -------
        :
            API facades that can be used to search `project`.
        """
        return [v.facade for v in self.classifications if project in v.projects]

    def get_api_facades_from_host(self, host: str) -> list[SearchAPIFacade]:
        """
        Get the API facades that use a specific host

        Parameters
        ----------
        host
            The host for which we want to get API facades.

        Returns
        -------
        :
            API facades that use `host`
        """
        return [
            v.facade for v in self.classifications if v.facade.search_api.host == host
        ]

    def get_api_facade_for_project_from_host(
        self, project: str, host: str
    ) -> SearchAPIFacade:
        """
        Get the API facade that can be used to search a project from a specific host

        Parameters
        ----------
        project
            The project for which we want to get the API facade.

        host
            The host for which we want to get API facade.

        Returns
        -------
        :
            API facade for `project` that uses `host`
        """
        matches = [
            v
            for v in self.classifications
            if v.facade.search_api.host == host and project in v.projects
        ]
        if len(matches) < 1:
            host_projects: dict[str, list[str]] = {}
            for v in self.classifications:
                host_projects.setdefault(v.facade.search_api.host, []).extend(
                    v.projects
                )

            supported_hosts_and_projects = "\n".join(
                f"  - {host}: {projects}" for host, projects in host_projects.items()
            )
            msg = (
                f"No API from {host=} is associated with {project=}. "
                "Available hosts and supported projects:\n"
                f"{supported_hosts_and_projects}"
            )
            raise ValueError(msg)

        elif len(matches) > 1:
            msg = f"More than one candidate for {host=} and {project=}. {matches=}"
            raise AssertionError(msg)

        return matches[0].facade

    @classmethod
    def initialise_with_default_api_facades(
        cls, retrying: Retrying | None = None
    ) -> SearchAPIFacadeStore:
        """
        Initialise with our default API facade set

        Parameters
        ----------
        retrying
            Retrying strategy to use with all the APIs.

            If `None` (the default), a fresh
            [build_transient_retrying][esmporium.search.retry.build_transient_retrying]
            is built for each API. This matters because a `Retrying` carries
            per-run state, so sharing one across APIs is not safe once calls can
            be made in parallel; pass your own only if you know you want it shared.

        Returns
        -------
        :
            Initialised object
        """
        classifications_l = []

        # There are probably clearer ways to do this.
        # One for another day.
        cmip5_facades = (
            (
                ESGF1_CMIP5_FACADE_PARAMETERS,
                SearchAPIESGF1Solr,
                "esg-dn1.nsc.liu.se",
            ),
            (
                ESGF1_CMIP5_FACADE_PARAMETERS,
                SearchAPIESGF1Solr,
                "esgf.nci.org.au",
            ),
            (
                ESGF1_CMIP5_FACADE_PARAMETERS,
                SearchAPIESGF15BridgeSolr,
                "esgf-node.ornl.gov",
            ),
            (
                ESGF1_CMIP5_FACADE_PARAMETERS,
                SearchAPIESGF1Solr,
                "esgf.ceda.ac.uk",
            ),
            (
                ESGF1_CMIP5_FACADE_PARAMETERS,
                SearchAPIESGF1Solr,
                "esgf-data.dkrz.de",
            ),
            (
                ESGFNG_CMIP5_FACADE_PARAMETERS,
                SearchAPIESGFNGSTAC,
                "search.east.esgf.io",
            ),
            (
                ESGFNG_CMIP5_FACADE_PARAMETERS,
                SearchAPIESGFNGSTAC,
                "search.west.esgf.io",
            ),
        )

        cmip6_facades = (
            (
                ESGF1_CMIP6_FACADE_PARAMETERS,
                SearchAPIESGF1Solr,
                "esg-dn1.nsc.liu.se",
            ),
            (
                ESGF1_CMIP6_FACADE_PARAMETERS,
                SearchAPIESGF1Solr,
                "esgf.nci.org.au",
            ),
            (
                ESGF1_CMIP6_FACADE_PARAMETERS,
                SearchAPIESGF15BridgeSolr,
                "esgf-node.ornl.gov",
            ),
            (
                ESGF1_CMIP6_FACADE_PARAMETERS,
                SearchAPIESGF1Solr,
                "esgf.ceda.ac.uk",
            ),
            (
                ESGF1_CMIP6_FACADE_PARAMETERS,
                SearchAPIESGF1Solr,
                "esgf-data.dkrz.de",
            ),
            (
                ESGFNG_CMIP6_FACADE_PARAMETERS,
                SearchAPIESGFNGSTAC,
                "search.east.esgf.io",
            ),
            (
                ESGFNG_CMIP6_FACADE_PARAMETERS,
                SearchAPIESGFNGSTAC,
                "search.west.esgf.io",
            ),
        )

        cmip7_facades = (
            (
                ESGF1_CMIP7_FACADE_PARAMETERS,
                SearchAPIESGF1Solr,
                "esgf.nci.org.au",
            ),
            (
                ESGF1_CMIP7_FACADE_PARAMETERS,
                SearchAPIESGF1Solr,
                "esgf-data.dkrz.de",
            ),
            (
                ESGFNG_CMIP7_FACADE_PARAMETERS,
                SearchAPIESGFNGSTAC,
                "search.east.esgf.io",
            ),
            (
                ESGFNG_CMIP7_FACADE_PARAMETERS,
                SearchAPIESGFNGSTAC,
                "search.west.esgf.io",
            ),
        )

        # To add CMIP6Plus support in future:
        # add a `cmip6plus_facades` block here
        # (its own STAC vocabulary with a `cmip6plus` prefix would be needed,
        # as ESGFNG_CMIP6_FACADE_PARAMETERS is tied to the `cmip6` collection),
        # classify it against `("CMIP6Plus",)`
        # in the loop below,
        # and add "CMIP6Plus" to DEFAULT_SEARCH_API_FACADES_BY_PROJECT.
        for projects, facade_definitions in (
            (("CMIP5",), cmip5_facades),
            (("CMIP6",), cmip6_facades),
            (("CMIP7",), cmip7_facades),
        ):
            for facade_parameters, search_api_type, host in facade_definitions:
                # A fresh retry policy per API unless the caller shared one:
                # tenacity's Retrying carries per-run state.
                api_retrying = (
                    retrying if retrying is not None else build_transient_retrying(3)
                )
                search_api = cast(
                    "SearchAPI", search_api_type(host=host, retrying=api_retrying)
                )
                classifications_l.append(
                    SearchAPIFacadeClassification(
                        SearchAPIFacade(
                            parameters=facade_parameters,
                            search_api=search_api,
                        ),
                        projects=projects,
                    )
                )

        res = cls(classifications=tuple(classifications_l))

        return res


INBUILT_SEARCH_API_FACADE_STORE = (
    SearchAPIFacadeStore.initialise_with_default_api_facades()
)
"""
Our in-built search API facade store.

This should not be taken to be exhaustive.
You may need to add more APIs or adjust retry policies etc. yourself.
"""

DEFAULT_SEARCH_API_FACADES_BY_PROJECT: Mapping[str, Sequence[SearchAPIFacade]] = {
    project: INBUILT_SEARCH_API_FACADE_STORE.get_api_facades_for_project(project)
    for project in ("CMIP5", "CMIP6", "CMIP7")
}
"""
Default search APIs to use, grouped by project
"""

DEFAULT_SELECTOR = build_project_list_selector(DEFAULT_SEARCH_API_FACADES_BY_PROJECT)
"""The selector used when the caller does not choose one"""

__all__ = [
    "ESGF1_CMIP5_FACADE_PARAMETERS",
    "ESGF1_CMIP6_FACADE_PARAMETERS",
    "ESGF1_CMIP7_FACADE_PARAMETERS",
    "ESGFNG_CMIP5_FACADE_PARAMETERS",
    "ESGFNG_CMIP6_FACADE_PARAMETERS",
    "ESGFNG_CMIP7_FACADE_PARAMETERS",
    "ESGF1CMIP5ParametersQueryStyle",
    "ESGF1CMIP6ParametersQueryStyle",
    "ESGF1CMIP7ParametersQueryStyle",
    "ESGFNGCMIP5ParametersQueryStyle",
    "ESGFNGCMIP6ParametersQueryStyle",
    "ESGFNGCMIP7ParametersQueryStyle",
    "OneProjectRequiredError",
    "ProjectPrefixMismatchError",
    "get_mapping_to_query_style_facet_names",
]
