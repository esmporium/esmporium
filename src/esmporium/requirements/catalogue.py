"""
What selection needs to know about the datasets available

A [`Dataset`][esmporium.db.schema.Dataset] row carries everything we recorded when
we ingested it.
Selection needs less than that, and it needs one thing that row does not have:
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
        super().__init__(
            f"Cannot select datasets on {', '.join(self.facets)}{asked}: "
            f"every dataset records {recorded}. "
            "Any other facet is fine, whether it is project-specific "
            "(CMIP5's `product`, say) or one we can search but do not store "
            "(`activity`, `realm` and `resolution`), "
            "but the catalogue has to put it in each record's `extra`."
        )


@dataclass(frozen=True)
class DatasetRecord:
    """
    A dataset, as far as selection is concerned

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

    Carried because dataset identity is every facet below *plus* this
    (see [`Dataset`][esmporium.db.schema.Dataset]'s identity index),
    so two records can agree on every facet and still be different datasets.
    CMIP5 is where this happens: the same variable is published under
    `cmip5.output1.…` and `cmip5.output2.…`, and `product` is not a column,
    so this is the only thing we record which tells the two apart.
    Without it, "two datasets matched and we cannot choose" would be
    impossible to explain to whoever has to resolve it.
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
    its own business. What selection needs is this: anything it should be able to
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
    The datasets available to selection, and what is known about them

    This grows alongside what is built on top of it, so it is deliberately small
    for now: `find` is the only thing selection can do with a catalogue today.
    Walking a dataset's parents, reading its metadata and following its links to
    other datasets are all part of the eventual protocol
    (the *Expressing analysis data requirements* design note has the whole of it),
    and each arrives with the first thing which needs it.
    """

    def find(self, query: QueryProtocol) -> tuple[DatasetRecord, ...]:
        """
        Find every dataset which matches a query

        How a query naming a facet we have no column for is answered is up to the
        catalogue: it might read the raw search documents, or a project-specific
        table, or simply know. Facets the catalogue wants selection to use
        afterwards belong in each record's `extra`.

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
        ValueError
            Two or more records have the same `id`
        """
        ids = [record.id for record in self.records]
        duplicated = sorted(
            {record_id for record_id in ids if ids.count(record_id) > 1}
        )
        if duplicated:
            msg = (
                f"Record IDs must be unique; these are repeated: {duplicated}. "
                "An ID identifies one dataset, "
                "so a repeat would make two datasets indistinguishable."
            )
            raise ValueError(msg)

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
