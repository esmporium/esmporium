"""
What the solver needs to know about the datasets available

Throughout this package, *the solver* means the part which takes an analysis's
requirement and works out which datasets satisfy it, and which are missing.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
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
#
# There is a third in the family, [UnloadedFacetError][(m).UnloadedFacetError] below,
# and the three divide cleanly by what they are about. `UnaskableFacetError` is about
# *names*: no query style has a parameter for this one. This one is about *recorded
# values*: no stored dataset records this facet, so there is nothing to select on.
# `UnloadedFacetError` is about *what was read*: the facet may well be recorded, and
# this entry was built without looking, because whoever asked said they would not need
# it.
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


class UnloadedFacetError(ValueError):
    """Raised when an entry was built without reading the facet it is asked about."""

    def __init__(
        self, facets: Iterable[str], loaded: Iterable[str], entry_id: int
    ) -> None:
        """
        Initialise the error

        Parameters
        ----------
        facets
            The facets the entry was not built to answer for

        loaded
            The facets it was built for, i.e. its
            [CatalogueEntry.facets_loaded][(m).CatalogueEntry.facets_loaded]

        entry_id
            ID of the entry which was asked
        """
        self.facets = tuple(sorted(facets))
        self.loaded = tuple(sorted(loaded))
        self.entry_id = entry_id
        asked = ", ".join(self.facets)
        super().__init__(
            f"Entry {entry_id} was not built to answer for {asked}. "
            f"The facets it was built for are {', '.join(self.loaded)}. "
            "A lookup was told that only those would be read, so the stored search "
            "documents, which are where a facet no column holds is recorded, were not "
            f"read for this entry. Ask the catalogue again with `needs_facets` "
            f"including {asked}; from a solve, name the facet in the requirement -- in "
            "a leaf's query, `where`, `group_by`, `group_by_other_terms`, `prefer` or "
            "`prefer_other_terms` -- and solving will load it."
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

    Some query class declares this facet, so we can translate it.

    This includes, for example, a project-specific facet, which have no canonical
    name at all, such as CMIP5's `product` or CMIP6's `sub_experiment_id`, or a
    facet which has a canonical name but no column stores it, such as `activity`,
    `realm`, or `resolution`. For the latter, this means [Query][esmporium.query.Query]
    can ask for them, but they are not in
    [DATASET_FACET_COLUMNS][esmporium.datasets.DATASET_FACET_COLUMNS].

    A facet which several of a dataset's stored documents record is answered by the
    first-stored one, so the search which first found a dataset is the one whose
    index node describes it. That is stable from then on -- re-searching can add a
    facet but not change one -- and differs in a database rebuilt from scratch. See
    the note above `_facets_from_documents` in
    [esmporium.db.database_catalogue][], which is also where to look when a value
    here is not the one you expected.
    """

    other_terms: Mapping[str, str | None] = field(default_factory=dict)
    """
    Facets only a search document knows, spelled exactly as the document spelled them

    The read side of [Query.other_terms][esmporium.query.Query.other_terms], and the
    same bargain: nothing here is translated, normalised or checked. The keys are the
    document's own. The one difference from the query side is which direction it faces.
    There it holds what the user asked for; here it holds what the documents recorded.

    Subject to the same first-stored-document merge as
    [project_facets][(m).CatalogueEntry.project_facets], and more exposed to it: a
    facet we do not model is exactly the kind the index nodes are least consistent
    about recording.
    """

    facets_loaded: frozenset[str] | None = None
    """
    Which facets this entry was built to answer for, when it was built for only some

    `None` means everything this entry could know was loaded. That is what a lookup
    which was not told what would be read gives back, and what an entry written down by
    hand holds, so it is the default.

    A frozenset is the record of a lookup which *was* told
    ([needs_facets][(m).Catalogue.find]) and so did not read the stored search
    documents.

    It always contains
    [DATASET_FACET_COLUMNS][esmporium.datasets.DATASET_FACET_COLUMNS], because those
    are columns of the row and arrive with it whether or not anybody asked for them.
    """

    def _check_facet_was_loaded(self, name: str) -> None:
        """
        Check that this entry was built to answer for a facet

        Returns nothing: this is here to raise.

        Parameters
        ----------
        name
            Facet to check

        Raises
        ------
        UnloadedFacetError
            This entry was built without reading `name`
        """
        if self.facets_loaded is not None and name not in self.facets_loaded:
            raise UnloadedFacetError([name], self.facets_loaded, self.id)

    def facet(self, name: str) -> str | None:
        """
        Get the value of a facet

        `id` and `id_project_specific` are deliberately not facets:
        they identify the dataset rather than describe it,
        so selecting on them is not what this is for.

        Parameters
        ----------
        name
            Facet to get

        Returns
        -------
        :
            The facet's value, or `None` if this entry has no value for it or does not
            know it at all

            Two further reasons for `None` are worth knowing when one is unexpected.
            A facet the search document recorded with *several* values is not
            recorded at all, because a facet value is one string. And a facet only
            *some* of the dataset's documents recorded reads as `None` when the
            document which answered was not one of them -- which depends on which
            index node answered the search, not on the data. See
            [project_facets][(m).CatalogueEntry.project_facets] for both.

        Raises
        ------
        UnloadedFacetError
            This entry was built without reading `name`, i.e. `name` is not in its
            [facets_loaded][(m).CatalogueEntry.facets_loaded]
        """
        self._check_facet_was_loaded(name)

        modelled = self.modelled_facet(name)

        return self.other_terms.get(name) if modelled is None else modelled

    def modelled_facet(self, name: str) -> str | None:
        """
        Get the value of a facet we model, without consulting `other_terms`

        The accessor for anything which arrived as a facet *we named*: a declared
        field of a query, or a query-specific one. It must not read
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

        Raises
        ------
        UnloadedFacetError
            This entry was built without reading `name`, i.e. `name` is not in its
            [facets_loaded][(m).CatalogueEntry.facets_loaded]
        """
        self._check_facet_was_loaded(name)

        if name in DATASET_FACET_COLUMNS:
            value: str | None = getattr(self, name)
            return value

        return self.project_facets.get(name)

    def modelled_facets(self) -> frozenset[str]:
        """
        Get every facet name this entry can answer for as a facet we model

        The name-set companion to [modelled_facet][(m).CatalogueEntry.modelled_facet],
        and what the solver's checks on `group_by` and `prefer` are asked against --
        not their `_other_terms` counterparts, which are never checked: those name
        facets we model, so a name only `other_terms` carries is not an answer to
        them.

        Returns
        -------
        :
            The stored columns, plus whatever keys this entry's `project_facets` carry

            For an entry built for only some facets (see
            [facets_loaded][(m).CatalogueEntry.facets_loaded]) that is the names it
            loaded, and asking is not an error: this is a question about names rather
            than values, and the honest answer to "what can you answer for?" is the
            short list.
        """
        return frozenset(DATASET_FACET_COLUMNS) | frozenset(self.project_facets)

    def known_facets(self) -> frozenset[str]:
        """
        Get every facet name this entry can answer for, by any route

        Returns
        -------
        :
            The stored columns, plus whatever keys this entry's `project_facets` and
            `other_terms` carry

            As with [modelled_facets][(m).CatalogueEntry.modelled_facets], an entry
            built for only some facets answers with the names it loaded rather than
            raising.
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
    by the search document's own spelling.
    """


def asked_facets(query: QueryProtocol) -> AskedFacets:
    """
    Get the facets a query constrains, split by whether we model their names

    `query` may be written in any query style, we handle the translation to
    canonical facet names for modelled facets.

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
#   which passed the lookup's [VersionFilter][(m).VersionFilter] -- not simply the
#   newest version -- so a caller who excluded a retracted version is not then
#   described by it.
#
#   TODO future: this is the piece to revisit when a requirement can ask for a
#   particular version, which it cannot today. The reason is not that version is hard
#   to plumb, it is what an entry *is*: **one entry describes one `Dataset` row, not one
#   `DatasetVersion`**, and the versions filter decides which version's documents
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


# TODO future: these three settings are meant to become two, and the first half of that
# has now happened. The direction was to express retraction and recency where the
# *requirement* can see them rather than as settings on the catalogue reading it, so
# that "latest", "this particular version" and "give me everything, with
# `Cardinality.ALL`" are all things a requirement says.
#
# What has happened: `NOT_RETRACTED` is the default, so the common path no longer drops
# a dataset whose newest version was retracted but which has a usable older one; and
# this is now an argument to [Catalogue.find][(m).Catalogue.find], carried there by
# [Requirement.versions][esmporium.requirements.Requirement.versions] rather than held
# on a catalogue. So retraction is said in the requirement, is fingerprinted by
# `requirement_hash` along with everything else it asks for, and a catalogue is once
# again just "everything we have stored".
#
# What has not: `LATEST_NOT_RETRACTED` still exists, because recency is still a filter
# here rather than a preference in the requirement, and until a requirement can say
# "prefer the latest" there would be no way left to ask "only what is current" --
# removing the member would delete the question rather than move it. Deleting it is the
# last step, not the first.
#
# Important to note:
# - The preference half needs a version on the entry first. Why that is not a small
#   change is in the note above [Catalogue][(m).Catalogue]: an entry describes a
#   `Dataset`, not a `DatasetVersion`, so today there is nothing for a preference to
#   range over.
#
# `test_a_retracted_dataset_drops_its_group_from_the_solve` and
# `test_a_group_whose_other_leaf_is_retracted_becomes_unsatisfied` in
# `tests/integration/requirements/test_search_to_solve_recorded.py` are what pin the
# current behaviour, and are the tests to re-read when this moves.
#
# When we do this, also consider whether not retracted
# should just become something we express via preference,
# rather than requiring its own special 'VersionFilter' concept
# (thinking about it now, it is kind of special so having it as a filter,
# or a constraint when we get them, is better than as a preference I think
# (unlike versions, which are more naturally expressed as preferences,
# but could also be constraints I guess))


class VersionFilter(str, Enum):
    """
    Which of a dataset's stored versions count when looking for it

    A dataset has many versions and this says which of them a lookup accepts. It
    decides two things, not one: whether the dataset is found at all (it needs a
    version which qualifies), and which version's search documents describe it (the
    newest which qualifies). So this is not a filter applied to a finished answer,
    it is part of how the answer is read.
    """

    ANY = "any"
    """
    Every dataset on record, including ones whose only versions are retracted

    Use this to ask what we have ever seen rather than what is usable.
    """

    NOT_RETRACTED = "not_retracted"
    """
    The dataset has a version which was not retracted, latest or not

    The default, because a dataset's newest usable version beats nothing. A dataset
    whose latest version was retracted is still usable from the one before it, and the
    document which describes it here is that older version's -- so what the entry says
    matches what a caller would get.
    """

    # Note: Expected to go once recency can be expressed as a *preference* rather
    # than a filter -- see the note above [VersionFilter][(m).VersionFilter].
    LATEST_NOT_RETRACTED = "latest_not_retracted"
    """
    The dataset has a version which was both the latest and not retracted

    Stricter than the default: the dataset drops out entirely unless what ESGF last
    called its newest version is also one it has not retracted. Use it to ask "only
    what is current", and note it also drops a dataset whose single live version is
    simply not flagged latest.
    """


class Catalogue(Protocol):
    """
    Every dataset we have, and what is known about them

    A catalogue is the *contents*: every dataset on record and every version of each.
    """

    def find(
        self,
        query: QueryProtocol,
        *,
        versions: VersionFilter = VersionFilter.NOT_RETRACTED,
        needs_facets: Collection[str] | None = None,
    ) -> tuple[CatalogueEntry, ...]:
        """
        Find every dataset which matches a query

        The first two arguments narrow a lookup along two different axes, and neither
        is a property of the catalogue: `query` says which facet values are acceptable,
        `versions` says which of a dataset's versions are. The third narrows nothing --
        it says what the answer will be *asked*, so a catalogue can leave unread what
        nobody will read.

        Parameters
        ----------
        query
            Query to match

        versions
            Which of a dataset's versions count. See
            [VersionFilter][(m).VersionFilter].

            Not only a filter on what comes back: it also decides which version's
            search documents describe each entry, so two lookups which differ only in
            this can report different values for a facet no column holds. They are
            therefore not subsets of one another.

        needs_facets
            The facets the caller will ask the entries about

            `None`, the default, means every facet the catalogue can answer for, which
            is the right answer for a caller who does not know yet what they will read.

            Naming them lets a catalogue skip work. The database-backed one answers for
            a facet no column holds by reading that dataset's stored search documents,
            which is the bulk of a lookup, so it can skip reading them altogether when
            nothing named here needs one. What comes back then records what it was
            built for in
            [CatalogueEntry.facets_loaded][(m).CatalogueEntry.facets_loaded], and
            raises [UnloadedFacetError][(m).UnloadedFacetError] for anything else
            rather than answering `None` and being mistaken for a dataset which records
            no value.

        Returns
        -------
        :
            Matching datasets, in a stable order
        """
        ...


def _narrowed(entry: CatalogueEntry, needs_facets: Collection[str]) -> CatalogueEntry:
    """
    Get an entry as a lookup told what would be read would have built it

    Parameters
    ----------
    entry
        Entry to narrow

    needs_facets
        The facets the caller said they would ask about, i.e. `needs_facets` as
        [Catalogue.find][(m).Catalogue.find] takes it

    Returns
    -------
    :
        A copy holding only the named facets, and recording that in its
        [facets_loaded][(m).CatalogueEntry.facets_loaded]

        The columns come too, because they are the row rather than a document. An
        entry which was already built for only some facets cannot answer for more
        than it did, so the two sets are intersected rather than the new one replacing
        the old.
    """
    loaded = frozenset(needs_facets) | frozenset(DATASET_FACET_COLUMNS)
    if entry.facets_loaded is not None:
        loaded &= entry.facets_loaded

    return replace(
        entry,
        project_facets={
            name: value
            for name, value in entry.project_facets.items()
            if name in loaded
        },
        other_terms={
            name: value for name, value in entry.other_terms.items() if name in loaded
        },
        facets_loaded=loaded,
    )


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

    It has no concept of a version, so every
    [VersionFilter][(m).VersionFilter] gives the same entries back. That is the true
    answer rather than the argument being ignored: an entry here is a dataset somebody
    wrote down as present, so there is no retracted version to leave out and no newer
    one to prefer. A test which needs retraction to matter needs
    [esmporium.db.DatabaseCatalogue][], because retraction is something stored versions
    have and written-down entries do not.
    """

    # When we come back to handling VersionFilter more carefully,
    # add the concept of version to this catalogue.
    # We don't want to have to pull out an entire DatabaseCatalogue
    # to be able to test whether our logic around picking latest versions,
    # second-latest, non-retracted etc. works

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

    def find(
        self,
        query: QueryProtocol,
        *,
        versions: VersionFilter = VersionFilter.NOT_RETRACTED,
        needs_facets: Collection[str] | None = None,
    ) -> tuple[CatalogueEntry, ...]:
        """
        Find every dataset which matches a query

        Parameters
        ----------
        query
            Query to match

        versions
            Accepted, and makes no difference, for the reason given on the class: these
            entries have no versions for it to choose between.

        needs_facets
            Honoured, although there is nothing here to skip: these entries are already
            in memory, so narrowing them saves no work at all.

            It is honoured anyway because this catalogue is what everything built on
            [Catalogue][(m).Catalogue] is tested against.

            Matching happens first, against the entries as they are held, so a query
            naming a facet outside `needs_facets` still matches on it.

        Returns
        -------
        :
            Matching datasets, in the order they were given to the catalogue

            Narrowed to `needs_facets` when one was given, as `_narrowed` in this
            module describes.

        Raises
        ------
        ClashingFacetsError
            `query` sets the same facet in more than one place
        """
        asked = asked_facets(query)
        found = tuple(entry for entry in self.entries if matches_facets(asked, entry))

        if needs_facets is None:
            return found

        return tuple(_narrowed(entry, needs_facets) for entry in found)
