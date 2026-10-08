"""
The datasets we have stored, as the solver sees them

[esmporium.requirements][] defines what a catalogue is
([Catalogue][esmporium.requirements.Catalogue]) and ships an in-memory one for
tests. This is the real one: it answers from the [Dataset][(m).Dataset] rows a
search saved.

It lives in `esmporium.db` rather than beside the protocol it implements, for two
reasons. `esmporium.db` is the only layer which touches the local databases directly,
and this is a `select`. And `esmporium.requirements` may not import `esmporium.db`
(see the developer note in `esmporium/requirements/__init__.py`), whereas the reverse
is free: `db` -> `requirements` -> `query` has no cycle in it. Being on this side also
means [normalise_stored_document][esmporium.search.normalise_stored_document] is in
reach, which is what lets `extra` be filled at all.

**The semantics are the in-memory catalogue's.** The facets a dataset records are
columns, so they are matched in SQL; everything else is matched in Python by
[CatalogueEntry.facet][esmporium.requirements.CatalogueEntry.facet], exactly as
[InMemoryCatalogue][esmporium.requirements.InMemoryCatalogue] does it. The SQL is an
optimisation, not a second set of rules, and
`test_solving_against_the_database_matches_the_in_memory_catalogue` pins that.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from sqlmodel import Session, col, select

from esmporium.datasets import DATASET_FACET_COLUMNS
from esmporium.db.schema import (
    Dataset,
    DatasetRawDoc,
    DatasetVersion,
    RawDocVersionLink,
)
from esmporium.query import (
    PROJECT_QUERY_MAP_DEFAULT,
    facet_spec,
)
from esmporium.requirements import (
    CatalogueEntry,
    UnrecordedFacetError,
    matches_facets,
    set_facets,
)
from esmporium.search.result_normalisation import (
    DEFAULT_NORMALISERS,
    normalise_stored_document,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy import ColumnElement, Engine

    from esmporium.query import QueryProtocol
    from esmporium.search.result_normalisation import NormaliseFunc


class Availability(str, Enum):
    """
    Which of the datasets we have stored count as available

    Every setting is "as far as we last looked": `is_latest` and `retracted` are what
    ESGF said when the search ran, not what it says now
    (see [DatasetVersion.retracted][esmporium.db.schema.DatasetVersion.retracted]).
    Re-searching updates them in place, which is what makes "this stopped being
    available since yesterday" a thing the catalogue can show.
    """

    ANY = "any"
    """
    Every dataset on record, including ones whose only versions are retracted

    Use this to ask what we have ever seen rather than what is usable.
    """

    NOT_RETRACTED = "not_retracted"
    """
    The dataset has a version which was not retracted, latest or not

    Use this to keep datasets whose newest version has not been re-searched yet.
    """

    LATEST_NOT_RETRACTED = "latest_not_retracted"
    """
    The dataset has a version which was both the latest and not retracted

    The default: it is what "I can go and use this" means.
    """


class UnknownAvailabilityError(ValueError):
    """Raised when `availability` is not one of the settings we know."""

    def __init__(self, availability: object) -> None:
        """
        Initialise the error

        Parameters
        ----------
        availability
            What was asked for
        """
        self.availability = availability
        known = ", ".join(repr(each.value) for each in Availability)
        super().__init__(
            f"{availability!r} is not an availability we know. Use one of: {known}."
        )


def facet_filters(
    facets: Mapping[str, tuple[str, ...]],
) -> list[ColumnElement[bool]]:
    """
    Turn facets into the `where` clauses which match them

    Several values for one facet are an `IN`, i.e. an "or", exactly as they are when
    searching and as [matches][esmporium.requirements.matches] treats them.

    A `NULL` column never matches: SQL's `IN` yields `NULL` for a `NULL` left-hand
    side, so a CMIP5 row (which has no `grid_label`) does not match
    `grid_label=("gn",)`. That is the same answer the in-memory catalogue gives, where
    `None not in ("gn",)`. A query can never ask *for* `NULL`, because facet values
    are strings.

    Parameters
    ----------
    facets
        Facet name -> the values which are acceptable. Every name must be one of
        [DATASET_FACET_COLUMNS][esmporium.datasets.DATASET_FACET_COLUMNS].

    Returns
    -------
    :
        One clause per facet, in the order `facets` gives them

    Raises
    ------
    UnrecordedFacetError
        `facets` names something which is not a column

        A caller mistake rather than a user one: the split between what SQL can answer
        and what has to be matched on the entry happens in
        [find_datasets][(m).find_datasets], so anything reaching here should already
        be a column.
    """
    not_columns = [name for name in facets if name not in DATASET_FACET_COLUMNS]
    if not_columns:
        raise UnrecordedFacetError(not_columns)

    return [col(getattr(Dataset, name)).in_(values) for name, values in facets.items()]


def availability_filter(availability: Availability) -> ColumnElement[bool] | None:
    """
    Get the `where` clause which keeps only the datasets an availability allows

    An `EXISTS` over [DatasetVersion][esmporium.db.schema.DatasetVersion] rather than
    a join, because a dataset has many versions and a join would return it once per
    version.

    One consequence worth knowing: under either filtering setting, a `Dataset` with no
    `DatasetVersion` rows at all is *not* available. That is the right answer --
    nothing has told us it exists anywhere -- and it cannot arise from a search, since
    [ingest_parsed_documents][esmporium.db.ingest_parsed_documents] always writes a
    version.

    Parameters
    ----------
    availability
        Which datasets to keep. See [Availability][(m).Availability].

    Returns
    -------
    :
        The clause, or `None` for [Availability.ANY][(m).Availability.ANY], which
        filters nothing

    Raises
    ------
    UnknownAvailabilityError
        `availability` is not one of the settings we know
    """
    if availability == Availability.ANY:
        return None

    if availability == Availability.NOT_RETRACTED:
        version_is_ok: ColumnElement[bool] = col(DatasetVersion.retracted).is_(False)
    elif availability == Availability.LATEST_NOT_RETRACTED:
        version_is_ok = col(DatasetVersion.retracted).is_(False) & col(
            DatasetVersion.is_latest
        ).is_(True)
    else:
        raise UnknownAvailabilityError(availability)

    return (
        select(DatasetVersion.id)
        .where(
            col(DatasetVersion.dataset_id) == col(Dataset.id),
            version_is_ok,
        )
        .exists()
    )


def _canonical_name_for(project: str) -> Mapping[str, str]:
    """
    Get the native-to-canonical facet name mapping for a project

    A flattened raw document is keyed by the facet names the API uses
    (`activity_id`, `nominal_resolution`), and an entry has to answer for the names a
    query asks with (`activity`, `resolution`). The query class a project uses
    declares exactly that mapping, so it is read off there.

    Parameters
    ----------
    project
        The project whose names to map, as [Dataset.project][(m).Dataset] records it

    Returns
    -------
    :
        Native facet name -> canonical facet name

        Empty for a project we have no query class for, in which case the native names
        are kept as they are, which is the best we can do and still better than
        nothing.
    """
    for known, query_class in PROJECT_QUERY_MAP_DEFAULT.items():
        if known.lower() == project.lower():
            return facet_spec(query_class).native_to_canonical

    return {}


def _extra_from_documents(
    project: str,
    documents: list[tuple[dict[str, Any], str]],
    normalisers: Mapping[str, NormaliseFunc],
) -> dict[str, str | None]:
    """
    Build an entry's `extra` from the raw documents describing it

    Parameters
    ----------
    project
        The dataset's project, which decides how its facet names translate

    documents
        The raw documents, as `(parsed JSON, format tag)` pairs, in the order they
        should win: the first document to record a facet is the one that answers for
        it

    normalisers
        How to flatten a document of each format

    Returns
    -------
    :
        Facet name -> value, for the facets which are not already columns

    Raises
    ------
    UnknownRawDocFormatTagError
        A document's format tag has no flattener in `normalisers`
    """
    to_canonical_name = _canonical_name_for(project)

    extra: dict[str, str | None] = {}
    for raw, tag in documents:
        flat = normalise_stored_document(raw, tag, normalisers)
        for native, value in flat.items():
            # `extra` holds single facet values. A document can carry a list (several
            # realms, say) or a nested object, and there is no one value to answer
            # with, so it is left out rather than guessed at.
            if not isinstance(value, str):
                continue

            # Recorded under the canonical name a query asks with, and under the
            # document's own spelling too, so a facet named through `other_terms`
            # exactly as the API writes it is not left unanswerable. `dict.fromkeys`
            # rather than a set, so the two are deduplicated without the order
            # becoming undefined.
            canonical = to_canonical_name.get(native, native)
            for name in dict.fromkeys((canonical, native)):
                # The columns answer for themselves, and `CatalogueEntry.facet` looks
                # there first, so repeating them here would be dead weight at best and
                # a second, disagreeing answer at worst.
                if name not in DATASET_FACET_COLUMNS and name not in extra:
                    extra[name] = value

    return extra


def _documents_by_dataset(
    session: Session, dataset_ids: list[int], availability: Availability
) -> dict[int, list[tuple[dict[str, Any], str]]]:
    """
    Get the raw documents which answer for each dataset, newest available version first

    Which version's documents answer matters, and is why this takes `availability`
    rather than just reading the newest version: with
    [Availability.ANY][(m).Availability.ANY] and only an old version on record, that
    old version's document is the one describing the dataset we just said was
    available. Reading the newest version regardless would describe a version the
    caller has excluded.

    Parameters
    ----------
    session
        Session to read with

    dataset_ids
        The datasets to get documents for

    availability
        Which versions count, matching the filter the datasets were selected with

    Returns
    -------
    :
        Dataset ID -> its documents, as `(parsed JSON, format tag)` pairs

        Ordered by version descending then document ID ascending, so the first entry
        is from the newest qualifying version and, within a version, the
        earliest-stored document. A dataset with no documents is absent.
    """
    if not dataset_ids:
        return {}

    # Only the three values we read are selected. `version` and the document's own ID
    # decide the order (below) without needing to come back with the rows.
    statement = (
        select(
            DatasetVersion.dataset_id,
            DatasetRawDoc.raw_json,
            DatasetRawDoc.raw_docs_format_tag,
        )
        .join(
            RawDocVersionLink,
            col(RawDocVersionLink.dataset_version_id) == col(DatasetVersion.id),
        )
        .join(DatasetRawDoc, col(DatasetRawDoc.id) == col(RawDocVersionLink.raw_doc_id))
        .where(col(DatasetVersion.dataset_id).in_(dataset_ids))
    )

    if availability == Availability.NOT_RETRACTED:
        statement = statement.where(col(DatasetVersion.retracted).is_(False))
    elif availability == Availability.LATEST_NOT_RETRACTED:
        statement = statement.where(
            col(DatasetVersion.retracted).is_(False),
            col(DatasetVersion.is_latest).is_(True),
        )

    # Version descending, so the newest qualifying version answers; then document ID
    # ascending, so several documents for one version (one per data node, typically)
    # resolve the same way every time. Version strings are compared as strings, which
    # is right for the dates ESGF usually publishes and is noted as unreliable for
    # CMIP5 replicas on `DatasetVersion.version` itself.
    statement = statement.order_by(
        col(DatasetVersion.version).desc(), col(DatasetRawDoc.id).asc()
    )

    found: dict[int, list[tuple[dict[str, Any], str]]] = {}
    for dataset_id, raw_json, tag in session.exec(statement):
        found.setdefault(dataset_id, []).append((json.loads(raw_json), tag))

    return found


def to_catalogue_entry(
    dataset: Dataset, extra: Mapping[str, str | None] | None = None
) -> CatalogueEntry:
    """
    Read a stored dataset row as the solver's view of it

    The write-side mirror of this is
    [DatasetFacets][esmporium.search.DatasetFacets], which is what a parser produces
    on the way in.

    Parameters
    ----------
    dataset
        The row to read

    extra
        The facets which are not columns, as
        [CatalogueEntry.extra][esmporium.requirements.CatalogueEntry.extra]
        describes them. If `None`, the entry can answer only for its columns.

    Returns
    -------
    :
        The entry

    Raises
    ------
    ValueError
        `dataset.id` is `None`, i.e. the row has not been saved

        An entry describes a dataset which is in the database, so it has an ID. A row
        which has not been flushed yet has no entry.
    """
    if dataset.id is None:
        msg = (
            "A dataset which has not been saved has no catalogue entry: "
            f"{dataset.id_project_specific!r} has no `id` yet. "
            "Flush or commit the row first."
        )
        raise ValueError(msg)

    return CatalogueEntry(
        id=dataset.id,
        id_project_specific=dataset.id_project_specific,
        **{column: getattr(dataset, column) for column in DATASET_FACET_COLUMNS},
        extra={} if extra is None else dict(extra),
    )


def find_datasets(
    session: Session,
    query: QueryProtocol,
    *,
    availability: Availability = Availability.LATEST_NOT_RETRACTED,
    normalisers: Mapping[str, NormaliseFunc] = DEFAULT_NORMALISERS,
) -> tuple[CatalogueEntry, ...]:
    """
    Find every stored dataset which matches a query

    The session-level primitive behind
    [DatabaseCatalogue.find][(m).DatabaseCatalogue], for a caller who wants one
    transaction across a whole solve rather than one per `find`.

    Parameters
    ----------
    session
        Session to read with

    query
        Query to match, in any query style

    availability
        Which datasets count as available. See [Availability][(m).Availability].

    normalisers
        How to flatten a stored raw document of each format, when filling `extra`

    Returns
    -------
    :
        Matching datasets, ordered by [Dataset.id][(m).Dataset], which is the order
        they were first saved in

    Raises
    ------
    UnrecordedFacetError
        `query` names a facet which is neither a column nor anything the datasets'
        raw documents record

    ClashingFacetsError
        `query` sets the same facet in more than one place

    UnknownAvailabilityError
        `availability` is not one of the settings we know

    UnknownRawDocFormatTagError
        A stored raw document's format tag has no flattener in `normalisers`
    """
    facets = set_facets(query)
    column_facets = {
        name: values for name, values in facets.items() if name in DATASET_FACET_COLUMNS
    }
    other_facets = {
        name: values
        for name, values in facets.items()
        if name not in DATASET_FACET_COLUMNS
    }

    statement = select(Dataset).where(*facet_filters(column_facets))

    keep_available = availability_filter(availability)
    if keep_available is not None:
        statement = statement.where(keep_available)

    # By ID, which is the order the rows were saved in, so the result is stable and
    # the ordering costs nothing (it is the primary key).
    rows = list(session.exec(statement.order_by(col(Dataset.id))))

    # `extra` is filled for every entry, not just when the query asks for one of its
    # facets, because `solve` groups and prefers on *entries*: a `group_by=("realm",)`
    # reads `realm` off each entry whether or not any query mentioned it. The cost is
    # one JSON parse per stored document per `find`.
    documents = _documents_by_dataset(
        session, [row.id for row in rows if row.id is not None], availability
    )

    entries = [
        to_catalogue_entry(
            row,
            _extra_from_documents(
                row.project, documents.get(row.id, []) if row.id else [], normalisers
            ),
        )
        for row in rows
    ]

    if not other_facets:
        return tuple(entries)

    # Matched here rather than in SQL, by the same function the in-memory catalogue
    # uses, so that a facet only a raw document knows about is answered identically
    # whichever catalogue is asked -- including the `UnrecordedFacetError` for one
    # nothing knows about.
    return tuple(entry for entry in entries if matches_facets(other_facets, entry))


@dataclass(frozen=True)
class DatabaseCatalogue:
    """
    A [Catalogue][esmporium.requirements.Catalogue] backed by our own database

    Answers from the [Dataset][(m).Dataset] rows a search saved, so
    [solve][esmporium.requirements.solve] can be run against everything found so far
    rather than against a catalogue written out by hand. Run the same requirement
    again tomorrow and the difference between the two
    [SolveResult][esmporium.requirements.SolveResult]s is what changed.
    """

    engine: Engine
    """The database to read from"""

    availability: Availability = Availability.LATEST_NOT_RETRACTED
    """
    Which datasets count as available

    See [Availability][(m).Availability]. The default is the strict one, so a dataset
    whose latest version was retracted reads as missing rather than as satisfying the
    requirement.
    """

    normalisers: Mapping[str, NormaliseFunc] = field(
        default_factory=lambda: DEFAULT_NORMALISERS
    )
    """
    How to flatten a stored raw document of each format, when filling `extra`

    The default covers the search APIs we ship. Override it if you bypassed our
    facades with a search API of your own, whose documents carry your own format tag.
    """

    def find(self, query: QueryProtocol) -> tuple[CatalogueEntry, ...]:
        """
        Find every stored dataset which matches a query

        A session per call, deliberately. A held session would hold a transaction, and
        a transaction's snapshot is the wrong thing here: a catalogue opened before a
        search ran would not see what that search just saved, which is exactly the
        question this exists to answer. Use [find_datasets][(m).find_datasets] if you
        do want one transaction across a whole solve.

        [solve][esmporium.requirements.solve] calls this once to discover the groups
        and once per leaf per group, so a large solve issues a good many identical
        queries. That is fine on SQLite and is the thing to look at first if it is
        ever not.

        Parameters
        ----------
        query
            Query to match, in any query style

        Returns
        -------
        :
            Matching datasets, in a stable order

        Raises
        ------
        UnrecordedFacetError
            `query` names a facet nothing can answer for

        ClashingFacetsError
            `query` sets the same facet in more than one place
        """
        with Session(self.engine) as session:
            return find_datasets(
                session,
                query,
                availability=self.availability,
                normalisers=self.normalisers,
            )
