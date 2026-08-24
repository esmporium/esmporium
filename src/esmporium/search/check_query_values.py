"""
Opt-in typo/case checker for a facet query that returned NO results.

Sits BESIDE the search ([search][esmporium.search.search.search]); `search()`
never changes.
The intended flow: a search comes back empty -> a UI asks the user "no hits --
check your values against ESGF for typos?" -> on yes, this runs, fetches the
allowed values for each facet the user set, and returns a `ValueReport` of
findings (case slips, likely typos, unknowns). It does NOT prompt and does NOT
re-run the search -- the "did you mean X? [y/n]" glue and any re-run are the
caller's job, so this core stays pure and testable. Motto holds: names are OURS,
values are THEIRS (must be correct case, no typos).

Two SOURCES of "allowed values", one behind the same seam, routed by project:

  VocabularySource  -> {canonical facet: set of allowed values}
       +-- SolrVocabularySource(api)    CMIP5/6: ask the LIVE index node
       |                                (facets=<all>&limit=0, one request).
       |                                Means "values PUBLISHED on that node" --
       |                                right for fully-published projects.
       +-- Cmip7CvVocabularySource()    CMIP7: read the CONTROLLED VOCABULARY
                                        (cmip7-stac.json, fetched on demand).
                                        Means "values the SPEC allows" -- right
                                        because ESGF-NG CMIP7 is near-empty, so
                                        the live index would reject legal-but-
                                        unpublished values as "unknown", AND
                                        ESGF-NG cannot list facet values at all.

This module is library code: it hands back findings and never prints or prompts.
A runnable demo of it (a few wrong queries checked against the live sources)
lives in `scripts/check_query_values_demo.py`, beside the search demo.

The comparison core (`compare_values`) and `ValueReport` are source-agnostic:
both sources emit the same {facet: values} shape, so the SAME difflib-based
tiering (exact -> case-only -> typo -> unknown) runs for every project.

Lookup is PROJECT-SCOPED (each facet checked independently against the full
project vocabulary), NOT conditioned on the user's other facets: with 2+ typos,
conditioning makes GOOD values look unknown, and the checker only runs BECAUSE
the query has mistakes, so that is the common case.

======================================================================
TEST NOTES (recommended; NOT yet implemented -- see the plan).
Per repo policy: pure tests run by DEFAULT, live ones behind the `SearchESGF`
mark, skipped by default.
  - compare_values (PURE, fast): feed canned `available` dicts and assert each
    tier: exact -> no finding; wrong case ("Historical" vs "historical") ->
    kind="case", one suggestion; near miss ("abrupt-4xco2" vs "abrupt4xCO2") ->
    kind="typo", ranked suggestions; nonsense -> kind="unknown", no suggestion;
    a facet absent from `available` -> lands in ValueReport.unchecked, not a
    finding. Cover multi-value facets and a facet with several bad values.
  - Solr parsing (PURE, fast): hand `solr_facet_values` a saved
    `facet_counts.facet_fields` blob (values interleaved with counts) and assert
    it returns the right sets keyed by CANONICAL facet (wire -> canonical map).
  - CV parsing (PURE, fast): vendor a TRIMMED slice of cmip7-stac.json as a
    fixture; assert Cmip7CvVocabularySource._values_from_schema pulls the enums
    for cmip7:experiment_id etc. and keys them by canonical facet. No network.
  - Fail-soft (PURE, fast): CV fetch raising -> allowed_values returns {} and the
    facets show up as `unchecked` (never an exception to the caller).
  - Live (SearchESGF, opt-in): QueryCMIP5(experiment="abrupt-4xco2") against the
    real top node suggests "abrupt4xCO2"; QueryCMIP7(experiment_id="abrupt-4x")
    via the CV suggests "abrupt-4xCO2". One assertion each; these are the
    end-to-end proofs, not the everyday suite.
======================================================================
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

# How close a spelling must be to count as a "did you mean" (0..1). difflib's
# ratio; 0.6 is its own default and errs toward offering a suggestion.
TYPO_CUTOFF = 0.6
MAX_SUGGESTIONS = 3

# Facets whose values are generated identifiers with a known grammar, rather than
# a controlled vocabulary. We never difflib these against a list of "real" values
# -- the near neighbours are just other people's runs, which is noise, not a
# correction. Instead we check their FORM against a grammar when a source gives us
# one (the CMIP7 CV, a STAC collection), or their PRESENCE when a source only
# enumerates values (Solr). See `check_generated_ids` for why the two differ.
GENERATED_ID_FACETS: frozenset[str] = frozenset({"variant_label"})

# A named capture group in a regex, e.g. `(?P<realization_index>\d+)`. The body
# allows one level of nested parentheses, which is all the CV patterns use (the
# initialisation index is itself a `(\d{4}\d{2}[abcde]?|\d+)` alternation).
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
        return pattern
    return rendered.strip("^$")


def _sample_key(value: str) -> tuple[bool, list[int], str]:
    """Sort key for picking sample values: real-looking, low-numbered ones first.

    Real index nodes carry the odd junk value (a bare ``"1"`` turned up live), and
    plain string sorting puts ``r100...`` before ``r2...``. So we (1) push
    all-digit junk to the back, then (2) order by the integers in the value, so a
    variant label reads r1, r2, ... r10 rather than r1, r10, r2.
    """
    return (value.isdigit(), [int(n) for n in re.findall(r"\d+", value)], value)


def sample_values(values: set[str]) -> tuple[str, ...]:
    """Return a small, stable sample of real values, to show a facet's shape by eye."""
    return tuple(sorted(values, key=_sample_key)[:MAX_SUGGESTIONS])


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


# =============================================================================
# What we hand back: findings, not prompts. The caller renders/acts on these.
# =============================================================================
@dataclass(frozen=True)
class FacetFinding:
    """One facet value that is not an exact match, plus what we suspect."""

    facet: str  # canonical facet name, e.g. "experiment"
    value: str  # what the user typed
    kind: str  # "case" (wrong case only) | "typo" (close) | "unknown" (no match)
    suggestions: tuple[str, ...]  # ranked, correctly-cased real values


@dataclass(frozen=True)
class ValueReport:
    """The outcome of checking one query against one vocabulary source."""

    project: str
    source: str  # where the vocabulary came from (a host, or the CV url)
    findings: tuple[FacetFinding, ...]
    unchecked: tuple[str, ...] = ()  # facets we could not get values for

    def ok(self) -> bool:
        """Return True when nothing looked wrong (`unchecked` facets aside)."""
        return not self.findings


# =============================================================================
# The pure comparison core. No network -- feed it the query and a
# {facet: allowed values} map and it tiers each value. This is the tested heart.
# =============================================================================
def compare_values(
    canonical: QueryCanonical, available: dict[str, set[str]]
) -> tuple[FacetFinding, ...]:
    """
    Compare each set facet value against the allowed values for that facet.

    Only facets present in `available` are checked (the rest are `unchecked`,
    handled by the caller). Tiers, in order: exact -> drop; case-insensitive
    match -> "case"; close spelling -> "typo"; nothing close -> "unknown".
    """
    findings: list[FacetFinding] = []
    for facet in sorted(available):  # sorted -> deterministic output
        allowed = available[facet]
        by_lower = {value.lower(): value for value in allowed}
        for value in getattr(canonical, facet):
            if value in allowed:
                continue
            cased = by_lower.get(value.lower())
            if cased is not None:
                findings.append(FacetFinding(facet, value, "case", (cased,)))
                continue
            close = tuple(
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
    """Canonical facets the user actually populated, minus `project`.

    Query-specific facets (e.g. CMIP5 `product`) are included so they surface as
    `unchecked` -- we cannot validate them, and saying so is more honest than
    silently ignoring them.
    """
    canonical_set = {facet for facet in CANONICAL_FACETS if getattr(canonical, facet)}
    canonical_set.discard("project")
    return canonical_set | set(canonical.query_specific_facets)


# =============================================================================
# The seam: a vocabulary source turns a project + facets into {facet: values}.
# =============================================================================
class VocabularySource(Protocol):
    """Something that can list the allowed values of some facets."""

    @property
    def description(self) -> str:
        """Where these values came from, for the report (a host, or a CV url)."""
        ...

    def allowed_values(
        self, canonical: QueryCanonical, facets: set[str]
    ) -> dict[str, set[str]]:
        """List allowed values per canonical facet; omit a facet it cannot answer."""
        ...

    def facet_pattern(self, facet: str) -> re.Pattern[str] | None:
        """Return the grammar for a facet, if this source describes one; else `None`.

        A source that enumerates values (Solr) has no grammar and returns `None`;
        one that describes a facet with a regex (the CMIP7 CV) returns it compiled.
        Used for generated-identifier facets, whose form is worth checking even
        when the exact value cannot be.
        """
        ...


@dataclass
class SolrVocabularySource:
    """CMIP5/6: ask the live index node for its published facet values."""

    api: SearchAPI

    @property
    def description(self) -> str:
        """Where these values come from -- the node host."""
        return self.api.host

    def allowed_values(
        self, canonical: QueryCanonical, facets: set[str]
    ) -> dict[str, set[str]]:
        """One project-scoped facets= request to the node; {} if it never answers."""
        request = self.api.generation.build_get_facet_values_request(canonical, facets)
        with httpx.Client(follow_redirects=True) as client:
            raw = fire(client, self.api, request)
        if raw is None:  # node down / non-transient no -> nothing to check against
            return {}
        return self.api.generation.parse_facet_values(raw, facets)

    def facet_pattern(self, facet: str) -> re.Pattern[str] | None:
        """Solr enumerates values; it does not describe a grammar, so `None`."""
        return None


@dataclass
class Cmip7CvVocabularySource:
    """CMIP7: read allowed values from the controlled vocabulary (cmip7-stac.json)."""

    tag: str = CMIP7_CV_TAG
    _cache: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def description(self) -> str:
        """Where these values come from -- the pinned CV file."""
        return f"CMIP7-CVs@{self.tag} (cmip7-stac.json)"

    def allowed_values(
        self, canonical: QueryCanonical, facets: set[str]
    ) -> dict[str, set[str]]:
        """Pull the enum for each facet from the CV; {} (fail-soft) if unreachable."""
        schema = self._load_schema()
        if not schema:
            return {}
        return self._values_from_schema(schema, facets)

    def facet_pattern(self, facet: str) -> re.Pattern[str] | None:
        """Return the CV's compiled regex for a facet; `None` if it has none.

        `None` also covers a failed fetch. Generated identifiers like
        `variant_label` are described in the CV by a `pattern` rather than an
        `enum`, so this is how their grammar is read.
        """
        schema = self._load_schema()
        prop = self._property_for(schema, facet) if schema else None
        if not prop or "pattern" not in prop:
            return None
        try:
            return re.compile(prop["pattern"])
        except re.error:  # a pattern we cannot compile is one we cannot check against
            return None

    def _load_schema(self) -> dict[str, Any]:
        """Fetch + cache the CV for the session; return {} on ANY fetch/parse error."""
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
        """Return the CV schema property describing a canonical facet, or `None`.

        StacCMIP7Parameters already maps canonical facet -> STAC stem; the CV
        property is that stem under the `cmip7:` prefix. No new mapping table.
        """
        properties = (
            schema.get("definitions", {}).get("item_fields", {}).get("properties", {})
        )
        stem = facet_spec(StacCMIP7Parameters).canonical_to_native.get(facet)
        if stem is None:
            return None
        prop = properties.get(f"{StacCMIP7Parameters.prefix}:{stem}")
        return prop if isinstance(prop, dict) else None

    @staticmethod
    def _values_from_schema(
        schema: dict[str, Any], facets: set[str]
    ) -> dict[str, set[str]]:
        """Map each canonical facet -> its `cmip7:<stem>` enum in the CV schema."""
        out: dict[str, set[str]] = {}
        for facet in facets:
            prop = Cmip7CvVocabularySource._property_for(schema, facet)
            if prop and "enum" in prop:
                out[facet] = set(prop["enum"])
        return out


# =============================================================================
# Routing: pick the source by project. CMIP5/6 -> the SAME top node the search
# used (selector attempt 0); CMIP7 -> the CV. Injectable selector, like search().
# =============================================================================
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


# =============================================================================
# Entry points -- the high/low split. High picks a source by project; low runs
# a given source and tiers the results.
# =============================================================================
def check_query_values(
    query: QueryProtocol, selector: SearchAPISelector = DEFAULT_SELECTOR
) -> ValueReport:
    """Check a query's values against the right vocabulary source for its project."""
    canonical = to_canonical(query)
    source = vocabulary_source_for(canonical, selector)
    if source is None:  # no source -> everything the user set is "unchecked"
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

    - it ENUMERATES the facet (Solr, i.e. the facet is in `available`): we know
      what exists, not the grammar, so a value that is not in the list is
      reported `absent` with a sample of real values. A well-formed value that
      simply was not produced looks the same as a typo here, so we never call it
      malformed and never assign blame -- we just show what does exist.
    - it gives a GRAMMAR (`facet_pattern`, e.g. the CMIP7 CV): we know the form,
      not what exists, so a value that does not match is `malformed` with the
      expected form. A well-formed value passes silently, because we cannot say
      whether that particular run exists.

    Returns the findings and the set of facets we managed to check; a facet the
    source can neither enumerate nor describe is left for the caller's
    `unchecked`.
    """
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

    # Controlled-vocabulary facets go through the difflib tiering. We restrict it
    # to CANONICAL facets: query-specific ones (e.g. CMIP5 `product`) have no
    # attribute on the canonical query to read, and we do not claim to check
    # them, so they fall through to `unchecked` below.
    vocabulary = {facet for facet in facets if facet in CANONICAL_FACETS}
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
