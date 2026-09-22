"""
The failures a search can raise or record

These fall into two families. [SearchAPIRequestError][(m).] is the low-level failure
of one request to one API. The [CouldNotSearchError][(m).] family (and
[NoFacadeAnsweredError][(m).]) are the higher-level "this facade, or every facade, gave
us nothing we could use" failures that a search records or raises.
[PaginationLimitError][(m).] and [PaginationWarning][(m).] guard paging through a large
result set.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import TYPE_CHECKING

from esmporium.formatting import readable_list
from esmporium.query import QueryProtocol, facet_spec
from esmporium.search.search.keys import FacadeKey, get_facade_key

if TYPE_CHECKING:
    from esmporium.search.check_query_values import CouldNotGetAllowedValuesError
    from esmporium.search.search_api_facade import SearchAPIFacade


class SearchAPIRequestError(RuntimeError):
    """
    Raised when one request to one search API does not come back with a usable answer

    This is the low-level failure that [fire][esmporium.search.search.fire] raises,
    whether the host never answered (a transport error or timeout)
    or answered with something we could not use (a bad status, unreadable JSON).

    Higher-level callers are expected to translate this into their own terms.
    """

    def __init__(self, host: str) -> None:
        """
        Initialise the error

        Parameters
        ----------
        host
            The host the request went to
        """
        self.host = host
        super().__init__(
            f"{host} did not come back with a usable answer to our request."
        )


class CouldNotSearchError(RuntimeError):
    """
    Raised when one API gives us no results for a search

    This is deliberately vague.
    Generally, a subclass of this error provides more specific detail e.g.
    [CouldNotGetSearchResponseError][(m).] means the API did not answer
    and [CouldNotUseSearchResultsError][(m).] means it answered
    with something we could not read.
    """

    def __init__(
        self, message: str, facade_key: FacadeKey, cause: Exception | None
    ) -> None:
        """
        Initialise the error

        Parameters
        ----------
        message
            The message which says why we have no results

        facade_key
            The facade which gave us no results

        cause
            What went wrong, if we know it, kept on `cause` for callers to inspect
        """
        super().__init__(message)
        self.facade_key = facade_key
        self.cause = cause


class CouldNotGetSearchResponseError(CouldNotSearchError):
    """
    Raised when an API does not answer a search
    """

    def __init__(self, facade_key: FacadeKey, cause: Exception | None = None) -> None:
        """
        Initialise the error

        Parameters
        ----------
        facade_key
            The facade which did not answer

        cause
            What went wrong, if we know it

            Folded into the message so that the failure can say *why*,
            and kept on `cause` for callers to inspect.
        """
        why = "" if cause is None else f" ({cause})"
        super().__init__(
            f"{facade_key[0]} did not answer our search request{why}, "
            "so it has given us no results.",
            facade_key=facade_key,
            cause=cause,
        )


class CouldNotUseSearchResultsError(CouldNotSearchError):
    """
    Raised when an API answers a search with something we cannot read

    Unlike [CouldNotGetSearchResponseError][(m).], the host did answer:
    what failed was our reading of it, so the cause is an
    [UnreadableResponseError][esmporium.search.apis.UnreadableResponseError]
    saying which key we could not find rather than a transport failure.
    """

    def __init__(
        self,
        facade_key: FacadeKey,
        cause: Exception,
        url: str | None = None,
    ) -> None:
        """
        Initialise the error

        Parameters
        ----------
        facade_key
            The facade whose answer we could not read

        cause
            What we could not read, kept on `cause` for callers to inspect

        url
            The URL we asked, if we know it

            Folded into the message so a report of this carries
            what someone would need to ask the same question again.
        """
        super().__init__(
            f"{facade_key} answered our search request "
            "with something we could not read "
            f"({cause}), so it has given us no results."
            f"{f' We asked: {url}.' if url else ''}",
            facade_key=facade_key,
            cause=cause,
        )
        self.url = url


class NoFacadeAnsweredError(RuntimeError):
    """
    Raised when every facade we asked failed to give us anything we could use

    "Failed" covers a facade not being able to handle the query,
    an API not answering and an API answering with something we could not read:
    `failures` says which it was for each API.

    Should carry all of the failures rather than only the last,
    because which APIs failed, and how, is the interesting part:
    one node being down says nothing, all of them being down says a lot,
    and only the whole list tells you which it was.
    """

    def __init__(
        self,
        failures: dict[FacadeKey, CouldNotSearchError]
        | dict[FacadeKey, CouldNotGetAllowedValuesError],
    ) -> None:
        """
        Initialise the error

        Parameters
        ----------
        failures
            Why each facade gave us nothing we could use, keyed by the facade key
        """
        self.failures = failures
        asked = "\n".join(
            f"  - {facade_key}: {failure}" for facade_key, failure in failures.items()
        )
        noun = "facade" if len(failures) == 1 else "facades"
        super().__init__(
            f"Asked {len(failures)} {noun} and none of them gave us anything "
            f"we could use:\n{asked}"
        )


class AllSubQueriesFailedError(RuntimeError):
    """
    Raised when every sub-query of a multi-query search or value check failed
    """

    def __init__(self, failures: tuple[NoFacadeAnsweredError, ...]) -> None:
        """
        Initialise the error

        Parameters
        ----------
        failures
            Each failed sub-query's error, in the order the sub-queries ran
        """
        self.failures = failures
        joined = "\n".join(f"  - {failure}" for failure in failures)
        super().__init__(
            f"All {len(failures)} sub-queries failed to return anything we could use:\n"
            f"{joined}"
        )


class ClashingFacetsForFacadeError(CouldNotSearchError):
    """
    Raised when `other_terms` sets values for facets that a query already sets

    Unlike
    [ClashingFacetsError][esmporium.search.search_api_facade.ClashingFacetsError],
    this also gives context about the original query
    and facade being used
    (because the facade decides
    how the original query's facet names are translated to API names).

    other_terms is the escape hatch for facets we do not model,
    so a name in other_terms which lands on a facet the query already sets is ambiguous:
    there is no way to tell which value should win, so we refuse to guess.

    This sub-classes [CouldNotSearchError][(m).]
    because clashing facets prevent us from constructing a request
    and therefore from searching,
    but a query which clashes for one facade can be perfectly askable for another
    so we want this to fall under the same 'banner'.
    """

    def __init__(
        self,
        query: QueryProtocol,
        facade: SearchAPIFacade,
        clashing: Collection[str],
    ) -> None:
        """
        Initialise the error

        Parameters
        ----------
        query
            The original query

        facade
            The facade being used

        clashing
            The facet names that clash,
            once `query` has been translated into the API names required by `facade`.
        """
        self.query = query
        self.facade = facade
        self.clashing = tuple(sorted(clashing))

        clashing_api_names = readable_list(self.clashing)
        noun = "facet" if len(self.clashing) == 1 else "facets"
        conjugation = "clashes" if len(self.clashing) == 1 else "clash"

        # Figure out the canonical name the clashes map to, if any
        spec_facade_parameters = facet_spec(facade.parameters.base_query_style)
        api_name_to_canonical_map = {
            v: k
            for k, v in facade.parameters.get_mapping_to_api_facet_names(
                {
                    *spec_facade_parameters.facet_names,
                    *spec_facade_parameters.canonical_to_native,
                }
            ).items()
        }

        clash_to_canonical_map = {
            clash: api_name_to_canonical_map[clash]
            for clash in self.clashing
            if clash in api_name_to_canonical_map
        }

        # Figure out the query name each clash maps to
        spec_query = facet_spec(type(query))
        clash_to_query_map_canonical = {
            clash: spec_query.canonical_to_native[maybe_canonical]
            for clash, maybe_canonical in clash_to_canonical_map.items()
            if maybe_canonical not in spec_query.query_specific_facets
        }
        clash_to_query_map_query_specific = {
            clash: maybe_query_specific
            for clash, maybe_query_specific in clash_to_canonical_map.items()
            if maybe_query_specific in spec_query.query_specific_facets
            # Don't need to check spec_facade_parameters,
            # the facet would have caused an error already there
            # if it wasn't supported by spec_facade_parameters.
        }

        # The two are keyed by different kinds of facet, so neither can shadow
        # the other. Anything the query does not set (e.g. a facet the facade
        # sets itself) is already gone: it never made it into the maps.
        clash_to_query_map = {
            **clash_to_query_map_canonical,
            **clash_to_query_map_query_specific,
        }

        # A clash we could not map back is a facet the facade puts in the request
        # itself (STAC's `collection`, say, which it works out from the project)
        # rather than one the query names, so there is no query name to offer
        # as the alternative to setting it in `other_terms`.
        mapped = tuple(clash for clash in self.clashing if clash in clash_to_query_map)
        unmapped = tuple(
            clash for clash in self.clashing if clash not in clash_to_query_map
        )

        # Create our relevant map from query terms to API terms,
        # in the order the clashes are reported in
        query_to_clash_map_relevant = {
            clash_to_query_map[clash]: clash for clash in mapped
        }
        clashing_query_names = readable_list(tuple(query_to_clash_map_relevant))

        # Store key results
        self.query_to_api_map_relevant = query_to_clash_map_relevant

        advice = ""
        if mapped:
            advice += (
                f"The relevant mapping from query names to API names is: "
                f"{query_to_clash_map_relevant}. "
                f"Either set {clashing_query_names} via the query "
                f"or set {readable_list(mapped)} via `other_terms`, don't do both. "
            )

        if unmapped:
            verb = "is" if len(unmapped) == 1 else "are"
            pronoun = "it" if len(unmapped) == 1 else "they"
            advice += (
                f"{readable_list(unmapped)} {verb} set by the facade itself "
                "rather than by a facet of the query, "
                f"so {pronoun} cannot be set via `other_terms`. "
            )

        msg = (
            f"`other_terms` {noun} {clashing_api_names} {conjugation} "
            "with the query's facet names, "
            "once the query's facet names are translated to the API's names. "
            f"{advice}"
            f"{query=}"
        )
        super().__init__(msg, facade_key=get_facade_key(facade), cause=None)


class PaginationLimitError(RuntimeError):
    """
    Raised when paging through a search hits a safety limit before it is exhausted

    This is a guardrail, not a normal outcome. It fires either because a search
    collected more records than the caller allowed (`max_results`), or because an
    endpoint asked us to re-request a page we had already requested, which would
    otherwise loop forever.
    """

    def __init__(self, facade_key: FacadeKey, message: str) -> None:
        """
        Initialise the error

        Parameters
        ----------
        facade_key
            The facade we were paging through

        message
            Why we stopped, phrased to follow on from "while paging results from
            <facade>: "
        """
        self.facade_key = facade_key
        super().__init__(f"While paging results from {facade_key}: {message}")


# Note for developers: A [UserWarning][] so it shows by default,
# and its own category so it is easy to silence on its own: filter it with
# `warnings.simplefilter("ignore", PaginationWarning)`,
# or escalate it to an error in tests, without touching any other warning.
class PaginationWarning(UserWarning):
    """
    Warns that a search is large enough that it will page through several requests

    Emitted (unless `warn_on_pagination` is turned off) when an endpoint reports more
    matches than fit in one page, so the caller knows the search will make several
    requests and may take a while before it is done.
    """
