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
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dcfield
from enum import Enum
from typing import Protocol

import httpx

from esmporium.query import (
    CANONICAL_FACETS,
    QueryCanonical,
    QueryProtocol,
    facet_spec,
    to_canonical,
)
from esmporium.search.esgf_generations import native_facet_names
from esmporium.search.search import fire
from esmporium.search.search_api import (
    DEFAULT_SELECTOR,
    SearchAPI,
    SearchAPISelector,
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

    source: str
    """
    How the source of allowed values describes itself

    Used for reporting only, hence just a string
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


class AllowedValuesSource(Protocol):
    """Something that can say what the allowed values of some facets are"""

    @property
    def description(self) -> str:
        """Where these values came from, for reporting purposes."""
        ...

    def allowed_values(
        self, canonical: QueryCanonical, facets: set[str]
    ) -> AllowedValues:
        """
        Get the allowed values for a given set of facets

        Parameters
        ----------
        canonical
            Canonical query

        facets
            Facets for which to get the allowed values

        Returns
        -------
        :
            What this source can say about each facet

        Raises
        ------
        CouldNotGetAllowedValuesError
            The source could not be reached, or would not answer
        """
        ...


class CouldNotGetAllowedValuesError(RuntimeError):
    """
    Raised when a source cannot tell us what the allowed values are
    """

    def __init__(self, description: str) -> None:
        """
        Initialise the error

        Parameters
        ----------
        description
            Where we were trying to get allowed values from
        """
        self.description = description
        super().__init__(
            f"{description} did not answer our request for facet values, "
            "so we have nothing to check this query against."
        )


@dataclass
class SearchAPIValuesSource:
    """
    Retrieval of allowed values from a search API
    """

    api: SearchAPI

    client: httpx.Client
    """
    The HTTP client to ask with
    """

    @property
    def description(self) -> str:
        """See [AllowedValuesSource.description][(m).]"""
        return self.api.host

    def allowed_values(
        self, canonical: QueryCanonical, facets: set[str]
    ) -> AllowedValues:
        """See [AllowedValuesSource.allowed_values][(m).]"""
        askable = set(native_facet_names(self.api.generation.params, facets))

        request = self.api.generation.build_get_facet_values_request(canonical, askable)

        raw = fire(self.client, self.api, request)

        if raw is None:
            raise CouldNotGetAllowedValuesError(self.description)

        return AllowedValues(
            values=self.api.generation.parse_facet_values(raw, askable),
            patterns=self.api.generation.parse_facet_patterns(raw, askable),
        )


class NoSourceWouldAnswerError(RuntimeError):
    """
    Raised when every source we asked refused to say what the allowed values are

    Carries all of the refusals rather than only the last,
    because which endpoints refused is the interesting part:
    one node being down says nothing, all of them being down says a lot,
    and only the whole list tells you which it was.
    """

    def __init__(self, refusals: tuple[CouldNotGetAllowedValuesError, ...]) -> None:
        """
        Initialise the error

        Parameters
        ----------
        refusals
            What each source said, in the order they were asked
        """
        self.refusals = refusals
        self.described = tuple(refusal.description for refusal in refusals)
        asked = "\n".join(f"  - {refusal}" for refusal in refusals)
        super().__init__(
            f"Asked {len(refusals)} source(s) for facet values and none answered, "
            f"so we have nothing to check this query against:\n{asked}"
        )


def check_query_values(
    query: QueryProtocol,
    selector: SearchAPISelector = DEFAULT_SELECTOR,
    *,
    stop_at_first_result: bool = True,
    close_matches: CloseMatcher = close_matches_difflib,
    client: httpx.Client | None = None,
) -> dict[str, ValueReport]:
    """
    Check a query's values against the APIs which would have served it

    The endpoints are worked through in the order
    [search][esmporium.search.search.search] would have tried them,
    and this takes the same `stop_at_first_result` as `search` does,
    so the two answer the same question about the same endpoints
    in the same way.

    Parameters
    ----------
    query
        Query to check

    selector
        How to pick the API to ask about allowed values at each attempt

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

    Returns
    -------
    :
        What each API said about the query's values, keyed by host.
        An API which refused to answer is left out.

        Empty if the selector had no API to offer,
        which is not the same as one refusing:
        there was nobody to have refused.

    Raises
    ------
    NoSourceWouldAnswerError
        The selector offered at least one API and none of them answered
    """
    canonical = to_canonical(query)

    reports: dict[str, ValueReport] = {}
    refusals: list[CouldNotGetAllowedValuesError] = []

    owns_client = client is None
    client = client if client is not None else httpx.Client(follow_redirects=True)

    try:
        attempt = 0
        while (api := selector(canonical, attempt)) is not None:
            source = SearchAPIValuesSource(api, client)
            try:
                reports[source.description] = check_query_values_low(
                    canonical, source, close_matches
                )
            except CouldNotGetAllowedValuesError as exc:
                refusals.append(exc)
            else:
                if stop_at_first_result:
                    break

            attempt += 1

    finally:
        if owns_client:
            client.close()

    if not reports and refusals:
        raise NoSourceWouldAnswerError(tuple(refusals))

    return reports


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
    source: AllowedValuesSource,
    close_matches: CloseMatcher = close_matches_difflib,
) -> ValueReport:
    """
    Check a canonical query against ONE source of allowed values

    Parameters
    ----------
    canonical
        Canonical query

    source
        Where to get the allowed values from

    close_matches
        How to decide which allowed values a wrong one is close to

    Returns
    -------
    :
        What we could say about the query's values

    Raises
    ------
    CouldNotGetAllowedValuesError
        `source` would not tell us what the allowed values are
    """
    facets = facets_the_user_set(canonical)
    allowed = source.allowed_values(canonical, facets)

    findings = (
        *compare_values(canonical, allowed.values, close_matches),
        *check_against_patterns(canonical, allowed.patterns),
    )

    failed_to_check = tuple(sorted(facets - allowed.facets_covered()))

    return ValueReport(canonical, source.description, findings, failed_to_check)
