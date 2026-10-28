"""
Definition of our core [SearchAPIFacade][(m).] class
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from esmporium.query import (
    FacetNotExpressibleError,
    QueryCanonical,
    QueryProtocol,
    facet_spec,
)
from esmporium.search.apis import Request, SearchAPI, SolrSearchAPI
from esmporium.search.apis.esgf1 import solr_parsed_file_documents
from esmporium.search.result_normalisation import SOLR_FORMAT_TAG
from esmporium.search.result_parsing import (
    DatasetFacets,
    ParsedDocument,
    ParsedFileDocument,
)
from esmporium.search.search_api_facade.parameters import (
    FacadeParametersProtocol,
    OneProjectRequiredError,
    get_mapping_to_query_style_facet_names,
)
from esmporium.search.search_api_facade.result_parsers import ResultParserProtocol


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


class NotASolrFacadeError(ValueError):
    """Raised when a facade with no `type=File` request to make is asked for one"""

    def __init__(self, facade: SearchAPIFacade) -> None:
        self.facade = facade
        """The facade we refused"""

        super().__init__(
            f"{facade.search_api.host} answers in the "
            f"{facade.search_api.raw_docs_format_tag!r} format, not "
            f"{SOLR_FORMAT_TAG!r}, so it has no `type=File` request to make. "
            "ESGF-NG carries per-file information in the search response itself, so "
            "its routes are already recorded by the time this could be called. "
            "This is a mistake rather than a no-op, so it is loud: a caller which "
            "thinks it is filling in file access here is not."
        )


class UnaskableFacetError(AssertionError):
    """
    Raised when we ask for a facet we could never have asked an API about

    This error means a facets request was built
    and sent naming a facet the query style has no parameter name for,
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
            The query style the response is written in

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


def check_facets_askable(query_style: type[QueryProtocol], facets: set[str]) -> None:
    """
    Check that every facet being read is one we could have asked about

    Unlike [check_facets_expressible][(m).],
    which guards the request we are about to build,
    this guards a response we have already been given.
    Getting here with a facet this query style cannot express
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
    so we deliberately make this tradeoff throughout esmporium).

    The facade owns the name translation.
    [build_search_request][(c).build_search_request] and
    [build_get_facet_values_request][(c).build_get_facet_values_request]
    turn a canonical query into a request using `search_api`'s API parameter names.
    [parse_facet_values][(c).parse_facet_values] and
    [parse_facet_patterns][(c).parse_facet_patterns]
    read the raw answers from the search APIs
    back into canonical names.
    """

    parameters: FacadeParametersProtocol
    """
    The parameters that this facade uses
    """

    search_api: SearchAPI
    """
    The search API for which we are providing a facade
    """

    result_parser: ResultParserProtocol
    """
    The parser that reads this facade's search results

    What a result looks like can be a function of its project
    as well as of the format it arrived in
    (e.g. a CMIP5 Solr record bundles many variables into one document,
    where a CMIP6 Solr one is a single row),
    which is why the parser is chosen for this pairing
    rather than derived from either half alone;
    see [esmporium.search.search_api_facade.result_parsers][].

    Anything which depends only on the format,
    e.g. the number of records that matched,
    belongs on [search_api][(c).search_api] instead.
    """

    def askable_facets(self, facets: set[str]) -> set[str]:
        """
        Get the subset of `facets` this facade can handle

        The rest cannot be asked about (there is no name to ask them under).

        Parameters
        ----------
        facets
            The facets to filter, named as they were asked for,
            i.e. canonically or, for a facet with no canonical equivalent,
            by the name the query style gives it

        Returns
        -------
        :
            The subset of `facets` which `parameters` can express,
            named the same way they were passed in
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
            `canonical` sets a facet this facade's query style cannot express

        LimitOutOfRangeError
            `limit` is outside the range `search_api` accepts

        OneProjectRequiredError
            `self.parameters` needs exactly one project
            and `canonical` specifies zero or more than one
            (e.g. our STAC facades need this;
            see [get_collection][esmporium.search.STACFacadeParameters.get_collection])

        ProjectPrefixMismatchError
            The facade parameters were given a project
            whose prefix disagrees with the one they write
            (again, our STAC facades are the ones which check this)
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
            The facets to list the values of, named as they were asked for,
            i.e. canonically or, for a facet with no canonical equivalent,
            by the name the query style gives it.

            Every one has to be a facet this query style can express,
            because there is no way to ask about one that is not.

        Returns
        -------
        :
            The request to send to `search_api`

        Raises
        ------
        FacetNotExpressibleError
            This facade's query style cannot express one of `facets`

        OneProjectRequiredError
            `canonical` specifies zero or more than one project

            Getting facet values for one project at a time
            makes error handling much easier.
            If you want to get facet values for multiple projects at a time,
            use the low-level functions directly.
        """
        check_facets_expressible(self.parameters.base_query_style, facets)
        project = get_single_project(canonical)

        api_facet_names = self.parameters.get_facet_values_request_facet_names(
            canonical, facets
        )

        return self.search_api.build_get_facet_values_for_project_request(
            api_facet_names, project
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
            The facets we asked about, named as they were asked for,
            i.e. canonically or, for a facet with no canonical equivalent,
            by the name the query style gives it

        Returns
        -------
        :
            The values which are available,
            keyed by the same names `facets` used

            A facet whose values the API does not enumerate is left out.

        Raises
        ------
        NoFacetValuesReturnedError
            The response does not enumerate facet values at all

        UnaskableFacetError
            This facade's query style cannot express one of `facets`,
            so this response was never going to answer the question
        """
        return self.read_back(self.search_api.parse_facet_values, raw, facets)

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
            The facets we asked about, named as they were asked for,
            i.e. canonically or, for a facet with no canonical equivalent,
            by the name the query style gives it

        Returns
        -------
        :
            The pattern each facet's values must take,
            keyed by the same names `facets` used

            A facet the API does not describe with a pattern is left out.

        Raises
        ------
        NoFacetValuesReturnedError
            The response does not describe facets at all

        UncompilableFacetPatternError
            A pattern given for a facet is not a valid regular expression

        UnaskableFacetError
            This facade's query style cannot express one of `facets`
        """
        return self.read_back(self.search_api.parse_facet_patterns, raw, facets)

    def parse_search_results(self, raw: dict[str, Any]) -> tuple[ParsedDocument, ...]:
        """
        Parse a raw search response into the documents it carries

        The facade holds the pieces this needs, so no collaborator has to guess
        another's concern: the [result_parser][(c).result_parser] knows how this
        project's results are written by this endpoint, the
        [search_api][(c).search_api] knows the response format, and the
        [parameters][(c).parameters] know which API field carries each facet.

        Parameters
        ----------
        raw
            The response to read

            Usually the answer to a request built with
            [build_search_request][(c).build_search_request].

        Returns
        -------
        :
            Parsed documents
        """
        return self.result_parser.parse_search_results(
            raw, api=self.search_api, facade_parameters=self.parameters
        )

    @property
    def file_attribution_facets(self) -> tuple[str, ...]:
        """
        The facets a file-level request from this facade must narrow by

        See
        [ResultParserProtocol.file_attribution_facets][esmporium.search.ResultParserProtocol.file_attribution_facets],
        which this reads: whether one document is one dataset row is the
        [result_parser][(c).result_parser]'s knowledge, so it is the parser which
        states this, and the facade which hands it on.

        Returns
        -------
        :
            The facets to narrow by, named canonically, possibly none
        """
        return self.result_parser.file_attribution_facets

    def file_search_api(self) -> SolrSearchAPI:
        """
        Check this facade can be asked for file documents, and give back its API as that shape

        The check and the narrowing are one method so that the narrowed type cannot be
        had without the check.

        Returns
        -------
        :
            This facade's search API, as the shape which carries
            `build_file_search_request`

        Raises
        ------
        NotASolrFacadeError
            It does not answer in the Solr format, so it has no `type=File` request
            to make
        """  # noqa: E501
        if self.search_api.raw_docs_format_tag != SOLR_FORMAT_TAG:
            raise NotASolrFacadeError(self)

        # Established by the check above, which the type system cannot see.
        return cast("SolrSearchAPI", self.search_api)

    def build_file_search_request(
        self,
        dataset_ids: Sequence[str],
        limit: int,
        facet_values: Mapping[str, Sequence[str]] | None = None,
    ) -> Request:
        """
        Build a request for the file documents of some datasets

        The facade owns this for the same reason it owns
        [build_search_request][(c).build_search_request]: the facets come in named
        canonically and go out named the way this project is spelled on this API
        (`variable` for CMIP5, `variable_id` for CMIP6 and CMIP7), so no caller has
        to know either name.

        Parameters
        ----------
        dataset_ids
            The node-specific dataset ids to ask about, i.e.
            `<instance_id>|<data_node>`

        limit
            The page size to ask for

        facet_values
            Further facets to narrow by, named canonically

            Usually the values of
            [file_attribution_facets][(c).file_attribution_facets].

        Returns
        -------
        :
            The request to send to `search_api`

        Raises
        ------
        LimitOutOfRangeError
            `limit` is outside the range `search_api` accepts

        NotASolrFacadeError
            This facade has no `type=File` request to make

        FacetNotExpressibleError
            This facade cannot express one of `facet_values`' facets, so the request
            could not narrow by it
        """
        api = self.file_search_api()
        asked = dict(facet_values or {})
        field_of = self.parameters.get_mapping_to_api_facet_names(set(asked))
        unexpressible = set(asked) - set(field_of)
        if unexpressible:
            # The same failure build_search_request reports, raised the same way:
            # narrowing we were asked for and cannot do is not narrowing we may skip,
            # because the answer would then be about more datasets than the caller
            # can place.
            raise FacetNotExpressibleError(
                unexpressible, self.parameters.base_query_style.__name__
            )

        return api.build_file_search_request(
            list(dataset_ids),
            limit,
            {field_of[facet]: tuple(values) for facet, values in asked.items()},
        )

    def parse_file_search_results(
        self, raw: dict[str, Any]
    ) -> tuple[ParsedFileDocument, ...]:
        """
        Parse a raw file-document response into the documents it carries

        The file-level counterpart of [parse_search_results][(c).parse_search_results].
        It does not go through [result_parser][(c).result_parser]: a File document is
        a File document whatever project it describes, so there is nothing
        project-specific left to do by the time the answer is in hand. What was
        project-specific happened on the way out, in
        [build_file_search_request][(c).build_file_search_request].

        Parameters
        ----------
        raw
            The response to read

            Usually the answer to a request built with
            [build_file_search_request][(c).build_file_search_request].

        Returns
        -------
        :
            Parsed file documents

        Raises
        ------
        NotASolrFacadeError
            This facade has no `type=File` request to make, so it cannot have an
            answer to one either
        """
        api = self.file_search_api()
        return solr_parsed_file_documents(raw, api)

    def read_dataset_rows(self, doc: dict[str, Any]) -> tuple[DatasetFacets, ...]:
        """
        Read the complete dataset rows one search document maps to

        Parameters
        ----------
        doc
            One document

            Usually extracted with
            [search_api.extract_result_documents][esmporium.search.apis.SearchAPI.extract_result_documents].

        Returns
        -------
        :
            Parsed dataset facets
        """
        return self.result_parser.get_dataset_rows(
            doc, api=self.search_api, facade_parameters=self.parameters
        )

    def read_back(
        self,
        parse: Callable[[dict[str, Any], set[str]], dict[str, Any]],
        raw: dict[str, Any],
        facets: set[str],
    ) -> dict[str, Any]:
        """
        Parse a facet response, translating its keys from API names back to the names given in `facets`

        `parse` reads `raw` and gives answers keyed by the API parameter names.
        This method translates those keys back to the names given in `facets`,
        so the caller reads the answer under the same names it asked with.

        A facet named in `facets` but missing from `parse`'s output is dropped
        silently: the caller must handle these drops.

        Parameters
        ----------
        parse
            Function with which to read `raw`

        raw
            Raw object to read

        facets
            Facets to retrieve
            (this function is responsible for figuring out the API names that correspond to the names in `facets`).

        Returns
        -------
        :
            The parsed response, using keys defined by `facets` rather than raw API keys where possible
        """  # noqa: E501
        check_facets_askable(self.parameters.base_query_style, facets)

        api_name_lookup = self.parameters.get_mapping_to_api_facet_names(facets)
        res_api_keyed = parse(raw, set(api_name_lookup.values()))

        asked_for_name_lookup = {
            api_name: asked_for for asked_for, api_name in api_name_lookup.items()
        }

        res = {
            asked_for_name_lookup[api_name]: value
            for api_name, value in res_api_keyed.items()
            # Guard against facets which cannot be parsed out of the response.
            # This silently drops - the caller has to handle these drops.
            if api_name in asked_for_name_lookup
        }

        return res
