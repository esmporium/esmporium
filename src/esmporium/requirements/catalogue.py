"""
What the solver needs to know about the datasets available

Throughout this package, *the solver* means the part which takes an analysis's
requirement and works out which datasets satisfy it, and which are missing.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from esmporium.datasets import DATASET_FACET_COLUMNS
from esmporium.query import (
    CANONICAL_FACETS,
    ClashingFacetsError,
    QueryCanonical,
    QueryProtocol,
    to_canonical,
)


# A note for developers:
# This looks like a duplicate of
# [UnaskableFacetError][esmporium.search.UnaskableFacetError] and is not one.
# That error is an `AssertionError`: reaching it means a request was
# built naming a facet a query style has no parameter for, i.e. a guard was bypassed
# and the bug is ours. This one is a `ValueError`, because asking is reasonable and
# the answer is simply no: `Query(realm="atmos")` is a fair question that a catalogue
# which did not fill `project_facets` cannot answer.
#
# The two also describe different things. `UnaskableFacetError` is about *names*,
# whether a query style has a parameter for a facet. This is about *recorded values*,
# whether one stored dataset knows a facet -- hence "unrecorded". Search has no
# equivalent, because it asks an API and the API either knows the parameter or errors;
# "what does this stored row know?" only exists on this side of the boundary.
#
# Matching no longer raises this: [CatalogueEntry.facet][(m).CatalogueEntry.facet]
# answers `None` for a facet an entry does not know, so a query naming one finds
# nothing rather than erroring (see the note above [Catalogue][(m).Catalogue]).
# What is left is the one case which really is a caller mistake:
# [facet_filters][esmporium.db.facet_filters] builds SQL and is handed something which
# is not a column, which means the column/non-column split one level up has gone wrong.
class UnrecordedFacetError(ValueError):
    """Raised when an entry does not know a facet it was asked about."""

    def __init__(self, facets: Iterable[str], entry_id: int | None = None) -> None:
        """
        Initialise the error

        Parameters
        ----------
        facets
            The facets the entry does not know

        entry_id
            ID of the entry which was asked, if there was one
        """
        self.facets = tuple(sorted(facets))
        self.entry_id = entry_id
        asked = f" (asked of entry {entry_id})" if entry_id is not None else ""
        recorded = ", ".join(DATASET_FACET_COLUMNS)
        super().__init__(
            f"Cannot select datasets on {', '.join(self.facets)}{asked}: "
            f"every dataset records {recorded}. "
            "For project-specific facets (e.g. CMIP5 `product`), "
            "or facets we can search but do not store "
            "(e.g. `activity`, `realm` and `resolution`), "
            "the catalogue has to put it in each entry's `project_facets`; "
            "for a facet only a search document knows, in `other_terms`."
        )


class DuplicateEntryIDError(ValueError):
    """
    Two or more of a catalogue's entries share an ID

    Not to be confused with
    [UnhandledDatasetClashError][esmporium.db.UnhandledDatasetClashError],
    which is about the *data*: two rows our dataset model cannot tell apart, where
    the fix is to work out which facet we are missing.
    This is about the *code*: an ID identifies one row, so a catalogue which repeats
    one has been built wrongly.
    It cannot arise from the database, where `id` is
    [Dataset.id][esmporium.db.schema.Dataset.id], a primary key.
    """

    def __init__(self, collisions: Mapping[int, int]) -> None:
        """
        Initialise the error

        Parameters
        ----------
        collisions
            Repeated ID -> how many of the catalogue's entries carry it
        """
        self.collisions = dict(sorted(collisions.items()))
        listed = "; ".join(
            f"id {entry_id} is used by {count} entries"
            for entry_id, count in self.collisions.items()
        )
        super().__init__(
            f"A catalogue's entries must have unique IDs, but {listed}. "
            "An ID identifies one dataset row, "
            "so a repeat makes two entries indistinguishable. "
            "This is a mistake in whatever built the catalogue: "
            "read from the database, `id` is a primary key and cannot repeat. "
            "Two rows which describe the same data "
            "but are genuinely different datasets are a different problem, "
            "see esmporium.db.UnhandledDatasetClashError."
        )


@dataclass(frozen=True)
class CatalogueEntry:
    """
    A dataset, as far as the solver is concerned

    The fields mirror the columns of [Dataset][esmporium.db.schema.Dataset],
    so that the two cannot drift apart unnoticed.
    [DatasetFacets][esmporium.search.DatasetFacets] is the mirror image of this
    class on the write side: parsers produce those, and this is what reading a
    stored row gives back.

    An entry is compared by value but is not hashable, because its facet mappings are
    mappings. Anything which needs a key should use `id`.
    """

    id: int
    """
    See [Dataset.id][esmporium.db.schema.Dataset.id]

    Not optional, unlike the column it mirrors:
    an entry describes a row which is already in the database,
    so its ID has been assigned.
    A dataset which has not been saved yet has no entry.
    """

    id_project_specific: str
    """
    See
    [Dataset.id_project_specific][esmporium.db.schema.Dataset.id_project_specific]
    """

    project: str
    """See [Dataset.project][esmporium.db.schema.Dataset.project]."""

    model: str
    """See [Dataset.model][esmporium.db.schema.Dataset.model]."""

    institution: str
    """See [Dataset.institution][esmporium.db.schema.Dataset.institution]."""

    experiment: str
    """See [Dataset.experiment][esmporium.db.schema.Dataset.experiment]."""

    variant_label: str
    """See [Dataset.variant_label][esmporium.db.schema.Dataset.variant_label]."""

    variable: str
    """See [Dataset.variable][esmporium.db.schema.Dataset.variable]."""

    reporting_interval: str
    """See [Dataset.reporting_interval][esmporium.db.schema.Dataset.reporting_interval]."""  # noqa: E501

    grid_label: str | None
    """
    See [Dataset.grid_label][esmporium.db.schema.Dataset.grid_label]

    `None` for a project with no concept of a grid (CMIP5), exactly as the column is.
    A query can therefore never match this facet for such a dataset:
    facet values are strings, and this one has no string value.
    """

    processing_id: str
    """See [Dataset.processing_id][esmporium.db.schema.Dataset.processing_id]."""

    project_facets: Mapping[str, str | None] = field(default_factory=dict)
    """
    Facets we model which no column stores, under their canonical names

    Two kinds of facet live here, and what they have in common is that *we own the
    name*: some query class declares it, so we can translate it and a typo in it is a
    mistake we can point at.
    Project-specific ones, which have no canonical name at all
    (CMIP5's `product`, CMIP6's `sub_experiment_id`).
    And `activity`, `realm` and `resolution`, which *are* canonical facets
    — [Query][esmporium.query.Query] can ask for them and the search APIs
    answer — but which [Dataset][esmporium.db.schema.Dataset] has no column for,
    so they are not in
    [DATASET_FACET_COLUMNS][esmporium.datasets.DATASET_FACET_COLUMNS].

    Which of them an entry may hold is decided by *its own project's* query class, not
    by pooling every class: a CMIP6 search document happens to carry a `product` field,
    and `product` is CMIP5's facet, so a CMIP6 entry records that value in
    `other_terms` rather than claiming to answer for a facet CMIP6 does not have.

    This deliberately does **not** mirror one field of
    [QueryCanonical][esmporium.query.QueryCanonical], and the near-miss is worth
    naming:
    [QueryCanonical.query_specific_facets][esmporium.query.QueryCanonical.query_specific_facets]
    is only the *project-specific*
    part of this (`product`, `sub_experiment_id`), because `activity`, `realm` and
    `resolution` are declared fields there rather than query-specific ones. A query
    reaches everything here through either home, so the split matters on the query
    side and does not on this one.

    How a catalogue fills this is its own business. What the solver needs is that
    anything it should be able to group by, prefer on, or match auxiliary data on is
    here by the time `find` returns, because those comparisons happen on the entry
    rather than in the query.

    One value per facet, as a string. A facet a search document records with several
    values at once -- a genuinely multi-realm dataset -- is therefore recorded here not
    at all rather than partly, so it reads as a facet this entry does not know. That is
    a known limit we have thought about rather than an oversight, and what supporting
    it would take is written down beside the line which drops it, in
    [esmporium.db.database_catalogue][].
    """
    other_terms: Mapping[str, str | None] = field(default_factory=dict)
    """
    Facets only a search document knows, spelled exactly as the document spelled them

    The read side of [Query.other_terms][esmporium.query.Query.other_terms], and the
    same bargain: nothing here is translated, normalised or checked. The keys are the
    document's own, prefixes included, so a stored ESGF-NG STAC document contributes
    `cmip6:activity_id` and a Solr one contributes `activity_id`. That is what makes
    the escape hatch usable from both ends -- the string you sent to the API is the
    string that matches it back -- and it is also why it is per-API: no single document
    uses both spellings, and [matches_facets][(m).matches_facets] is an `and` across
    facet names, so asking for both matches nothing.

    The one difference from the query side is which direction it faces. There it holds
    what the user asked for; here it holds what the documents recorded.

    Values are flattened to a single string because a facet value is a string, so a
    document field holding a list of several or a nested object is left out rather than
    guessed at. The *key* is never touched.
    """

    def facet(self, name: str) -> str | None:
        """
        Get the value of a facet

        `id` and `id_project_specific` are deliberately not facets:
        they identify the dataset rather than describe it,
        so selecting on them is not what this is for.

        A facet this entry does not know reads as `None`, the same as a facet it knows
        and has no value for (CMIP5 and `grid_label`). The two are different questions
        and [known_facets][(m).CatalogueEntry.known_facets] is how to ask the other
        one; they are answered the same way here because *matching* does not care,
        and because an entry which raised would make the result of a query depend on
        which row happened to be asked first.

        Parameters
        ----------
        name
            Facet to get

        Returns
        -------
        :
            The facet's value, or `None` if this entry has no value for it or does not
            know it at all

            A third reason for `None` is worth knowing when one is unexpected: a facet
            the search document recorded with *several* values is not recorded at all,
            because a facet value is one string. See
            [project_facets][(m).CatalogueEntry.project_facets].
        """
        modelled = self.modelled_facet(name)

        return self.other_terms.get(name) if modelled is None else modelled

    def modelled_facet(self, name: str) -> str | None:
        """
        Get the value of a facet we model, without consulting `other_terms`

        The accessor for anything which arrived as a facet *we named*: a declared
        field of a query, a query-specific one, or
        [group_by][esmporium.requirements.Requirement.group_by]. It must not read
        `other_terms`, and the reason is a real failure rather than tidiness. A CMIP6
        search document carries a `product` field of its own, which is not CMIP5's
        `product` -- the two mean different things, which is why CMIP5 declares it and
        CMIP6 does not. That value is recorded in `other_terms` under the document's
        own spelling, so a lookup which fell through to it would answer `product` for a
        CMIP6 dataset and quietly undo the per-project decision
        [project_facets][(m).CatalogueEntry.project_facets] exists to make.

        Parameters
        ----------
        name
            Facet to get, under its canonical name

        Returns
        -------
        :
            The facet's value, or `None` if this entry has no value for it or does not
            model it
        """
        if name in DATASET_FACET_COLUMNS:
            value: str | None = getattr(self, name)
            return value

        return self.project_facets.get(name)

    def modelled_facets(self) -> frozenset[str]:
        """
        Get every facet name this entry can answer for as a facet we model

        The name-set companion to [modelled_facet][(m).CatalogueEntry.modelled_facet],
        and what the solver's checks on `group_by` and `prefer` are asked against:
        those name facets we model, so a name only `other_terms` carries is not an
        answer to them.

        Returns
        -------
        :
            The stored columns, plus whatever keys this entry's `project_facets` carry
        """
        return frozenset(DATASET_FACET_COLUMNS) | frozenset(self.project_facets)

    def known_facets(self) -> frozenset[str]:
        """
        Get every facet name this entry can answer for, by any route

        The companion to [facet][(m).CatalogueEntry.facet], which cannot tell
        "no value" from "no such facet" because both are `None`. The difference matters
        exactly once, and it is load-bearing:
        [group_by][esmporium.requirements.Requirement.group_by] on `grid_label` across
        CMIP5 is one group of datasets which genuinely have no grid, whereas grouping
        on a facet nothing recorded is no grouping at all.

        Wider than [modelled_facets][(m).CatalogueEntry.modelled_facets] by the
        `other_terms` keys, so use this where the question spans both homes -- what a
        requirement could possibly name, in either field of a pair -- and that one
        where it does not.

        Returns
        -------
        :
            The stored columns, plus whatever keys this entry's `project_facets` and
            `other_terms` carry
        """
        return self.modelled_facets() | frozenset(self.other_terms)


# A note for developers:
# `other_terms` is an escape hatch for facet names we do not translate or model.
# Both ends of it are the user's to get right, and that is the deal rather than a
# shortcoming: its keys reach a search API exactly as written, prefixes included, and
# they are matched against a stored document's own keys, also exactly as written. So
# `cmip6:experiment_id` is right for an ESGF-NG STAC endpoint at both ends, and
# `experiment_id` is right for a Solr one at both ends.
#
# What this cannot do is span the two. `matches_facets` is an `and` across facet names,
# so naming one facet under both spellings matches nothing: no single document carries
# both. An analysis which needs both has to solve once per spelling. The thing which
# would lift that is an `any_of` node -- an "or" over leaves -- which
# [Node][esmporium.requirements.Node] does not have (it is `Leaf | AllOf`), so this is
# a limit of the tree rather than of `other_terms`.
#
# For anything longer-lived than a one-off, declare the facet on a query class of
# your own instead: annotate it `QueryFacet(None)` the way CMIP5's `product` is,
# and the facade does the prefixing while `to_canonical` does the translating. It then
# lands in [project_facets][(m).CatalogueEntry.project_facets] under one canonical
# name, askable whichever API found the data.
# See [QueryFacet][esmporium.query.QueryFacet] and
# [esmporium.query.known_queries][].


@dataclass(frozen=True)
class AskedFacets:
    """
    The facets a query constrains, kept in the two groups which are matched differently

    A query holds its facets in three homes and the homes are not interchangeable:
    a facet it declares and a facet of the same name in `other_terms` are different
    questions. [set_facets][(m).set_facets] flattens all three, which is the right
    shape for "does this query constrain anything?" and the wrong shape for matching.
    This keeps the distinction that matching needs.
    """

    modelled: Mapping[str, tuple[str, ...]]
    """
    Facets we model, under canonical names

    The query's declared fields plus its query-specific ones
    ([QueryCanonical.query_specific_facets][esmporium.query.QueryCanonical.query_specific_facets]),
    translated on the way in. Matched against
    an entry's columns and its
    [project_facets][(m).CatalogueEntry.project_facets].
    """

    literal: Mapping[str, tuple[str, ...]]
    """
    Facets from `other_terms`, exactly as the user wrote them

    Not translated, because that is the whole point of the escape hatch. Matched
    against an entry's [other_terms][(m).CatalogueEntry.other_terms], which is keyed
    by the search document's own spelling -- so these have to be the search API's
    names, prefixes and all, to match anything.
    """


def asked_facets(query: QueryProtocol) -> AskedFacets:
    """
    Get the facets a query constrains, split by whether we model their names

    `query` may be written in any query style
    ([QueryCMIP6][esmporium.query.QueryCMIP6], say): the facets it declares are
    translated to canonical names on the way in, so a caller does not have to know
    that our columns are named `experiment` rather than `experiment_id`.
    `other_terms` is deliberately not translated.

    Parameters
    ----------
    query
        Query to inspect

    Returns
    -------
    :
        The facets, in the two groups [AskedFacets][(m).AskedFacets] describes, each
        holding only facets with at least one value

    Raises
    ------
    ClashingFacetsError
        The same facet is set in more than one of the three homes

    Examples
    --------
    >>> from esmporium.query import Query, QueryCMIP5
    >>> asked = asked_facets(QueryCMIP5(variable="tas", product="output1"))
    >>> dict(asked.modelled)
    {'project': ('CMIP5',), 'variable': ('tas',), 'product': ('output1',)}
    >>> dict(asked.literal)
    {}

    The escape hatch stays where it was put, under the spelling it was given:

    >>> asked = asked_facets(Query(other_terms={"cmip6:mip_era": ("CMIP6",)}))
    >>> dict(asked.literal)
    {'cmip6:mip_era': ('CMIP6',)}
    """
    canonical = query if isinstance(query, QueryCanonical) else to_canonical(query)

    declared = {
        name: values
        for name in sorted(CANONICAL_FACETS)
        if (values := getattr(canonical, name))
    }
    query_specific = {
        name: values
        for name, values in canonical.query_specific_facets.items()
        if values
    }
    other = {name: values for name, values in canonical.other_terms.items() if values}

    clashes = (
        (set(declared) & set(query_specific))
        | (set(declared) & set(other))
        | (set(query_specific) & set(other))
    )
    if clashes:
        raise ClashingFacetsError(clashes)

    return AskedFacets(modelled={**declared, **query_specific}, literal=other)


def set_facets(query: QueryProtocol) -> dict[str, tuple[str, ...]]:
    """
    Get the facets a query actually constrains, under canonical names, flattened

    A query holds its facets in three places, and all three are facets:
    the fields it declares, the facets it names which have no canonical
    equivalent, and `other_terms`, which is esmporium's escape hatch for facets no
    query class names. This flattens all three into one mapping, for the questions
    which do not care where a facet came from: whether a query constrains anything at
    all, and whether two queries constrain the same things.

    Matching *does* care, because a declared facet and an `other_terms` facet of the
    same name are answered from different places, so it uses
    [asked_facets][(m).asked_facets] instead.

    `query` may be written in any query style
    ([QueryCMIP6][esmporium.query.QueryCMIP6], say):
    it is translated to canonical names on the way in, so a caller does not have to
    know that our columns are named `experiment` rather than `experiment_id`.

    `other_terms` is deliberately not translated: it is the escape hatch for facets
    no query class names. Its keys here have to be facet names
    a catalogue can use.
    Note too that a facet a query class *does* name is translated while the same
    facet in `other_terms` is not, so `QueryCMIP6(table_id="Amon")` asks for
    `processing_id`, whereas `QueryCMIP6(other_terms={"table_id": ("Amon",)})` asks
    for `table_id` (and likely finds no entry which knows it).

    Parameters
    ----------
    query
        Query to inspect

    Returns
    -------
    :
        Facet name -> values, for every facet with at least one value

        Canonical facets come first, in alphabetical order, so the result is stable.

    Raises
    ------
    ClashingFacetsError
        The same facet is set in more than one of the three places

    Examples
    --------
    >>> from esmporium.query import Query, QueryCMIP6
    >>> set_facets(Query(variable="tas", experiment="historical"))
    {'experiment': ('historical',), 'variable': ('tas',)}

    A query written in a project's own names gives back canonical names.
    Note `project`, which `QueryCMIP6` sets for you:

    >>> set_facets(QueryCMIP6(variable_id="tas", frequency="mon"))
    {'project': ('CMIP6',), 'reporting_interval': ('mon',), 'variable': ('tas',)}
    """
    asked = asked_facets(query)

    return {**asked.modelled, **asked.literal}


def matches_facets(asked: AskedFacets, entry: CatalogueEntry) -> bool:
    """
    Determine whether an entry matches already-split facets

    The companion to [asked_facets][(m).asked_facets], and separate from
    [matches][(m).matches] so that a catalogue can split a query once and then check
    every entry against the result, rather than re-splitting the same query for each
    entry. Both catalogues we ship do exactly that --
    [InMemoryCatalogue.find][(m).InMemoryCatalogue.find] over its own entries, and
    [esmporium.db.DatabaseCatalogue][] over the rows its `select` returned -- which
    is how the two come to agree about a facet only an entry can answer for.

    Each group is looked up where that group's facets live:
    `modelled` against the entry's columns and
    [project_facets][(m).CatalogueEntry.project_facets],
    `literal` against its [other_terms][(m).CatalogueEntry.other_terms].
    A facet the entry cannot answer for reads as `None`, which no query can ask for
    (facet values are strings), so it simply does not match. Nothing here raises.

    Several values for one facet are an "or", exactly as they are when searching.
    Several facet *names* are an "and", which is why naming one facet under two
    spellings (`cmip6:source_id` and `source_id`, say) matches nothing: no one search
    document carries both.

    Parameters
    ----------
    asked
        The facets to match, as returned by [asked_facets][(m).asked_facets]

    entry
        Entry to check

    Returns
    -------
    :
        `True` if `entry` matches every facet in `asked`
    """
    return all(
        entry.modelled_facet(name) in values for name, values in asked.modelled.items()
    ) and all(
        entry.other_terms.get(name) in values for name, values in asked.literal.items()
    )


def matches(query: QueryProtocol, entry: CatalogueEntry) -> bool:
    """
    Determine whether a dataset matches a query

    An entry matches if, for every facet the query sets,
    the entry's value is one of the query's values.
    Several values for one facet are therefore an "or",
    exactly as they are when searching.

    A query which sets no facets constrains nothing, so it matches every entry.
    That is the right answer to the question as asked here, but it is a sharp edge
    one level up: an empty query on a requirement's leaf would quietly select the
    whole catalogue, which is why
    [EmptyLeafQueryError][esmporium.requirements.EmptyLeafQueryError] refuses one
    there.

    Parameters
    ----------
    query
        Query to match against

    entry
        Entry to check

    Returns
    -------
    :
        `True` if `entry` matches `query`

    Raises
    ------
    ClashingFacetsError
        `query` sets the same facet in more than one place
    """
    return matches_facets(asked_facets(query), entry)


# A note for developers: the database-backed catalogue
# ([esmporium.db.DatabaseCatalogue][]) is what fills both facet mappings, out of the
# stored raw search documents. What goes in which is on the fields themselves; two
# things about doing it from documents are decisions rather than details, and belong
# here:
#
# - **Which document answers.** A dataset has many versions and a version can be
#   described by several documents. The one that answers belongs to the newest version
#   which passed the catalogue's `availability` filter -- not simply the newest version
#   -- so a caller who excluded a retracted version is not then described by it.
#
#   TODO future: this is the piece to revisit when a requirement can ask for a
#   particular version, which it cannot today. The reason is not that version is hard
#   to plumb, it is what an entry *is*: **one entry describes one `Dataset` row, not one
#   `DatasetVersion`**, and the availability filter decides which version's documents
#   speak for it. So "prefer version X" has nothing to range over -- there is a single
#   entry per dataset however many versions exist -- and expressing it needs either a
#   `version` field on the entry (making "prefer latest" trivially true and "this
#   version" a filter rather than a preference) or one entry per
#   `(dataset, version)`, which changes what `id` means and what a group is.
#
#   Note also where version would *not* go. It is not a facet: facets are what a query
#   matches on, and version is metadata about which snapshot answered, like `id`. So it
#   belongs as a field of its own rather than in `project_facets` or `other_terms`,
#   both of which are built *from* a particular version's documents and so cannot be
#   how you choose between them.
#
# - **A facet nothing records finds nothing, unconditionally.** `facet` answers `None`
#   for one, and no query can ask for `None`, so an empty table and a populated one
#   give the same answer. That is what makes this one rule rather than two, and
#   `test_solving_against_the_database_matches_the_in_memory_catalogue` holds both
#   catalogues to it. It used to depend on which row happened to be asked first;
#   `test_a_facet_nothing_records_finds_nothing_either_way` records why.
#
#   What is *not* silent is a facet a requirement groups or prefers on: see
#   [UngroupableFacetError][esmporium.requirements.UngroupableFacetError] and
#   [UnrankableFacetError][esmporium.requirements.UnrankableFacetError]. The asymmetry
#   is deliberate. A query naming a facet nothing records is a question with the answer
#   "nothing", which is a fact about the data. A *grouping* on one cannot produce
#   groups at all, so it is a mistake in the requirement, and
#   [known_facets][(m).CatalogueEntry.known_facets] is what lets the solver tell that
#   apart from a facet which is known and simply has no value.


class Catalogue(Protocol):
    """
    The datasets available to the solver, and what is known about them
    """

    def find(self, query: QueryProtocol) -> tuple[CatalogueEntry, ...]:
        """
        Find every dataset which matches a query

        Note for implementers: Matching on a facet we have no column for is essential.
        For example, a query naming `product` or `realm` must match whenever the
        entry's [project_facets][(m).CatalogueEntry.project_facets] carries it, and a
        query naming something through `other_terms` must match whenever the entry's
        [other_terms][(m).CatalogueEntry.other_terms] carries that exact spelling.
        What is up to each catalogue is where those come from —
        [esmporium.db.DatabaseCatalogue][] reads them out of the raw search documents,
        another might consult a project-specific table, or simply know.
        So anything the solver should be able to group by, prefer on or match
        auxiliary data on has to be in one of the two by the time `find` returns,
        whether or not `query` happens to mention it.

        Parameters
        ----------
        query
            Query to match

        Returns
        -------
        :
            Matching datasets, in a stable order
        """
        ...


@dataclass(frozen=True)
class InMemoryCatalogue:
    """
    A catalogue held entirely in memory

    Intended for tests and for prototyping, and expected to stay that way: it lets
    everything built on top of [Catalogue][(m).Catalogue] be written and tested
    without a database, which is how each new piece of this package gets written.

    The database-backed one is [esmporium.db.DatabaseCatalogue][]. The two have to
    give the same answers -- that is what makes testing against this one mean anything
    -- and `test_solving_against_the_database_matches_the_in_memory_catalogue` is what
    holds them together.
    """

    entries: tuple[CatalogueEntry, ...]
    """The datasets available"""

    def __post_init__(self) -> None:
        """
        Check that no two entries share an ID

        Raises
        ------
        DuplicateEntryIDError
            Two or more entries have the same `id`
        """
        # `Counter` rather than counting each ID against the whole list, which rescans
        # it once per entry. This class stands in for a real catalogue, so it should
        # not go quadratic on the number of datasets.
        counts = Counter(entry.id for entry in self.entries)
        # The count is the whole report. Saying *which* entries collided means
        # choosing the facets which identify one, and no such choice survives moving
        # between projects (see `DuplicateEntryIDError`). A repeated ID is a mistake
        # in the code which built the catalogue, and the ID is the literal that code
        # wrote, so it is the thing to go and look for.
        collisions = {
            entry_id: count for entry_id, count in counts.items() if count > 1
        }
        if collisions:
            raise DuplicateEntryIDError(collisions)

    def find(self, query: QueryProtocol) -> tuple[CatalogueEntry, ...]:
        """
        Find every dataset which matches a query

        Parameters
        ----------
        query
            Query to match

        Returns
        -------
        :
            Matching datasets, in the order they were given to the catalogue

        Raises
        ------
        ClashingFacetsError
            `query` sets the same facet in more than one place
        """
        asked = asked_facets(query)

        return tuple(entry for entry in self.entries if matches_facets(asked, entry))
