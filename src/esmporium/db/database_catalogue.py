"""
The datasets we have stored, as the solver sees them

[esmporium.requirements][] defines what a catalogue is.
This identifies what datasets exist in the database which fulfil the requirement:
it answers from the [Dataset][(m).Dataset] rows a search saved.
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
    asked_facets,
    matches_facets,
)
from esmporium.search.result_normalisation import (
    DEFAULT_NORMALISERS,
    VERBATIM_NORMALISERS,
    normalise_stored_document,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy import ColumnElement, Engine

    from esmporium.query import FacetSpec, QueryProtocol
    from esmporium.search.result_normalisation import NormaliseFunc


# TODO future: these three settings are meant to become two, and this is half way
# there. The direction is to express retraction and recency where the *requirement* can
# see them rather than here: "not retracted" as a constraint, and recency as a
# preference, so that "latest", "this particular version" and "give me everything, with
# `Cardinality.ALL`" are all things a requirement says rather than settings on the
# catalogue reading it. That leaves this enum needing only "not retracted" and
# "everything", because `LATEST_NOT_RETRACTED` is exactly the part which becomes a
# preference.
#
# What has happened already: `NOT_RETRACTED` is the default, so the common path no
# longer drops a dataset whose newest version was retracted but which has a usable
# older one. What has not: `LATEST_NOT_RETRACTED` still exists, because until a
# requirement can say "prefer the latest" there would be no way left to ask "only what
# is current", and removing the member would delete the question rather than move it.
# Deleting it is the last step, not the first.
#
# Important to note:
# - The preference half needs a version on the entry first. Why that is not a small
#   change is in the note above [Catalogue][esmporium.requirements.Catalogue]: an entry
#   describes a `Dataset`, not a `DatasetVersion`, so today there is nothing for a
#   preference to range over.
#
# `test_a_retracted_dataset_drops_its_group_from_the_solve` and
# `test_a_group_whose_other_leaf_is_retracted_becomes_unsatisfied` in
# `tests/integration/requirements/test_search_to_solve_recorded.py` are what pin the
# current behaviour, and are the tests to re-read when this moves.


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

    The default, because a dataset's newest usable version beats nothing. A dataset
    whose latest version was retracted is still usable from the one before it, and the
    document which describes it here is that older version's -- so what the entry says
    matches what a caller would get.
    """

    # Note: Expected to go once recency can be said in a requirement rather than here,
    # at which point this becomes a preference -- see the note above
    # [Availability][(m).Availability].
    LATEST_NOT_RETRACTED = "latest_not_retracted"
    """
    The dataset has a version which was both the latest and not retracted

    Stricter than the default: the dataset drops out entirely unless what ESGF last
    called its newest version is also one it has not retracted. Use it to ask "only
    what is current", and note it also drops a dataset whose single live version is
    simply not flagged latest.
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


def _spec_for(
    project: str, project_query_map: Mapping[str, type[QueryProtocol]]
) -> FacetSpec | None:
    """
    Get the facet spec of the query class a project uses

    Parameters
    ----------
    project
        The project to look up, as [Dataset.project][(m).Dataset] records it

    project_query_map
        Mapping from projects to the query class which names their facets

    Returns
    -------
    :
        The spec, or `None` for a project `project_query_map` has no query class for

        `None` is not a failure. It means we model nothing about that project's facets,
        so *everything* its documents record is
        [other_terms][esmporium.requirements.CatalogueEntry.other_terms] territory.
    """
    for known, query_class in project_query_map.items():
        if known.lower() == project.lower():
            return facet_spec(query_class)

    return None


def _canonical_name_for(
    project: str, project_query_map: Mapping[str, type[QueryProtocol]]
) -> Mapping[str, str]:
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

    project_query_map
        Mapping from projects to the query class which names their facets

    Returns
    -------
    :
        Native facet name -> canonical facet name

        Empty for a project `project_query_map` has no query class for, in which case
        nothing that project records is a facet we model.
    """
    spec = _spec_for(project, project_query_map)

    return {} if spec is None else spec.native_to_canonical


# A note for developers: the merge, and what pins its answer
#
# A dataset version is published on several data nodes and indexed by several index
# nodes, and each returns its *own* document for it. In principle those documents
# describe the same thing and agree. In practice they do not: two ESGF-NG mirrors of
# one project in `tests/test-data/search/` differ by ten field names. An entry has to
# answer one string per facet, because that is what a group key and a `prefer` ranking
# compare, so the documents are merged first-writer-wins in the order
# [_documents_by_dataset][(m)._documents_by_dataset] returns them.
#
# The cost of merging rather than taking the first document whole: the entry can end up
# asserting a combination no single node published. We merge anyway, because the
# alternative loses facets which exist -- a sparser first document would make
# `group_by` on the others silently impossible -- and because "first" is arbitrary
# rather than authoritative, so there is nothing to be faithful to. Disagreement is at
# least deterministic. If that trade is ever revisited, the thing to reach for is
# surfacing the disagreement (this repo already has the notion, in
# [UnhandledDatasetClashError][esmporium.db.UnhandledDatasetClashError] and
# [facet_differences][esmporium.db.facet_differences]) rather than discarding the data.
#
# What it means for a user: the answer is **pinned early and then stable**. "First" is
# `DatasetRawDoc.id` ascending, i.e. first ingested, and a document already stored
# keeps its row (`_get_or_create_raw_doc` dedupes on `esgf_doc_id`), so the search
# which *first found a dataset* is the one whose node describes it from then on. A
# later search reaching a different node can only add facets no stored document
# carried; it cannot overwrite one already answered. So this does not drift under you
# as you work, and the only things which legitimately change an answer are a new
# version being published and `availability` changing which version qualifies.
#
# Where it does bite is **rebuilding**. Which node answered first is the order a
# search happened to return and which endpoints were reachable that day -- see
# `stop_at_first_result` in [esmporium.search.search_single_project][], and
# [build_health_selector][esmporium.db.build_health_selector], which reorders endpoints
# per run by design. So a database rebuilt from scratch can answer differently for a
# facet no column holds, and `group_by` can split the same datasets differently. That
# makes this a reproducibility property of the store: re-searching is safe, starting
# over is what to be careful about, and a scratch database, a post-migration rebuild
# and recorded test fixtures are all separate stores in this sense.
#
# One important exception to the merge, which is not a merge at all: a format whose
# document id carries the data node (Solr: `<dataset_id>|<data_node>`) stores one row
# per node, so the merge above is what happens. A format whose id does not (STAC, where
# the id is the dataset id alone) gives both index mirrors the *same* `esgf_doc_id`, so
# the second is deduped away by `_get_or_create_raw_doc` and never stored -- whichever
# mirror arrived first is the only description there will ever be, and re-searching
# cannot repair it because the dedupe key matches. For ESGF-NG the east and west
# fixtures in `tests/test-data/search/` differ by eleven property names, so this is
# real rather than theoretical. Whether that dedupe is right is an open question, not
# a settled decision.
#
# There is no sensible way around the disagreement itself on our side. The APIs should
# not disagree and we have no third source to arbitrate with; picking a winner by node,
# by `retrieved_at` or by majority vote would be a guess dressed up as a policy.
# Merging deterministically is the most we can honestly do. The disagreement is ESGF's
# to fix,
# so **if this bites you, please raise an issue at
# https://github.com/esmporium/esmporium/issues** quoting the dataset and the
# disagreeing values: we cannot ask upstream for a fix without cases.
#
# Note: It is also the
# one place a future "skip the documents when nothing needs them" filter has to
# land, which is another reason to keep it a single function rather than two.
def _facets_from_documents(
    project: str,
    documents: list[tuple[dict[str, Any], str]],
    normalisers: Mapping[str, NormaliseFunc],
    verbatim_normalisers: Mapping[str, NormaliseFunc],
    project_query_map: Mapping[str, type[QueryProtocol]],
) -> tuple[dict[str, str | None], dict[str, str | None]]:
    """
    Build an entry's two facet mappings from the raw documents describing it

    One pass per document per mapping, over documents which have already been parsed,
    because this is the hot part of a `find`: the fetch-and-parse above it is the bulk
    of the cost and this should not add another walk of the same data.

    Parameters
    ----------
    project
        The dataset's project, which decides which of its facets we model and how
        their names translate

    documents
        The raw documents, as `(parsed JSON, format tag)` pairs, in the order they
        should win: the first document to record a facet is the one that answers for
        it

    normalisers
        See [DEFAULT_NORMALISERS][esmporium.search.DEFAULT_NORMALISERS].

    verbatim_normalisers
        See [VERBATIM_NORMALISERS][esmporium.search.VERBATIM_NORMALISERS].

    project_query_map
        Mapping from projects to the query class which names their facets, used to
        translate each document's native facet names

    Returns
    -------
    :
        `(project_facets, other_terms)`, as
        [CatalogueEntry.project_facets][esmporium.requirements.CatalogueEntry.project_facets]
        and
        [CatalogueEntry.other_terms][esmporium.requirements.CatalogueEntry.other_terms]
        describe them

    Raises
    ------
    UnknownRawDocFormatTagError
        A document's format tag has no flattener in either set of normalisers
    """
    to_canonical_name = _canonical_name_for(project, project_query_map)
    spec = _spec_for(project, project_query_map)

    modelled: frozenset[str] = (
        frozenset()
        if spec is None
        else frozenset(spec.canonical_to_native) | spec.query_specific_facets
    )

    project_facets: dict[str, str | None] = {}
    other_terms: dict[str, str | None] = {}
    for raw, tag in documents:
        canonical_flat = normalise_stored_document(raw, tag, normalisers)
        # Flattened once when the two flatteners are the same function, which they are
        # for every format that does not prefix its facet names -- Solr, and so most
        # stored documents. Flattening builds a dict of every field in the document, so
        # doing it twice for the same answer is the one avoidable cost in here.
        verbatim_flat = (
            canonical_flat
            if verbatim_normalisers.get(tag) is normalisers.get(tag)
            else normalise_stored_document(raw, tag, verbatim_normalisers)
        )

        for source, destination in (
            (canonical_flat, project_facets),
            (verbatim_flat, other_terms),
        ):
            for name, value in source.items():
                # TODO future: multi-valued facets are dropped, and this is the line
                # which drops them. Both mappings hold a single string per facet,
                # because that is what a group key and a `prefer` ranking compare, so a
                # document field carrying a list of several -- or a nested object --
                # has no one value to answer with and is left out rather than guessed
                # at.
                #
                # Written as a note rather than a comment because it has a visible
                # consequence and we would rather be reminded that we foresaw it than
                # rediscover it as a bug: a genuinely multi-realm dataset
                # (`realm: ["atmos", "ocean"]`) records *no* `realm` at all, so
                # `Query(realm="atmos")` does not match it and `group_by=("realm",)`
                # cannot group it, while its single-realm sibling is fine.
                # `test_a_non_scalar_document_value_is_left_out` pins it.
                #
                # Supporting them is a feature, not a fix, and it is a bigger change
                # than it looks: a facet with several values needs deciding what a
                # group key made from it means (one group per value? one group for the
                # set?), what `matches` should do (any of them? all of them?), and
                # whether `CatalogueEntry.facet` can keep returning `str | None`. If
                # you need it, that is the shape of the issue to raise.
                if not isinstance(value, str):
                    continue

                if destination is project_facets:
                    # Canonical names only, and only facets this project models: those
                    # are the names a translated query asks with.
                    canonical = to_canonical_name.get(name, name)
                    if canonical not in modelled:
                        continue
                    name = canonical  # noqa: PLW2901 - the canonical name is the key

                # The columns answer for themselves, and `CatalogueEntry.facet` looks
                # there first, so repeating them here would be dead weight at best and
                # a second, disagreeing answer at worst. Note this excludes a *name*
                # which is a column, not a value: `variable` is dropped and
                # `variable_id` is kept, which is right -- the latter is the document's
                # own spelling and the only thing an `other_terms` lookup could use.
                if name not in DATASET_FACET_COLUMNS and name not in destination:
                    destination[name] = value

    return project_facets, other_terms


def _documents_by_dataset(
    session: Session, dataset_ids: list[int], availability: Availability
) -> dict[int, list[tuple[dict[str, Any], str]]]:
    """
    Get the raw documents which answer for each dataset, newest available version first

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
    #
    # `DatasetRawDoc.id` ascending is first-ingested, which is the order a search
    # happened to return and which endpoint was up -- arbitrary, chosen for
    # determinism within one database rather than for authority. The note above
    # [_facets_from_documents][(m)._facets_from_documents] says what follows from
    # that, and it is the thing to read before trusting a facet no column holds.
    statement = statement.order_by(
        col(DatasetVersion.version).desc(), col(DatasetRawDoc.id).asc()
    )

    found: dict[int, list[tuple[dict[str, Any], str]]] = {}
    for dataset_id, raw_json, tag in session.exec(statement):
        found.setdefault(dataset_id, []).append((json.loads(raw_json), tag))

    return found


def to_catalogue_entry(
    dataset: Dataset,
    project_facets: Mapping[str, str | None] | None = None,
    other_terms: Mapping[str, str | None] | None = None,
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

    project_facets
        The facets we model which are not columns, as
        [CatalogueEntry.project_facets][esmporium.requirements.CatalogueEntry.project_facets]
        describes them. If `None`, the entry can answer only for its columns.

    other_terms
        The facets only a search document knows, as
        [CatalogueEntry.other_terms][esmporium.requirements.CatalogueEntry.other_terms]
        describes them. If `None`, the entry answers for none of them.

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
        project_facets={} if project_facets is None else dict(project_facets),
        other_terms={} if other_terms is None else dict(other_terms),
    )


def find_datasets(  # noqa: PLR0913 - one argument per thing a find has to be told
    session: Session,
    query: QueryProtocol,
    *,
    availability: Availability = Availability.NOT_RETRACTED,
    normalisers: Mapping[str, NormaliseFunc] = DEFAULT_NORMALISERS,
    verbatim_normalisers: Mapping[str, NormaliseFunc] = VERBATIM_NORMALISERS,
    project_query_map: Mapping[str, type[QueryProtocol]] = PROJECT_QUERY_MAP_DEFAULT,
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
        See [DEFAULT_NORMALISERS][esmporium.search.DEFAULT_NORMALISERS].

    verbatim_normalisers
        See [VERBATIM_NORMALISERS][esmporium.search.VERBATIM_NORMALISERS].

    project_query_map
        Mapping from projects to the query class which names their facets, used to
        translate a stored document's native facet names into the canonical ones a
        query asks with

    Returns
    -------
    :
        Matching datasets, ordered by [Dataset.id][(m).Dataset], which is the order
        they were first saved in

    Raises
    ------
    ClashingFacetsError
        `query` sets the same facet in more than one place

    UnknownAvailabilityError
        `availability` is not one of the settings we know

    UnknownRawDocFormatTagError
        A stored raw document's format tag has no flattener in `normalisers`
    """
    asked = asked_facets(query)
    # Only modelled facets can become SQL, and only the ones which are columns. A
    # facet from `other_terms` cannot: its name is the search API's spelling, and
    # mapping that back to a column is exactly the translation `other_terms` exists to
    # avoid. So a leaf constrained *only* through `other_terms` pulls every row the
    # availability filter allows, and their documents with it, which is the expensive
    # shape to know about.
    column_facets = {
        name: values
        for name, values in asked.modelled.items()
        if name in DATASET_FACET_COLUMNS
    }
    matched_on_entry = bool(asked.literal) or any(
        name not in DATASET_FACET_COLUMNS for name in asked.modelled
    )

    statement = select(Dataset).where(*facet_filters(column_facets))

    keep_available = availability_filter(availability)
    if keep_available is not None:
        statement = statement.where(keep_available)

    # By ID, which is the order the rows were saved in, so the result is stable and
    # the ordering costs nothing (it is the primary key).
    rows = list(session.exec(statement.order_by(col(Dataset.id))))

    # Possible future TODO:
    # The cost is one fetch plus one JSON parse per stored document per `find`, and it
    # is the bulk of a `find`: measured over 20,000 datasets with one 57-key Solr
    # document each, the fetch-and-parse is ~59% of the time and flattening ~25%, so
    # skipping the documents entirely would be roughly a 6x saving. We cannot skip
    # them from here, because `Catalogue.find` is given a query and not the
    # `group_by`/`prefer` which decide what else will be read. Giving it that -- e.g.
    # `find(query, *, needs_facets=...)` derived from the requirement -- is the
    # optimisation to reach for, and it is a change to the protocol, so it is not done
    # here. Note also that `solve` calls `find` once per leaf and then once per leaf
    # per group, so the repetition matters more than the parse does.
    documents = _documents_by_dataset(
        session, [row.id for row in rows if row.id is not None], availability
    )

    entries = [
        to_catalogue_entry(
            row,
            *_facets_from_documents(
                row.project,
                documents.get(row.id, []) if row.id else [],
                normalisers,
                verbatim_normalisers,
                project_query_map,
            ),
        )
        for row in rows
    ]

    if not matched_on_entry:
        return tuple(entries)

    # Matched here rather than in SQL, by the same function the in-memory catalogue
    # uses, so that a facet only a raw document knows about is answered identically
    # whichever catalogue is asked. Nothing here raises: a facet an entry cannot answer
    # for reads as `None` and so does not match, which is what makes the answer the
    # same whether or not any row got far enough to be asked.
    return tuple(entry for entry in entries if matches_facets(asked, entry))


@dataclass(frozen=True)
class DatabaseCatalogue:
    """
    A [Catalogue][esmporium.requirements.Catalogue] backed by our own database

    Answers from the [Dataset][(m).Dataset] rows a search saved, so
    [solve][esmporium.requirements.solve] can be run against everything found so far.
    Run the same requirement
    again tomorrow and the difference between the two
    [SolveResult][esmporium.requirements.SolveResult]s is what changed.

    One limit to know before **rebuilding** a database: for a facet no column holds,
    the answer comes from whichever of a dataset's stored search documents was
    ingested first, and ESGF's index nodes do not always agree. Searching again is
    safe -- a stored document keeps its place, so an answer can be added but not
    changed -- but a store built from scratch may have a different node answering
    first, and so a different value for such a facet and a different `group_by`
    split. The disagreement is upstream of us and we have no way to arbitrate it; the
    note above `_facets_from_documents` in this module explains what we do instead
    and why. **If it bites you, please raise an issue at
    https://github.com/esmporium/esmporium/issues** quoting the dataset and the
    values, so we can take it to ESGF.
    """

    engine: Engine
    """The database to read from"""

    availability: Availability = Availability.NOT_RETRACTED
    """
    Which datasets count as available

    See [Availability][(m).Availability].
    """

    normalisers: Mapping[str, NormaliseFunc] = field(
        default_factory=lambda: DEFAULT_NORMALISERS
    )
    """
    How to flatten a stored raw document of each format, when filling `project_facets`

    See [DEFAULT_NORMALISERS][esmporium.search.DEFAULT_NORMALISERS]. The default covers
    the search APIs we ship. Override it if you bypassed our facades with a search API
    of your own, whose documents carry your own format tag.
    """

    verbatim_normalisers: Mapping[str, NormaliseFunc] = field(
        default_factory=lambda: VERBATIM_NORMALISERS
    )
    """
    The same, but keeping each document's own facet names, when filling `other_terms`

    See [VERBATIM_NORMALISERS][esmporium.search.VERBATIM_NORMALISERS]. Override this
    alongside `normalisers` for a search API of your own.
    """

    project_query_map: Mapping[str, type[QueryProtocol]] = field(
        default_factory=lambda: PROJECT_QUERY_MAP_DEFAULT
    )
    """
    Mapping from projects to the query class which names their facets

    Used to translate a stored document's native facet names (`activity_id`) into the
    canonical ones a query asks with (`activity`), when filling
    [CatalogueEntry.project_facets][esmporium.requirements.CatalogueEntry.project_facets],
    and to decide which of its facets we model at all. Override it for a project whose
    query class is your own; for a project which is not in here we model nothing, so
    everything its documents record is reachable only through `other_terms`.
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
        ClashingFacetsError
            `query` sets the same facet in more than one place
        """
        with Session(self.engine) as session:
            return find_datasets(
                session,
                query,
                availability=self.availability,
                normalisers=self.normalisers,
                verbatim_normalisers=self.verbatim_normalisers,
                project_query_map=self.project_query_map,
            )
