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

from esmporium.db.schema import DATASET_FACET_COLUMNS
from esmporium.query import (
    CANONICAL_FACETS,
    ClashingFacetsError,
    QueryCanonical,
    QueryProtocol,
    to_canonical,
)


# A note for developers:
# This looks like a duplicate of
# [`UnaskableFacetError`][esmporium.search.UnaskableFacetError] and is not one.
# That error is an `AssertionError`: reaching it means a request was
# built naming a facet a query style has no parameter for, i.e. a guard was bypassed
# and the bug is ours. This one is a `ValueError`, because asking is reasonable and
# the answer is simply no: `Query(realm="atmos")` is a fair question that a catalogue
# which did not fill `extra` cannot answer.
#
# The two also describe different things. `UnaskableFacetError` is about *names*,
# whether a query style has a parameter for a facet. This is about *recorded values*,
# whether one stored dataset knows a facet -- hence "unrecorded". Search has no
# equivalent, because it asks an API and the API either knows the parameter or errors;
# "what does this stored row know?" only exists on this side of the boundary.
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
            "the catalogue has to put it in each entry's `extra`."
        )


class DuplicateEntryIDError(ValueError):
    """
    Two or more of a catalogue's entries share an ID

    Not to be confused with
    [`UnhandledDatasetClashError`][esmporium.db.UnhandledDatasetClashError],
    which is about the *data*: two rows our dataset model cannot tell apart, where
    the fix is to work out which facet we are missing.
    This is about the *code*: an ID identifies one row, so a catalogue which repeats
    one has been built wrongly.
    It cannot arise from the database, where `id` is
    [`Dataset.id`][esmporium.db.schema.Dataset.id], a primary key.
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

    The fields mirror the columns of [`Dataset`][esmporium.db.schema.Dataset],
    so that the two cannot drift apart unnoticed.
    [`DatasetFacets`][esmporium.search.DatasetFacets] is the mirror image of this
    class on the write side: parsers produce those, and this is what reading a
    stored row gives back.

    An entry is compared by value but is not hashable, because `extra` is a mapping.
    Anything which needs a key should use `id`.
    """

    id: int
    """
    See [`Dataset.id`][esmporium.db.schema.Dataset.id]

    Not optional, unlike the column it mirrors:
    an entry describes a row which is already in the database,
    so its ID has been assigned.
    A dataset which has not been saved yet has no entry.
    """

    id_project_specific: str
    """
    See
    [`Dataset.id_project_specific`][esmporium.db.schema.Dataset.id_project_specific]
    """

    project: str
    """See [`Dataset.project`][esmporium.db.schema.Dataset.project]."""

    model: str
    """See [`Dataset.model`][esmporium.db.schema.Dataset.model]."""

    institution: str
    """See [`Dataset.institution`][esmporium.db.schema.Dataset.institution]."""

    experiment: str
    """See [`Dataset.experiment`][esmporium.db.schema.Dataset.experiment]."""

    variant_label: str
    """See [`Dataset.variant_label`][esmporium.db.schema.Dataset.variant_label]."""

    variable: str
    """See [`Dataset.variable`][esmporium.db.schema.Dataset.variable]."""

    reporting_interval: str
    """See [`Dataset.reporting_interval`][esmporium.db.schema.Dataset.reporting_interval]."""  # noqa: E501

    grid_label: str | None
    """
    See [`Dataset.grid_label`][esmporium.db.schema.Dataset.grid_label]

    `None` for a project with no concept of a grid (CMIP5), exactly as the column is.
    A query can therefore never match this facet for such a dataset:
    facet values are strings, and this one has no string value.
    """

    processing_id: str
    """See [`Dataset.processing_id`][esmporium.db.schema.Dataset.processing_id]."""

    extra: Mapping[str, str | None] = field(default_factory=dict)
    """
    Facets beyond the ones every dataset records

    How a catalogue answers a query which names a facet we have no column for is
    its own business. What the solver needs is this: anything it should be able to
    group by, prefer on, or match auxiliary data on has to appear here, because
    those comparisons happen on the entry rather than in the query.

    Two kinds of facet live here.
    Project-specific ones, which have no canonical name at all
    (CMIP5's `product`, CMIP6's `sub_experiment_id`).
    And `activity`, `realm` and `resolution`, which *are* canonical facets
    — [`Query`][esmporium.query.Query] can ask for them and the search APIs
    answer — but which [`Dataset`][esmporium.db.schema.Dataset] has no column for,
    so they are not in
    [`DATASET_FACET_COLUMNS`][esmporium.db.schema.DATASET_FACET_COLUMNS].
    """

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
            The facet's value

        Raises
        ------
        UnrecordedFacetError
            `name` is neither one of
            [`DATASET_FACET_COLUMNS`][esmporium.db.schema.DATASET_FACET_COLUMNS]
            nor a key of this entry's `extra`
        """
        if name in DATASET_FACET_COLUMNS:
            value: str | None = getattr(self, name)
            return value

        if name in self.extra:
            return self.extra[name]

        raise UnrecordedFacetError([name], self.id)


# A note for developers:
# `other_terms` is an escape hatch for facet names we do not translate or model,
# and it is a short-term one. The difficulty is that it carries two incompatible
# contracts in one field. Outbound, its keys reach a search API exactly as written,
# prefixes included, so `cmip6:experiment_id` is correct for one STAC endpoint and
# wrong everywhere else. Inbound, a catalogue needs a facet name an entry can
# answer, which is `experiment`. No single spelling satisfies both, which is why
# `other_terms` is hard to handle well anywhere in the stack.
#
# For anything longer-lived than a one-off, declare the facet on a query class of
# your own instead: annotate it `QueryFacet(None)` the way CMIP5's `product` is,
# and the facade does the prefixing while `to_canonical` does the translating.
# See [`QueryFacet`][esmporium.query.QueryFacet] and
# [`esmporium.query.known_queries`][].
def set_facets(query: QueryProtocol) -> dict[str, tuple[str, ...]]:
    """
    Get the facets a query actually constrains, under canonical names, flattened

    A query holds its facets in three places, and all three are facets:
    the fields it declares, the facets it names which have no canonical
    equivalent, and `other_terms`, which is esmporium's escape hatch for facets no
    query class names. This flattens all three into one mapping, so that matching
    has a single thing to loop over.

    `query` may be written in any query style
    ([`QueryCMIP6`][esmporium.query.QueryCMIP6], say):
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

    return {**declared, **query_specific, **other}


def _matches_facets(
    facets: Mapping[str, tuple[str, ...]], entry: CatalogueEntry
) -> bool:
    """
    Determine whether an entry matches already-flattened facets

    Separate from [matches][(m).matches] so that
    [InMemoryCatalogue.find][(m).InMemoryCatalogue.find] can flatten a query once
    and then check every entry against the result, rather than re-flattening the
    same query for each entry.

    Parameters
    ----------
    facets
        Facet name -> the values which are acceptable,
        as returned by [set_facets][(m).set_facets]

    entry
        Entry to check

    Returns
    -------
    :
        `True` if `entry` matches every facet in `facets`

    Raises
    ------
    UnrecordedFacetError
        `facets` names a facet `entry` does not know
    """
    return all(entry.facet(name) in values for name, values in facets.items())


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
    whole catalogue. See the note above [set_facets][(m).set_facets].

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
    UnrecordedFacetError
        `query` sets a facet `entry` does not know, i.e. one which is neither one of
        [`DATASET_FACET_COLUMNS`][esmporium.db.schema.DATASET_FACET_COLUMNS]
        nor in the entry's `extra`

    ClashingFacetsError
        `query` sets the same facet in more than one place
    """
    return _matches_facets(set_facets(query), entry)


class Catalogue(Protocol):
    """
    The datasets available to the solver, and what is known about them
    """

    def find(self, query: QueryProtocol) -> tuple[CatalogueEntry, ...]:
        """
        Find every dataset which matches a query

        Note for implementers: Matching on a facet we have no column for is essential.
        For example, a query naming
        `product` or `realm` must match whenever the entry's `extra` carries it
        (see [`CatalogueEntry.extra`][(m).CatalogueEntry.extra]).
        What is up to each catalogue is where `extra` comes from — one backed by our
        database would read it out of the raw search documents, another might consult
        a project-specific table, or simply know.
        So anything the solver should be able to group by, prefer on or match
        auxiliary data on has to be in `extra` by the time `find` returns.

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

    Intended for tests and for prototyping.
    The catalogue we want is backed by our database;
    this one lets everything built on top of
    [`Catalogue`][(m).Catalogue] be written and tested without one.
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
        UnrecordedFacetError
            `query` sets a facet an entry does not know

        ClashingFacetsError
            `query` sets the same facet in more than one place
        """
        facets = set_facets(query)

        return tuple(entry for entry in self.entries if _matches_facets(facets, entry))
