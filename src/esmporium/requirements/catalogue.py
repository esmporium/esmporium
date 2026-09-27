"""
What the solver needs to know about the datasets available

Throughout this package, *the solver* means the part which takes an analysis's
requirement and works out which datasets satisfy it, and which are missing.
It does not exist yet; this module is the ground it will stand on.

A [`Dataset`][esmporium.db.schema.Dataset] row carries everything we recorded when
we ingested it.
The solver needs less than that, and it needs one thing that row does not have:
somewhere to put the facets our columns do not model
(see [`DatasetRecord.extra`][(m).DatasetRecord.extra]).
[`DatasetRecord`][(m).DatasetRecord] is that read-side view of a row,
and [`Catalogue`][(m).Catalogue] is where records come from.

[`Catalogue`][(m).Catalogue] is a protocol, not a class to inherit from,
because the catalogue we actually want is backed by our database
and cannot be written until datasets record their parents and their files.
[`InMemoryCatalogue`][(m).InMemoryCatalogue] stands in until then,
so everything built on top of this can be written and tested now.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from esmporium.db.schema import DATASET_FACET_COLUMNS
from esmporium.query import (
    CANONICAL_FACETS,
    QueryCanonical,
    QueryProtocol,
    to_canonical,
)


class ClashingFacetError(ValueError):
    """Raised when a query sets the same facet in more than one place."""

    def __init__(self, facets: Iterable[str]) -> None:
        """
        Initialise the error

        Parameters
        ----------
        facets
            The facets which are set more than once
        """
        self.facets = tuple(sorted(facets))
        super().__init__(
            f"{', '.join(self.facets)} set in more than one of a query's facets, "
            "its query-specific facets and its `other_terms`. "
            "Set each facet once, so which value applies is unambiguous."
        )


class UnsupportedFacetError(ValueError):
    """Raised when a record does not know a facet it was asked about."""

    def __init__(self, facets: Iterable[str], record_id: int | None = None) -> None:
        """
        Initialise the error

        Parameters
        ----------
        facets
            The facets the record does not know

        record_id
            ID of the record which was asked, if there was one
        """
        self.facets = tuple(sorted(facets))
        self.record_id = record_id
        asked = f" (asked of record {record_id})" if record_id is not None else ""
        recorded = ", ".join(DATASET_FACET_COLUMNS)

        # An API parameter name is the one wrong name worth calling out by itself.
        # `other_terms` is sent to a search API exactly as written, prefixes and all,
        # so reaching for the same spelling here is an easy habit to fall into.
        # A `:` is tell enough, and needs no knowledge of the prefixes themselves;
        # those belong to `esmporium.search`, which this package does not import.
        prefixed = [facet for facet in self.facets if ":" in facet]
        if prefixed:
            # Deliberately no suggested replacement: dropping the prefix leaves the
            # query style's own name (`cmip6:experiment_id` -> `experiment_id`), which
            # a record does not know either, and which name to use instead depends on
            # the query style this came from. Naming the mechanism is honest; naming a
            # facet would send the reader straight into the same error again.
            api_name_msg = (
                f" {', '.join(prefixed)} looks like a search API parameter name. "
                "`other_terms` reaches a search API exactly as it is written, "
                "prefixes included, but a catalogue matches datasets "
                "we have already stored, which carry no API prefix. "
                "Name the facet as this package does (listed above), "
                "or set it on the query itself, where the query style names it."
            )
        else:
            api_name_msg = ""

        super().__init__(
            f"Cannot select datasets on {', '.join(self.facets)}{asked}: "
            f"every dataset records {recorded}. "
            "For project-specific facets (e.g. CMIP5 `product`), "
            "or facets we can search but do not store "
            "(e.g. `activity`, `realm` and `resolution`), "
            f"the catalogue has to put it in each record's `extra`.{api_name_msg}"
        )


class DuplicateRecordIDError(ValueError):
    """
    Two or more of a catalogue's records share an ID

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
            Repeated ID -> how many of the catalogue's records carry it

            The ID and nothing else, deliberately. Naming the offending records
            would mean picking the facets which say who a record is, and that has
            no answer which holds across projects: one CMIP5 `id_project_specific`
            covers several variables, so it can print twice identically, while a
            CMIP6 one already carries the variable. A label needing a label of its
            own to be useful is the wrong thing to print. The ID is what whoever
            built the catalogue wrote down, so it is what they can search for.
        """
        self.collisions = dict(sorted(collisions.items()))
        listed = "; ".join(
            f"id {record_id} is used by {count} records"
            for record_id, count in self.collisions.items()
        )
        super().__init__(
            f"A catalogue's records must have unique IDs, but {listed}. "
            "An ID identifies one dataset row, "
            "so a repeat makes two records indistinguishable. "
            "This is a mistake in whatever built the catalogue: "
            "read from the database, `id` is a primary key and cannot repeat. "
            "Two rows which describe the same data "
            "but are genuinely different datasets are a different problem, "
            "see esmporium.db.UnhandledDatasetClashError."
        )


@dataclass(frozen=True)
class DatasetRecord:
    """
    A dataset, as far as the solver is concerned

    The fields mirror the columns of [`Dataset`][esmporium.db.schema.Dataset],
    so that the two cannot drift apart unnoticed
    (there is a test which checks exactly that).
    [`DatasetFacets`][esmporium.search.DatasetFacets] is the mirror image of this
    class on the write side: parsers produce those, and this is what reading a
    stored row gives back.

    A record is compared by value but is not hashable, because `extra` is a mapping.
    Anything which needs a key should use `id`.
    """

    id: int
    """
    See [`Dataset.id`][esmporium.db.schema.Dataset.id]

    Not optional, unlike the column it mirrors:
    a record describes a row which is already in the database,
    so its ID has been assigned.
    A dataset which has not been saved yet has no record.
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
    those comparisons happen on the record rather than in the query.

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
        UnsupportedFacetError
            `name` is neither one of
            [`DATASET_FACET_COLUMNS`][esmporium.db.schema.DATASET_FACET_COLUMNS]
            nor a key of this record's `extra`
        """
        if name in DATASET_FACET_COLUMNS:
            value: str | None = getattr(self, name)
            return value

        if name in self.extra:
            return self.extra[name]

        raise UnsupportedFacetError([name], self.id)


# TODO(R2): decide whether `Leaf` and `Requirement` should refuse `other_terms`
# on their queries outright. A requirement has to be portable, hashable,
# canonical-JSON serialisable and re-solvable months later, and `other_terms` is
# deliberately none of those: it is an outbound escape hatch, written for one search
# API. `QueryFacet(None)` is the route for a facet a requirement needs. Worth
# settling at R2 rather than at R11, when `to_search_plan` has to decide what to emit.
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
    no query class names, and its keys reach a search API exactly as written.
    That makes it outbound-only in practice, so its keys here have to be facet names
    a record can answer — a canonical name, or a key of the record's `extra` — never
    an API parameter name such as `cmip6:experiment_id`.
    Note too that a facet a query class *does* name is translated while the same
    facet in `other_terms` is not, so `QueryCMIP6(table_id="Amon")` asks for
    `processing_id`, whereas `QueryCMIP6(other_terms={"table_id": ("Amon",)})` asks
    for `table_id` and finds no record which knows it.
    A facet which has to work in both directions belongs on the query class,
    annotated `QueryFacet(None)` the way CMIP5's `product` is: those are translated,
    and arrive here through `query_specific_facets`.

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
    ClashingFacetError
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
        raise ClashingFacetError(clashes)

    return {**declared, **query_specific, **other}


def _matches_facets(
    facets: Mapping[str, tuple[str, ...]], record: DatasetRecord
) -> bool:
    """
    Determine whether a record matches already-flattened facets

    Separate from [matches][(m).matches] so that
    [InMemoryCatalogue.find][(m).InMemoryCatalogue.find] can flatten a query once
    and then check every record against the result, rather than re-flattening the
    same query for each record.

    Parameters
    ----------
    facets
        Facet name -> the values which are acceptable,
        as returned by [set_facets][(m).set_facets]

    record
        Record to check

    Returns
    -------
    :
        `True` if `record` matches every facet in `facets`

    Raises
    ------
    UnsupportedFacetError
        `facets` names a facet `record` does not know
    """
    return all(record.facet(name) in values for name, values in facets.items())


def matches(query: QueryProtocol, record: DatasetRecord) -> bool:
    """
    Determine whether a dataset matches a query

    A record matches if, for every facet the query sets,
    the record's value is one of the query's values.
    Several values for one facet are therefore an "or",
    exactly as they are when searching.

    Parameters
    ----------
    query
        Query to match against

    record
        Record to check

    Returns
    -------
    :
        `True` if `record` matches `query`

    Raises
    ------
    UnsupportedFacetError
        `query` sets a facet `record` does not know, i.e. one which is neither one of
        [`DATASET_FACET_COLUMNS`][esmporium.db.schema.DATASET_FACET_COLUMNS]
        nor in the record's `extra`

    ClashingFacetError
        `query` sets the same facet in more than one place
    """
    return _matches_facets(set_facets(query), record)


class Catalogue(Protocol):
    """
    The datasets available to the solver, and what is known about them

    A protocol rather than a base class: an implementation counts because it has
    the methods below, not because it inherits from anything.
    That is what lets the catalogue we actually want — backed by our database, so
    living beside [`esmporium.db`][] — and
    [`InMemoryCatalogue`][(m).InMemoryCatalogue], which is for tests and
    prototyping, exist without either importing the other or a shared parent.
    It also means you can write your own, over an intake catalogue or a directory
    of files, and the solver will take it.
    """

    def find(self, query: QueryProtocol) -> tuple[DatasetRecord, ...]:
        """
        Find every dataset which matches a query

        Matching on a facet we have no column for already works: a query naming
        `product` or `realm` matches whenever the record's `extra` carries it
        (see [`DatasetRecord.extra`][(m).DatasetRecord.extra]).
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

    records: tuple[DatasetRecord, ...]
    """The datasets available"""

    def __post_init__(self) -> None:
        """
        Check that no two records share an ID

        Raises
        ------
        DuplicateRecordIDError
            Two or more records have the same `id`
        """
        # `Counter` rather than counting each ID against the whole list, which rescans
        # it once per record. This class stands in for a real catalogue, so it should
        # not go quadratic on the number of datasets.
        counts = Counter(record.id for record in self.records)
        # The count is the whole report. Saying *which* records collided means
        # choosing the facets which identify one, and no such choice survives moving
        # between projects (see `DuplicateRecordIDError`). A repeated ID is a mistake
        # in the code which built the catalogue, and the ID is the literal that code
        # wrote, so it is the thing to go and look for.
        collisions = {
            record_id: count for record_id, count in counts.items() if count > 1
        }
        if collisions:
            raise DuplicateRecordIDError(collisions)

    def find(self, query: QueryProtocol) -> tuple[DatasetRecord, ...]:
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
        UnsupportedFacetError
            `query` sets a facet a record does not know

        ClashingFacetError
            `query` sets the same facet in more than one place
        """
        facets = set_facets(query)

        return tuple(
            record for record in self.records if _matches_facets(facets, record)
        )
