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
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from esmporium.query import (
    CANONICAL_FACETS,
    QueryCanonical,
    QueryProtocol,
    facet_spec,
    to_canonical,
)
from esmporium.search.esgf_generations import StacCMIP7Parameters
from esmporium.search.search import fire
from esmporium.search.search_api import (
    DEFAULT_SELECTOR,
    SearchAPI,
    SearchAPISelector,
)

# TODO: make all this injectable as part of compare_values.
# How close a spelling must be to count as a "did you mean" (0..1). difflib's
# ratio; 0.6 is its own default and errs toward offering a suggestion.
TYPO_CUTOFF = 0.6
MAX_SUGGESTIONS = 3

# Facets whose values we check against a known grammer.
# E.g. variant_label has a known structure "generated identifier"
# We compare a user's value against the known shape, or simply if
# their value exists, in comparison to difflib's "did you mean"
# TODO: remove this - we should not need it.
GENERATED_ID_FACETS: frozenset[str] = frozenset({"variant_label"})

# A named capture group in a regex, e.g. `(?P<realization_index>\d+)`. The body
# allows one level of nested parentheses, which is all the controlled vocabulary
# patterns use (the initialisation index is itself a `(\d{4}\d{2}[abcde]?|\d+)`
# alternation).
# TODO: remove this. Surely it can't be needed?
_NAMED_GROUP = re.compile(r"\(\?P<(?P<name>\w+)>(?:[^()]|\([^()]*\))*\)")


def render_form(pattern: str) -> str:
    r"""
    Turn a facet's regex into a human-readable template using its named groups.

    `^r(?P<realization_index>\d+)i...$` becomes
    `r{realization}i{initialization}p{physics}f{forcing}`: each named group turns
    into `{name}` (a trailing `_index` dropped for brevity) and the literal
    separators between them (`r`, `i`, `p`, `f`) are kept as they are.

    A pattern that names no groups (a bare `^r\d+i\d+...$`, as a STAC collection
    summary gives) cannot be rendered this way, so we hand back the regex itself
    rather than invent labels for it.
    """

    def slot(match: re.Match[str]) -> str:
        return "{" + match.group("name").removesuffix("_index") + "}"

    rendered = _NAMED_GROUP.sub(slot, pattern)
    if rendered == pattern:  # nothing matched, i.e. no named groups to work from
        # Hmm ok this might be handy, if the names are consistently there.
        # Still, for first parse: remove or make very cautious.
        return pattern
    return rendered.strip("^$")


# TODO: remove - too clever
def _sample_key(value: str) -> tuple[bool, list[int], str]:
    """Sort key for picking sample values.

    For example, for variant_label prioritise low-valued integers
    in order next to `r` index. r1, r2, r3, instead of r1, r10, r100.
    """
    return (value.isdigit(), [int(n) for n in re.findall(r"\d+", value)], value)


def sample_values(values: set[str]) -> tuple[str, ...]:
    """Return a small, stable sample of real values, to show a facet's shape by eye."""
    return tuple(sorted(values, key=_sample_key)[:MAX_SUGGESTIONS])


# TODO: I need to clean this up based on the Slack message from last week after you
# asked your colleague about tagging.
# TODO: this should all be able to be deleted
# because we can just get it directly from STAC.
#
# The CMIP7 controlled vocabulary: a JSON Schema whose per-facet properties carry
# `enum`s of the allowed values. Fetched on demand (this whole feature only runs
# after a live search, so we are online anyway) and cached for the session.
# Pinned to a TAG, not a moving branch, so the vocabulary matches a known
# data_specs_version and cannot shift mid-session. TODO: pin a real release tag
# (align with the STAC items' cmip7:data_specs_version, e.g. MIPDS7-0p0p1);
# "main" is a stand-in until we confirm the tag naming.
# Note that the data_specs_version, despite the name, doesn't actually pin the values.
# Let's see if claude can find a way to infer what schema was
# used for a given API response.
# If not, we'll just have to pin (and allow user to override if they want)
# the tag to use and I will go asking in the ESGF slack
# if there's a better way to do this.
CMIP7_CV_TAG = "main"
CMIP7_CV_URL = (
    "https://raw.githubusercontent.com/WCRP-CMIP/CMIP7-CVs/{tag}/cmip7-stac.json"
)


@dataclass(frozen=True)
class FacetFinding:
    """One facet value that is not an exact match, plus the suggested error"""

    facet: str
    """Canonical facet name, e.g. 'experiment'"""
    # TODO: make this support query-specific facet names
    # or have both facet_canonical and facet_user to differentiate

    value: str
    """What the user provided"""

    kind: str  # TODO: make this an enum if it is used as such later # "case" (wrong case only) | "typo" (close) | "unknown" (no match)
    """
    Kind of issue
    """

    suggestions: tuple[str, ...]
    """
    Suggested values (in order or priority/suggestion)
    """


@dataclass(frozen=True)
class ValueReport:
    """The outcome of checking one query against one vocabulary source."""

    project: str
    # TODO: why is this needed?
    # Shouldn't/can't we just use the query
    # or something else which doesn't assume a project identifier?

    source: str  # TODO: better type for this?
    """
    Where the allowed values come from
    """

    findings: tuple[FacetFinding, ...]
    """
    Findings of checking the query
    """

    unchecked: tuple[str, ...] = ()
    """
    Facets that were not checked
    """

    def ok(self) -> bool:
        """Return True when nothing looked wrong (`unchecked` facets aside)."""
        return not self.findings
        # TODO: update to and `and not self.unchecked` ?
        # ok when there is unchecked doesn't seem correct.


def compare_values(
    canonical: QueryCanonical, available: dict[str, set[str]]
) -> tuple[FacetFinding, ...]:
    """
    Compare each set facet value against the allowed values for that facet.

    Only facets present in `available` are checked (the rest are `unchecked`,
    handled by the caller). Tiers, in order: exact -> drop; case-insensitive
    match -> "case"; close spelling -> "typo"; nothing close -> "unknown".
    """
    # TODO: update docstring to match style used everywhere else

    findings: list[FacetFinding] = []
    for facet in sorted(available):  # sorted -> deterministic output
        allowed = available[facet]

        by_lower = {value.lower(): value for value in allowed}
        for value in getattr(canonical, facet):
            if value in allowed:
                # in allowed values i.e. nothing to report
                continue

            cased = by_lower.get(value.lower())
            if cased is not None:
                findings.append(FacetFinding(facet, value, "case", (cased,)))
                continue

            close = tuple(
                # TODO: actually do this injection.
                # TODO: allow this to be injectable,
                # I guess the type is
                # Callable[[str, set[str]], tuple[str, ...]]
                # i.e. give in the value and the known values,
                # get back the close matches.
                difflib.get_close_matches(
                    value, allowed, n=MAX_SUGGESTIONS, cutoff=TYPO_CUTOFF
                )
            )
            findings.append(
                FacetFinding(facet, value, "typo" if close else "unknown", close)
            )

    return tuple(findings)


def facets_the_user_set(canonical: QueryCanonical) -> set[str]:
    """Canonical facets the user populated, minus `project`.

    Query-specific facets (e.g. CMIP5 `product`) are included so they surface as
    `unchecked` -- we cannot validate them, and saying so is more honest than
    silently ignoring them.
    """
    # TODO: update docstring to match style used everywhere else
    # We should be able to validate query-specific facets
    canonical_set = {facet for facet in CANONICAL_FACETS if getattr(canonical, facet)}
    canonical_set.discard("project")
    return canonical_set | set(canonical.query_specific_facets)


# TODO: rename to AllowedValuesSource?
class VocabularySource(Protocol):
    """Something that can list the allowed values of some facets."""

    @property
    def description(self) -> str:
        """Where these values came from, for reporting purposes."""
        ...

    def allowed_values(
        self, canonical: QueryCanonical, facets: set[str]
    ) -> dict[str, set[str]]:
        """
        List the allowed values for a given set of facets

        Parameters
        ----------
        canonical
            Canonical query

        facets
            Facets for which to get the allowed values

        Returns
        -------
        :
            Mapping from facets to the allowed values
        """
        ...

    def facet_pattern(self, facet: str) -> re.Pattern[str] | None:
        """
        Return the regex for a facet, if there is one

        Parameters
        ----------
        facet
            Facet for which to get the pattern if there is one

        Returns
        -------
        :
            Regexp for `facet` if there is one, otherwise `None`
        """
        ...


@dataclass
class SolrVocabularySource:
    """Retrieval of allowed values from SOLR APIs"""

    api: SearchAPI

    @property
    def description(self) -> str:
        """See [VocabularySource.description][(m).]"""
        return self.api.host

    def allowed_values(
        self, canonical: QueryCanonical, facets: set[str]
    ) -> dict[str, set[str]]:
        """See [VocabularySource.allowed_values][(m).]"""
        request = self.api.generation.build_get_facet_values_request(canonical, facets)

        with httpx.Client(follow_redirects=True) as client:
            raw = fire(client, self.api, request)

        # TODO: better error handling here.
        # Silent nothing is wrong.
        # We can do better and say that the request failed, even though it shouldn't.
        # We can then handle that clear error in upper layers as needed.
        if raw is None:
            return {}

        return self.api.generation.parse_facet_values(raw, facets)

    def facet_pattern(self, facet: str) -> re.Pattern[str] | None:
        """See [VocabularySource.facet_pattern][(m).]"""
        return None


# TODO: delete
# - should not be needed now that we know how to get these from the API directly?
# - or maybe it just needs to updated to take a URL from which to get the schema,
#   because the API provides the full URL, rather than the tag
@dataclass
class Cmip7CvVocabularySource:
    """
    CMIP7: read allowed values from the controlled vocabulary (cmip7-stac.json).

    Cannot return a list of known values through STAC.
    [TODO: delete because the statement above turned out to be wrong]
    """

    tag: str = CMIP7_CV_TAG
    _cache: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def description(self) -> str:
        """See [VocabularySource.description][(m).]"""
        # TODO: update once we have clarity about what this is meant to do
        return f"CMIP7-CVs@{self.tag} (cmip7-stac.json)"

    def allowed_values(
        self, canonical: QueryCanonical, facets: set[str]
    ) -> dict[str, set[str]]:
        """See [VocabularySource.allowed_values][(m).]"""
        schema = self._load_schema()
        if not schema:
            # TODO: raise here rather than silently returning.
            # This API should always provide the schema
            return {}

        return self._values_from_schema(schema, facets)

    def facet_pattern(self, facet: str) -> re.Pattern[str] | None:
        """See [VocabularySource.facet_pattern][(m).]"""
        schema = self._load_schema()
        prop = self._property_for(schema, facet) if schema else None
        if not prop or "pattern" not in prop:
            return None

        try:
            return re.compile(prop["pattern"])
        except re.error:
            # TODO: raise here - a pattern which doesn't compile
            # is an API issue we want to know about.
            # Higher level functions can handle this as they wish.
            return None

    def _load_schema(self) -> dict[str, Any]:
        """Fetch + cache the CV for the session; return {} on ANY fetch/parse error."""
        # Should be able to get this via API queries instead.
        # For example, go to https://discovery.east.esgf.io/search.
        # Under "features" there is "stac_extensions".
        # We should be able to get the schema URL from those.
        if "schema" not in self._cache:
            url = CMIP7_CV_URL.format(tag=self.tag)
            try:
                response = httpx.get(url, timeout=30, follow_redirects=True)
                response.raise_for_status()
                self._cache["schema"] = response.json()
            except (httpx.HTTPError, ValueError):
                self._cache["schema"] = {}  # fail soft: the search result stands
        return self._cache["schema"]

    @staticmethod
    def _property_for(schema: dict[str, Any], facet: str) -> dict[str, Any] | None:
        """
        Get the property to look at for a given facet
        """
        properties = (
            schema.get("definitions", {}).get("item_fields", {}).get("properties", {})
        )
        stem = facet_spec(StacCMIP7Parameters).canonical_to_native.get(facet)
        # TODO make this more robust and consider whether returning None is helpful
        # or an error would be clearer to callers of this helper.
        if stem is None:
            return None
        prop = properties.get(f"{StacCMIP7Parameters.prefix}:{stem}")
        return prop if isinstance(prop, dict) else None

    @staticmethod
    def _values_from_schema(
        schema: dict[str, Any], facets: set[str]
    ) -> dict[str, set[str]]:
        """
        Get enum (i.e. explicitly listed) values from the schema

        Patterns etc. handled elsewhere
        """
        out: dict[str, set[str]] = {}
        for facet in facets:
            prop = Cmip7CvVocabularySource._property_for(schema, facet)
            if prop and "enum" in prop:
                out[facet] = set(prop["enum"])
        return out


# TODO: move vocabulary source onto the search API
# (either partly by just keeping references to the classes in this module
# or entirely by deleting the classes in this module
# and moving the functionality onto the search API or SearchAPIGeneration
# classes, I would lean towards the second option I think)
# rather than having it as a hard-coded function like this.
def vocabulary_source_for(
    canonical: QueryCanonical, selector: SearchAPISelector = DEFAULT_SELECTOR
) -> VocabularySource | None:
    """Choose where to get allowed values for this query's project, or None."""
    project = canonical.project[0] if canonical.project else None
    if project in ("CMIP5", "CMIP6"):
        api = selector(canonical, 0)  # the node the search would have hit first
        return SolrVocabularySource(api) if api is not None else None
    if project == "CMIP7":
        return Cmip7CvVocabularySource()
    return None  # a project we have no vocabulary source for


def check_query_values(
    query: QueryProtocol, selector: SearchAPISelector = DEFAULT_SELECTOR
) -> ValueReport:
    """Check a query's values against the right vocabulary source for its project."""
    canonical = to_canonical(query)
    source = vocabulary_source_for(canonical, selector)
    if source is None:  # no source -> everything the user set is "unchecked"
        # TODO: raise error here rather than just saying we didn't check.
        # In general, I don't think 'unchecked' is that helpful.
        # If we can't check, we should be raising clear errors
        # rather than silently passing I think
        # (or at least using a clearer name in the output like
        # "failed_to_check" and making sure that the `ok` property
        # does not return "ok" if we couldn't check something).
        project = canonical.project[0] if canonical.project else ""
        unchecked = tuple(sorted(facets_the_user_set(canonical)))
        return ValueReport(project, "", (), unchecked)

    return check_query_values_low(canonical, source)


def check_generated_ids(
    canonical: QueryCanonical,
    source: VocabularySource,
    facets: set[str],
    available: dict[str, set[str]],
) -> tuple[tuple[FacetFinding, ...], set[str]]:
    """
    Check generated-identifier facets (variant labels) by form or by presence.

    These are not a controlled vocabulary, so the honest thing to say depends on
    what the source can tell us:

    - For SOLR, we know what facet values exist, not the grammmar, for generated-
    identifier facets. If a user's values is not in the list of known facet values,
    it is reported as `absent` with a sample of real values. A well-formed value that
      simply was not produced looks the same as a typo here, so we never call it
      malformed and never assign blame -- we just show what does exist.
    - For CMIP7, we know the correct grammatical form of the generated-identifier
    facets, but not what exists. A well-formed value passes silently, because we
    cannot say whether that particular run exists.

    Returns the findings and the set of facets we managed to check; a facet the
    source can neither enumerate nor describe is left for the caller's
    `unchecked`.
    """
    # TODO: reconsider the usefulness of this.
    # From SOLR, we can just check against the available values.
    # If a value is there, great. If not, treat it as an error like anything else.
    # From STAC, verify against the provided pattern.
    # If it passes, then great, it's a possible value and we shouldn't flag it as wrong
    # (but it should be clear, to the user, that this value is checked against
    # a regexp i.e. for form only,
    # not for whether any actual published values use this value or not),
    # if it fails, then it's clearly wrong and we should tell the user that
    # and show them the pattern it has to match
    # (showing users regexp isn't perfect, but this isn't a perfect module,
    # it will only solve some problems, not all).
    findings: list[FacetFinding] = []
    checked: set[str] = set()
    for facet in facets:
        values = getattr(canonical, facet)

        if facet in available:  # a list source: check presence, not form
            allowed = available[facet]
            findings += [
                FacetFinding(facet, value, "absent", sample_values(allowed))
                for value in values
                if value not in allowed
            ]
            checked.add(facet)
            continue

        pattern = source.facet_pattern(facet)
        if pattern is not None:  # a grammar source: check form, not existence
            form = render_form(pattern.pattern)
            findings += [
                FacetFinding(facet, value, "malformed", (form,))
                for value in values
                if not pattern.fullmatch(value)
            ]
            checked.add(facet)
        # else: neither a list nor a grammar -> we cannot check it at all

    return tuple(findings), checked


def check_query_values_low(
    canonical: QueryCanonical, source: VocabularySource
) -> ValueReport:
    """Check a canonical query against ONE vocabulary source."""
    facets = facets_the_user_set(canonical)
    available = source.allowed_values(canonical, facets)

    # TODO: add support for query-specific facets here too.
    # Drive these with tests.
    # Controlled-vocabulary facets go through the difflib tiering. We restrict it
    # to CANONICAL facets: query-specific ones (e.g. CMIP5 `product`) have no
    # attribute on the canonical query to read, and we do not claim to check
    # them, so they fall through to `unchecked` below.
    vocabulary = {facet for facet in facets if facet in CANONICAL_FACETS}
    # TODO: rethink this in line with other comments above.
    vocabulary -= GENERATED_ID_FACETS
    vocabulary_available = {
        facet: available[facet] for facet in vocabulary if facet in available
    }
    findings = list(compare_values(canonical, vocabulary_available))

    # Generated identifiers (variant labels) are checked by form or presence.
    generated_findings, generated_checked = check_generated_ids(
        canonical, source, facets & GENERATED_ID_FACETS, available
    )
    findings.extend(generated_findings)

    checked = set(vocabulary_available) | generated_checked
    unchecked = tuple(sorted(facets - checked))
    project = canonical.project[0] if canonical.project else ""

    return ValueReport(project, source.description, tuple(findings), unchecked)
