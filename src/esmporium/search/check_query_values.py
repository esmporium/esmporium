"""
Check query values

This is deliberately opt-in.
We do not control values: values are yours (i.e. the users) to control.
The source of truth for the values is ESGF.
If we said that we knew them, we would be lying.

However, there are some ways that we can help check some values.
We offer those helpers here.
They will not solve every possible bug, but we hope they can help some bugs,
which is better than nothing.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from dataclasses import field as dcfield
from enum import Enum

import httpx

from esmporium.query import (
    CANONICAL_FACETS,
    QueryCanonical,
    QueryProtocol,
    as_query_iterable,
    facet_spec,
    to_canonical,
    translate_to_projects,
)
from esmporium.search.apis import (
    UncompilableFacetPatternError,
    UnreadableResponseError,
)
from esmporium.search.health import SearchAPICallObserver
from esmporium.search.search import (
    FacadeKey,
    NoFacadeAnsweredError,
    SearchAPIRequestError,
    fire,
    get_facade_key,
    get_url,
)
from esmporium.search.search_api_facade import (
    DEFAULT_SELECTOR,
    SearchAPIFacade,
    SearchAPIFacadeSelector,
    SelectorOfferedNoAPIFacadeError,
)

CloseMatcher = Callable[[str, set[str]], tuple[str, ...]]
"""
How to find the allowed values a wrong value is close enough to be a typo of

Given the value the user set and the values which are allowed,
return the ones worth suggesting, best first, or empty if none are close.
"""


class FindingKind(str, Enum):
    """
    What is wrong with a value, as far as we can tell
    """

    CASE = "case"
    """Right value, wrong case: the source lists it under a different casing"""

    TYPO = "typo"
    """Not a known value, but close enough to some that we can suggest them"""

    UNKNOWN = "unknown"
    """Not a known value, and nothing close enough to suggest"""

    MALFORMED = "malformed"
    """
    Does not match the form the source describes

    Used where the source describes a facet with a pattern rather than a list,
    so we can judge shape but not existence.
    """


@dataclass(frozen=True)
class FacetFinding:
    """One facet value that is not an exact match, plus the suggested error"""

    facet: str
    """Canonical facet name, e.g. 'experiment'"""

    value: str
    """What the user provided"""

    kind: FindingKind
    """
    Kind of issue
    """

    suggestions: tuple[str, ...]
    """
    Suggested values (in order or priority/suggestion)
    """


@dataclass(frozen=True)
class ValueReport:
    """The outcome of checking one query against one source of allowed values"""

    query: QueryCanonical
    """
    The query which was checked
    """

    source: FacadeKey
    """
    The facade the allowed values came from

    Used for reporting only
    """

    findings: tuple[FacetFinding, ...]
    """
    Findings of checking the query
    """

    failed_to_check: tuple[str, ...] = ()
    """
    Facets we could not check
    """

    def facet_as_asked(self, facet: str) -> str:
        """
        Get the name the user gave a facet, so a finding reads the way they wrote it

        Someone who wrote `QueryCMIP6(experiment_id=...)` should see
        `experiment_id` back, not `experiment`.

        Parameters
        ----------
        facet
            Canonical facet name, as carried by a
            [FacetFinding][(m).FacetFinding]

        Returns
        -------
        :
            What the user called `facet`

            The canonical name is handed back unchanged if we cannot do better.
        """
        source_query = self.query.source_query
        if source_query is None:
            return facet

        as_asked: str = facet_spec(type(source_query)).canonical_to_native.get(
            facet, facet
        )

        return as_asked

    def ok(self) -> bool:
        """
        Return `True` when we checked everything and nothing looked wrong
        """
        return not self.findings and not self.failed_to_check


class NotAFacetOfTheQueryError(ValueError):
    """
    Raised when we are asked for the values of a facet the query has no room for

    Every facet we check comes from
    [facets_the_user_set][(m).facets_the_user_set],
    so a facet the query cannot hold is a facet we never asked about,
    which means a source has answered a question we did not put to it.
    That is a bug in us or in the source, not a value to report on.
    """

    def __init__(self, facet: str, canonical: QueryCanonical) -> None:
        """
        Initialise the error

        Parameters
        ----------
        facet
            The facet we were asked for the values of

        canonical
            The query we were asked to read it out of
        """
        self.facet = facet
        self.canonical = canonical
        askable = ", ".join(
            sorted(set(CANONICAL_FACETS) | set(canonical.query_specific_facets))
        )
        super().__init__(
            f"{facet!r} is neither a canonical facet nor one of this query's own "
            f"facets, so it has no values to read. Askable facets: {askable}."
        )


def values_set_for(canonical: QueryCanonical, facet: str) -> tuple[str, ...]:
    """
    Get the values the user set for a facet

    Parameters
    ----------
    canonical
        Canonical query

    facet
        Facet whose values to read, named as the user asked for it

    Returns
    -------
    :
        The values the user set for `facet`, or empty if they set none

    Raises
    ------
    NotAFacetOfTheQueryError
        `facet` is not a facet `canonical` can hold values for
    """
    if facet in CANONICAL_FACETS:
        values: tuple[str, ...] = getattr(canonical, facet)
        return values

    if facet not in canonical.query_specific_facets:
        raise NotAFacetOfTheQueryError(facet, canonical)

    return canonical.query_specific_facets[facet]


def close_matches_difflib(
    value: str, allowed: set[str], n: int = 3, cutoff: float = 0.6
) -> tuple[str, ...]:
    """
    Find the allowed values a value is close enough to be a misspelling of

    The default [CloseMatcher][(m).CloseMatcher].

    Parameters
    ----------
    value
        The value the user gave

    allowed
        The values which are allowed

    n
        The number of suggestions to return

    cutoff
        The cutoff for 'close'

    Returns
    -------
    :
        The closest allowed values, best first, or empty if none are close
    """
    return tuple(difflib.get_close_matches(value, allowed, n=n, cutoff=cutoff))


def compare_values(
    canonical: QueryCanonical,
    available: dict[str, set[str]],
    close_matches: CloseMatcher = close_matches_difflib,
) -> tuple[FacetFinding, ...]:
    """
    Compare the values the user set against the values which are allowed

    Only facets present in `available` are judged.

    Parameters
    ----------
    canonical
        Canonical query

    available
        The values each facet is allowed to take

    close_matches
        How to decide which allowed values a wrong one is close to

    Returns
    -------
    :
        One finding per value which is not an exact match
    """
    findings: list[FacetFinding] = []
    for facet in sorted(available):  # sorted -> deterministic output
        allowed = available[facet]

        by_lower = {value.lower(): value for value in allowed}
        for value in values_set_for(canonical, facet):
            if value in allowed:
                # in allowed values i.e. nothing to report
                continue

            cased = by_lower.get(value.lower())
            if cased is not None:
                findings.append(FacetFinding(facet, value, FindingKind.CASE, (cased,)))
                continue

            close = close_matches(value, allowed)
            findings.append(
                FacetFinding(
                    facet,
                    value,
                    FindingKind.TYPO if close else FindingKind.UNKNOWN,
                    close,
                )
            )

    return tuple(findings)


def facets_the_user_set(canonical: QueryCanonical) -> set[str]:
    """
    Get the facets the user actually filled in

    Parameters
    ----------
    canonical
        Canonical query

    Returns
    -------
    :
        The facets the user set, named as they asked for them
    """
    canonical_set = {facet for facet in CANONICAL_FACETS if getattr(canonical, facet)}

    return canonical_set | set(canonical.query_specific_facets)


@dataclass(frozen=True)
class AllowedValues:
    """
    What a source can say about the allowed values of some facets

    An API describes a facet one of two ways, never both:
    by listing its values, or by describing their form.
    Which one it is matters to the caller,
    because they support different claims:
    a listed value which is missing is not published,
    while a value which fails a pattern cannot be valid at all.
    """

    values: dict[str, set[str]] = dcfield(default_factory=dict)
    """The values each facet is allowed to take, for the facets which are listed"""

    patterns: dict[str, re.Pattern[str]] = dcfield(default_factory=dict)
    """The form each facet's values must take, for the facets which are described"""

    def facets_covered(self) -> set[str]:
        """Get the facets this source could say something about"""
        return set(self.values) | set(self.patterns)


class CouldNotGetAllowedValuesError(RuntimeError):
    """
    Raised when a source cannot tell us what the allowed values are

    This is deliberately vague.
    Generally, a subclass of this error provides more specific detail e.g.
    [CouldNotGetAllowedValuesResponseError][(m).] means the source did not answer
    and [CouldNotUseAllowedValuesError][(m).] means it answered
    with something we could not read.
    """

    def __init__(self, message: str, description: str, cause: Exception | None) -> None:
        """
        Initialise the error

        Parameters
        ----------
        message
            The message, which the subclass writes to say why we have no values

        description
            Where we were trying to get allowed values from

        cause
            What went wrong, if we know it, kept on `cause` for callers to inspect
        """
        super().__init__(message)
        self.description = description
        self.cause = cause


class CouldNotGetAllowedValuesResponseError(CouldNotGetAllowedValuesError):
    """
    Raised when a source does not answer our request for facet values
    """

    def __init__(self, description: str, cause: Exception | None = None) -> None:
        """
        Initialise the error

        Parameters
        ----------
        description
            Where we were trying to get allowed values from

        cause
            What went wrong, if we know it

            Folded into the message so that the failure can say *why*,
            and kept on `cause` for callers to inspect.
        """
        why = "" if cause is None else f" ({cause})"
        super().__init__(
            f"{description} did not answer our request for facet values{why}, "
            "so we have nothing to check this query against.",
            description=description,
            cause=cause,
        )


class CouldNotUseAllowedValuesError(CouldNotGetAllowedValuesError):
    """
    Raised when a source answers about facet values with something we cannot read

    Unlike [CouldNotGetAllowedValuesResponseError][(m).], the source did answer:
    what failed was our reading of it.
    """

    def __init__(self, description: str, cause: Exception, url: str | None = None):
        """
        Initialise the error

        Parameters
        ----------
        description
            Where we were trying to get allowed values from

        cause
            What we could not read, kept on `cause` for callers to inspect

        url
            The URL we asked, if we know it

            Folded into the message so a report of this carries
            what someone would need to ask the same question again.
        """
        super().__init__(
            f"{description} answered our request for facet values with something "
            f"we could not read ({cause}), so we have nothing to check this query "
            "against."
            f"{f' We asked: {url}.' if url else ''}",
            description=description,
            cause=cause,
        )
        self.url = url


def allowed_values_from_api(
    facade: SearchAPIFacade,
    client: httpx.Client,
    canonical: QueryCanonical,
    facets: set[str],
    api_call_observer: SearchAPICallObserver | None = None,
) -> AllowedValues:
    """
    Get what a search API facade can tell us about the allowed values of some facets

    Parameters
    ----------
    facade
        The API facade to ask

    client
        The HTTP client to ask with

    canonical
        Canonical query

    facets
        Facets for which to get the allowed values

    api_call_observer
        Told about the request to `facade.search_api`.

        If `None` (the default), nothing is recorded.

    Returns
    -------
    :
        What `facade` can say about each facet

        Facets `facade` has no name for are simply absent:
        as the caller you need to decide what to do about these absences.

    Raises
    ------
    CouldNotGetAllowedValuesResponseError
        `facade.search_api` did not answer

    CouldNotUseAllowedValuesError
        `facade.search_api` answered with something we could not read
    """
    askable = facade.askable_facets(facets)

    request = facade.build_get_facet_values_request(canonical, askable)

    try:
        raw = fire(
            client,
            facade.search_api,
            request,
            api_call_observer,
            read_n_matches=facade.get_n_matches,
        )
    except SearchAPIRequestError as exc:
        raise CouldNotGetAllowedValuesResponseError(facade.search_api.host) from exc

    try:
        return AllowedValues(
            values=facade.parse_facet_values(raw, askable),
            patterns=facade.parse_facet_patterns(raw, askable),
        )
    except (UnreadableResponseError, UncompilableFacetPatternError) as exc:
        raise CouldNotUseAllowedValuesError(
            facade.search_api.host,
            cause=exc,
            url=get_url(facade.search_api, request),
        ) from exc


@dataclass(frozen=True)
class ValueCheckOutcome:
    """
    What came of checking a query: the APIs which answered, and those which did not
    """

    reports: dict[FacadeKey, ValueReport]
    """What each facade which answered said about the query, keyed by the facade"""

    failures: dict[FacadeKey, CouldNotGetAllowedValuesError]
    """Reasons we failed to get allowed values, keyed by the facade"""


def check_query_values_single_project(  # noqa: PLR0913 - the keyword-only extras are deliberate injection seams
    query: QueryProtocol,
    selector: SearchAPIFacadeSelector = DEFAULT_SELECTOR,
    *,
    stop_at_first_result: bool = True,
    close_matches: CloseMatcher = close_matches_difflib,
    client: httpx.Client | None = None,
    api_call_observer: SearchAPICallObserver | None = None,
) -> ValueCheckOutcome:
    """
    Check one single-project query's values against the APIs which would have served it

    This is the low-level, single-project building block: `query` must name exactly one
    project (the selector and facades enforce that). To check a query that names several
    projects, or several queries at once, use the higher-level
    [check_query_values][(m).], which splits multi-project queries and calls this
    function once per project.

    The endpoints are worked through in the order
    [search_single_project][esmporium.search.search.search_single_project]
    would have tried them,
    and this takes the same `stop_at_first_result` as `search_single_project` does,
    so the two answer the same question about the same endpoints
    in the same way.

    Parameters
    ----------
    query
        Query to check

    selector
        Chooses which facade to try at each attempt, and when to stop.

    stop_at_first_result
        If `True` (the default), report on the first endpoint which answers.
        The index nodes largely mirror one another,
        so one good answer can be enough.

        If `False`, ask every endpoint the selector yields
        and keep each one's report.
        The nodes do not hold exactly the same data,
        so a value one of them has never heard of may be published on another --
        comparing the reports is the only way to see that.

    close_matches
        How to decide which allowed values a wrong one is close to

    client
        The HTTP client to ask the APIs with.
        If `None`, one is built for the call and closed at the end.

    api_call_observer
        Told about each request to each API.

        If `None` (the default), nothing is recorded.
        See [esmporium.search.health][] for how to build one.

    Returns
    -------
    :
        What each facade which answered said about the query's values,
        and why each facade which gave us no allowed values gave us none,
        both keyed by the facade

    Raises
    ------
    SelectorOfferedNoAPIFacadeError
        `selector` had no API facade to offer for this query,
        so there was nobody to ask

    NoFacadeAnsweredError
        The selector offered at least one facade
        and none of them gave us facet values we could use
    """
    canonical = to_canonical(query)
    facets = facets_the_user_set(canonical)

    reports: dict[FacadeKey, ValueReport] = {}
    failures: dict[FacadeKey, CouldNotGetAllowedValuesError] = {}

    owns_client = client is None
    client = client if client is not None else httpx.Client(follow_redirects=True)

    selector_offered_an_option = False

    try:
        attempt = 0
        while (facade := selector(canonical, attempt)) is not None:
            selector_offered_an_option = True
            facade_key = get_facade_key(facade)
            try:
                allowed = allowed_values_from_api(
                    facade, client, canonical, facets, api_call_observer
                )
            except CouldNotGetAllowedValuesError as exc:
                failures[facade_key] = exc
            else:
                reports[facade_key] = check_query_values_low(
                    canonical, allowed, facade_key, close_matches
                )
                if stop_at_first_result:
                    break

            attempt += 1

    finally:
        if owns_client:
            client.close()

    if not selector_offered_an_option:
        raise SelectorOfferedNoAPIFacadeError(canonical, selector)

    if not reports and failures:
        raise NoFacadeAnsweredError(failures)

    return ValueCheckOutcome(reports, failures)


def check_query_values(  # noqa: PLR0913 - the keyword-only extras are deliberate injection seams
    queries: QueryProtocol | Iterable[QueryProtocol],
    selector: SearchAPIFacadeSelector = DEFAULT_SELECTOR,
    *,
    stop_at_first_result: bool = True,
    close_matches: CloseMatcher = close_matches_difflib,
    client: httpx.Client | None = None,
    api_call_observer: SearchAPICallObserver | None = None,
    project_query_map: Mapping[str, type[QueryProtocol]] | None = None,
) -> tuple[ValueCheckOutcome, ...]:
    """
    Check one or more queries, over one or more projects, against the APIs

    This is the high-level entry point, and the value-checking mirror of
    [`esmporium.search.search`][]: each query may name one *or more* projects; we split
    every query into one single-project query per project (via
    [translate_to_projects][esmporium.query.translate.translate_to_projects]) and check
    each through [check_query_values_single_project][(m).].

    Unlike [`esmporium.search.search`][], nothing is saved: value-checking only reads
    and reports, so there is no processor here, just the outcomes handed back to you.
    This is the opt-in helper to reach for when a search came back empty and you want to
    know whether a value was mistyped; see [check_query_values_single_project][(m).] for
    what each project's check does and what it can and cannot tell you.

    A query which names no project is refused: splitting raises
    [NoTargetProjectError][esmporium.query.translate.NoTargetProjectError] before any
    endpoint is contacted, so a bad query in the batch stops the whole call before it
    does anything.

    Parameters
    ----------
    queries
        A single query, or an iterable of queries. Each query may name one or more
        projects. A query that names no project is an error (see above).

    selector
        Passed straight through to [check_query_values_single_project][(m).] for every
        sub-query. The default picks facades by the sub-query's (single) project.

    stop_at_first_result
        Passed through to each [check_query_values_single_project][(m).] call.

    close_matches
        Passed through to each [check_query_values_single_project][(m).] call.

    client
        The HTTP client to ask the APIs with, shared across every sub-query. If `None`,
        one is built for the call and closed at the end.

    api_call_observer
        Passed through to each [check_query_values_single_project][(m).] call. See
        [esmporium.search.health][] for how to build one.

    project_query_map
        Passed through to
        [translate_to_projects][esmporium.query.translate.translate_to_projects] when
        splitting a query, to control which query class each project uses. If `None`,
        the default mapping is used.

    Returns
    -------
    :
        One [ValueCheckOutcome][(m).] per sub-query, in the order the sub-queries ran
        (queries in input order; within a query, the projects in the order
        [translate_to_projects][esmporium.query.translate.translate_to_projects]
        yields them).

    Raises
    ------
    NoTargetProjectError
        A query in `queries` names no project. Raised while splitting, before any check
        happens.

    Notes
    -----
    A sub-query that fails outright (e.g. every endpoint it was offered failed) raises,
    just as [check_query_values_single_project][(m).] does; that error propagates and
    stops the run. Aggregating partial failures across sub-queries is deliberately left
    for later.
    """
    # Split every query up front, so a query with no project blows up before we contact
    # any endpoint.
    sub_queries: list[QueryProtocol] = []
    for query in as_query_iterable(queries):
        by_project = translate_to_projects(query, project_query_map=project_query_map)
        sub_queries.extend(by_project.values())

    owns_client = client is None
    client = client if client is not None else httpx.Client(follow_redirects=True)

    outcomes: list[ValueCheckOutcome] = []
    try:
        for sub_query in sub_queries:
            outcomes.append(
                check_query_values_single_project(
                    sub_query,
                    selector,
                    stop_at_first_result=stop_at_first_result,
                    close_matches=close_matches,
                    client=client,
                    api_call_observer=api_call_observer,
                )
            )
    finally:
        if owns_client:
            client.close()

    return tuple(outcomes)


def check_against_patterns(
    canonical: QueryCanonical, patterns: dict[str, re.Pattern[str]]
) -> tuple[FacetFinding, ...]:
    """
    Check values against the form their facet's values have to take

    A pattern says what a value may look like, never whether it exists.
    A value which matches therefore passes silently -- it is a value which
    *could* exist -- and only one which cannot possibly be right is reported.

    Parameters
    ----------
    canonical
        Canonical query

    patterns
        The pattern each facet's values must match

    Returns
    -------
    :
        One finding per value which does not match its facet's pattern

        The suggestion is the pattern itself.
        A regular expression is a poor thing to show a user,
        but it is the only description of the form we have.
    """
    findings: list[FacetFinding] = []
    for facet in sorted(patterns):  # sorted -> deterministic output
        pattern = patterns[facet]
        findings += [
            FacetFinding(facet, value, FindingKind.MALFORMED, (pattern.pattern,))
            for value in values_set_for(canonical, facet)
            if not pattern.fullmatch(value)
        ]

    return tuple(findings)


def check_query_values_low(
    canonical: QueryCanonical,
    allowed: AllowedValues,
    facade_key: FacadeKey,
    close_matches: CloseMatcher = close_matches_difflib,
) -> ValueReport:
    """
    Check a canonical query against ONE set of allowed values

    This does no I/O: getting the values is the caller's job
    (see [allowed_values_from_api][(m).]),
    so what is checked and what is merely reported as unchecked
    is decided here and nowhere else.

    Parameters
    ----------
    canonical
        Canonical query

    allowed
        What the source could say about the query's facets

    facade_key
        How to name where `allowed` came from, for reporting purposes

    close_matches
        How to decide which allowed values a wrong one is close to

    Returns
    -------
    :
        What we could say about the query's values
    """
    facets = facets_the_user_set(canonical)

    findings = (
        *compare_values(canonical, allowed.values, close_matches),
        *check_against_patterns(canonical, allowed.patterns),
    )

    failed_to_check = tuple(sorted(facets - allowed.facets_covered()))

    return ValueReport(canonical, facade_key, findings, failed_to_check)
