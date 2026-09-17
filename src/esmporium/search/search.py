"""
High-level search functionality
"""

from __future__ import annotations

import json
import logging
import shlex
import time
from collections.abc import Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeAlias

import httpx

from esmporium.formatting import readable_list
from esmporium.query import (
    QueryProtocol,
    facet_spec,
    to_canonical,
)
from esmporium.search.apis import (
    NoSearchResultNumberOfMatchesReturnedError,
    Request,
    SearchAPI,
    UnreadableResponseError,
)
from esmporium.search.health import SearchAPICall, SearchAPICallObserver
from esmporium.search.result_parsing import ParsedDocument, ResultProcessor
from esmporium.search.search_api_facade import (
    DEFAULT_SELECTOR,
    ClashingFacetsError,
    NMatchesReader,
    SearchAPIFacade,
    SearchAPIFacadeSelector,
    SelectorOfferedNoAPIFacadeError,
)

if TYPE_CHECKING:
    from esmporium.search.check_query_values import CouldNotGetAllowedValuesError

logger = logging.getLogger(__name__)

FacadeKey: TypeAlias = tuple[str, str, str]
"""
A key which identifies a given facade

The first element is the host.
The second is the type of search API this facade uses/assumes.
The third is the query style used by this facade.
"""


def get_facade_key(facade: SearchAPIFacade) -> FacadeKey:
    """
    Get the key which identifies a facade

    Each element can be served by more than one facade,
    which is why we group them like this.

    Parameters
    ----------
    facade
        The facade to identify

    Returns
    -------
    :
        The facade's key
    """
    return (
        facade.search_api.host,
        type(facade.search_api).__name__,
        facade.parameters.base_query_style.__name__,
    )


def get_url(api: SearchAPI, request: Request) -> str:
    """
    Build the full URL for a request to a given API

    Parameters
    ----------
    api
        The API we want to ask

    request
        The request to make

    Returns
    -------
    :
        The URL to send `request` to
    """
    return f"{api.scheme}://{api.host}{request.path}"


def curl_equivalent(request: httpx.Request) -> str:
    """
    Render an httpx request as a `curl` command which reproduces it

    Every piece is shell-quoted, so the result is safe to paste into a terminal.

    Parameters
    ----------
    request
        The request to render

    Returns
    -------
    :
        A `curl` command equivalent to `request`
    """
    parts = ["curl", "-X", request.method]
    for name, value in request.headers.items():
        parts += ["-H", shlex.quote(f"{name}: {value}")]

    body = request.content
    if body:
        parts += ["--data", shlex.quote(body.decode("utf-8", errors="replace"))]

    parts.append(shlex.quote(str(request.url)))

    return " ".join(parts)


def log_request_as_url_and_curl(
    api: SearchAPI, request: httpx.Request, log_level: int = logging.DEBUG
) -> None:
    """
    Log a request we are about to send, at `DEBUG`

    Parameters
    ----------
    api
        The API the request is going to

    request
        The request

    log_level
        Level at which to log
    """
    if not logger.isEnabledFor(log_level):
        # Skip rendering if the logger is not enabled for the given level
        return

    url = str(request.url)
    curl = curl_equivalent(request)
    logger.log(
        log_level,
        "search request to %s\n%s %s\n%s",
        api.host,
        request.method,
        url,
        curl,
        extra={
            "search_api_host": api.host,
            "http_method": request.method,
            "http_url": url,
            "http_curl": curl,
        },
    )


class SearchAPIRequestError(RuntimeError):
    """
    Raised when one request to one search API does not come back with a usable answer

    This is the low-level failure that [fire][(m).fire] raises,
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


def _result_count_or_none(
    read_n_matches: NMatchesReader | None, raw: dict[str, Any]
) -> int | None:
    """
    Read how many records a response reported, or `None` if it reported none

    A search response reports its total; the STAC facet-values response
    (a collection document) does not, hence the tolerance.

    Parameters
    ----------
    read_n_matches
        Reads the count out of `raw`, or `None` if the caller cannot say how.

    raw
        The response to read

    Returns
    -------
    :
        The number of records reported, or `None` if the response carries no count
        (or if nothing that could read one was passed)
    """
    if read_n_matches is None:
        return None

    try:
        return read_n_matches(raw)
    except NoSearchResultNumberOfMatchesReturnedError:
        return None


def fire(
    client: httpx.Client,
    api: SearchAPI,
    request: Request,
    api_call_observer: SearchAPICallObserver | None = None,
    read_n_matches: NMatchesReader | None = None,
) -> dict[str, Any]:
    """
    Send one request to one API, using that API's retry policy and timeout

    Parameters
    ----------
    client
        The HTTP client to send with

    api
        The API to send to (this also carries the retry policy and timeout)

    request
        The request to send

    api_call_observer
        Told about this call once it is done, on both the success and failure path.
        If `None` (the default), nothing is recorded.
        See [esmporium.search.health][] for how to build one.

    read_n_matches
        Reads how many records the answer says matched, for the observer to record.
        If `None`, no count is recorded.

    Returns
    -------
    :
        The raw JSON the endpoint answered with

    Raises
    ------
    SearchAPIRequestError
        The API never answered, or answered with something we cannot use.
        The underlying cause is carried as the error's `__cause__`.
    """
    built = client.build_request(
        request.method,
        get_url(api, request),
        params=request.params,
        json=request.json_body,
        timeout=api.timeout,
    )
    log_request_as_url_and_curl(api, built)

    request_body = json.dumps(request.json_body) if request.json_body else None

    def _record(  # noqa: PLR0913 - one keyword-only argument per recorded field
        *,
        attempt_number: int,
        success: bool,
        response_code: int | None,
        error: Exception | None,
        num_results: int | None,
        seconds: float,
    ) -> None:
        """Tell the observer, if any, how one attempt went."""
        if api_call_observer is None:
            return
        api_call_observer(
            SearchAPICall(
                host=api.host,
                http_method=request.method,
                url=str(built.url),
                request_body=request_body,
                response_code=response_code,
                success=success,
                error=error,
                num_results=num_results,
                response_time_seconds=seconds,
                attempt_number=attempt_number,
            )
        )

    # Counts the attempts as the retry policy works through them, so each
    # recorded row can say which attempt it was. It has to persist across the
    # calls the retry policy makes to `_attempt`, hence the `nonlocal`.
    attempt = 0

    def _attempt() -> dict[str, Any]:
        # One HTTP attempt: sent, checked and parsed here, so it records itself
        # (with everything in scope) before returning or raising. The retry
        # policy calls this once per attempt, so recording here records them all.
        nonlocal attempt
        attempt += 1
        started = time.monotonic()
        response: httpx.Response | None = None
        try:
            response = client.send(built)
            response.raise_for_status()
            raw: dict[str, Any] = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Record failures we expect:
            # HTTP errors and issues converting the JSON.
            # Everything else just raises straight away
            # (because it's not something we expect or want to record).
            _record(
                attempt_number=attempt,
                success=False,
                response_code=response.status_code if response is not None else None,
                error=exc,
                num_results=None,
                seconds=time.monotonic() - started,
            )
            raise
        _record(
            attempt_number=attempt,
            success=True,
            response_code=response.status_code,
            error=None,
            num_results=_result_count_or_none(read_n_matches, raw),
            seconds=time.monotonic() - started,
        )
        return raw

    try:
        return api.retrying(_attempt)
    except (httpx.HTTPError, ValueError) as exc:
        raise SearchAPIRequestError(api.host) from exc


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


class ClashingFacetsForFacadeError(CouldNotSearchError):
    """
    Raised when `other_terms` sets values for facets that a query already sets

    Unlike [ClashingFacetsError][(m).], this also gives context about the original query
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


@dataclass(frozen=True)
class SearchOutcome:
    """
    What came of a search: the datasets found, how many matched, and what failed

    We deliberately do not carry the raw JSON here.
    Each host's raw documents are kept on the parsed `ParsedDocument.raw_json`,
    so re-exposing the whole response envelope would be redundant.
    The one envelope value worth keeping, the total number of records that matched,
    is surfaced explicitly as `n_matches`.
    """

    parsed_docs: dict[FacadeKey, tuple[ParsedDocument, ...]]
    """The parsed documents each facade answered with, keyed by the facade"""

    n_matches: dict[FacadeKey, int | None]
    """
    How many records each facade reported matched the search, keyed by the facade

    This is the total matched,
    which can exceed the number of documents returned in one page.
    `None` for a facade whose response carried no count we could read.
    """
    # TODO: consider raising if we can't get the number of matches in future.

    failures: dict[FacadeKey, CouldNotSearchError]
    """Reasons we failed to get search results, keyed by the facade"""


def search(  # noqa: PLR0913 - the keyword-only extras are deliberate injection seams
    query: QueryProtocol,
    selector: SearchAPIFacadeSelector = DEFAULT_SELECTOR,
    *,
    stop_at_first_result: bool = True,
    # Limit handling and pagination will be added in PR2.5
    limit: int = 10_000,
    client: httpx.Client | None = None,
    api_call_observer: SearchAPICallObserver | None = None,
    processor: ResultProcessor | None = None,
) -> SearchOutcome:
    """
    Search the facades the selector yields, and parse their answers into datasets

    Parameters
    ----------
    query
        The query to use for the search

    selector
        Chooses which facade to try at each attempt, and when to stop.

    stop_at_first_result
        If `True` (the default), return as soon as one endpoint answers.
        The index nodes largely mirror one another, so one good answer can be enough.

        If `False`, work through every endpoint the selector yields
        and keep each one's answer.
        The nodes do not hold exactly the same data,
        so the union across them is more complete than any single node
        (callers must handle merging and de-duplicating the union themselves).

    limit
        The page size to ask each endpoint for,
        i.e. the most records to get in one response, not the total matched.
        The total matched comes back in the response itself.

    client
        The HTTP client to search with.
        If `None`, one is built for the call and closed at the end.

    api_call_observer
        Told about each request to each API.

        If `None` (the default), nothing is recorded.
        See [esmporium.search.health][] for how to build one.

    processor
        Called with `(host, parsed_documents)` as soon as each endpoint answers,
        so its results can be acted on (e.g. saved) the moment they arrive
        rather than at the end.
        If `None` (the default),
        the parsed documents are still collected into the returned outcome,
        they are just not handed anywhere.
        See [`esmporium.db.build_result_processor`][] for the database-saving one.

    Returns
    -------
    :
        Results of the search

    Raises
    ------
    SelectorOfferedNoAPIFacadeError
        `selector` had no facade to offer for this query at all

    NoFacadeAnsweredError
        The selector offered at least one facade
        and none of them gave us results we could use
    """
    canonical = to_canonical(query)

    parsed_docs: dict[FacadeKey, tuple[ParsedDocument, ...]] = {}
    n_matches: dict[FacadeKey, int | None] = {}
    failures: dict[FacadeKey, CouldNotSearchError] = {}

    owns_client = client is None
    client = client if client is not None else httpx.Client(follow_redirects=True)

    selector_offered_an_option = False

    try:
        attempt = 0
        while (facade := selector(canonical, attempt)) is not None:
            selector_offered_an_option = True
            facade_key = get_facade_key(facade)

            try:
                request = facade.build_search_request(canonical, limit)
                raw = fire(
                    client,
                    facade.search_api,
                    request,
                    api_call_observer,
                    read_n_matches=facade.get_n_matches,
                )
            except ClashingFacetsError as exc:
                failures[facade_key] = ClashingFacetsForFacadeError(
                    query=query, facade=facade, clashing=exc.clashing
                )
            except SearchAPIRequestError as exc:
                failures[facade_key] = CouldNotGetSearchResponseError(
                    facade_key, cause=exc
                )
            else:
                # The facade knows this host's format and project, so it turns the raw
                # answer into datasets here, the moment it arrives.
                # Note: if the selector offers the same host twice,
                # the second answer simply replaces the first here.
                # That is wasteful, because we run the query again, but it is not wrong:
                # the answers are for the same query from the same host,
                # so either will do.
                # This way of handling results is only safe
                # because we only handle a single query in this function
                # and our facades only support searching a single project at a time.
                # If either of those assumptions changed, this would break.
                # We will have to be more careful in higher-level functions
                # to do queries over multiple projects (PR3).
                try:
                    parsed = facade.parse_search_results(raw)
                except UnreadableResponseError as exc:
                    failures[facade_key] = CouldNotUseSearchResultsError(
                        facade_key,
                        cause=exc,
                        url=get_url(facade.search_api, request),
                    )
                else:
                    parsed_docs[facade_key] = parsed
                    n_matches[facade_key] = _result_count_or_none(
                        facade.get_n_matches, raw
                    )
                    if processor is not None:
                        processor(facade, parsed)

                    if stop_at_first_result:
                        break

            attempt += 1

    finally:
        if owns_client:
            client.close()

    if not selector_offered_an_option:
        raise SelectorOfferedNoAPIFacadeError(canonical, selector)

    if not parsed_docs and failures:
        raise NoFacadeAnsweredError(failures)

    return SearchOutcome(parsed_docs, n_matches, failures)
