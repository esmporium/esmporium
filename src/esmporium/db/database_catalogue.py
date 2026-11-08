"""
The datasets we have stored, as the solver sees them

[esmporium.requirements][] defines what a catalogue is.
This identifies what datasets exist in the database which fulfil the requirement:
it answers from the [Dataset][(m).Dataset] rows a search saved.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlmodel import Session, col, select

from esmporium.datasets import DATASET_FACET_COLUMNS
from esmporium.db.parents_to_database import SOURCE_PRECEDENCE, preferred_parent_info
from esmporium.db.schema import (
    Dataset,
    DatasetParentLink,
    DatasetRawDoc,
    DatasetVersion,
    DatasetVersionParent,
    ParentInfoSource,
    ParentResolution,
    ParentVersionChoice,
    RawDocVersionLink,
)
from esmporium.query import (
    PROJECT_QUERY_MAP_DEFAULT,
    facet_spec,
)
from esmporium.requirements import (
    CatalogueEntry,
    ParentLookup,
    ParentStatus,
    UnrecordedFacetError,
    VersionFilter,
    asked_facets,
    matches_facets,
)
from esmporium.search.result_normalisation import (
    DEFAULT_NORMALISERS,
    VERBATIM_NORMALISERS,
    normalise_stored_document,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from sqlalchemy import ColumnElement, Engine

    from esmporium.query import FacetSpec, QueryProtocol
    from esmporium.search.result_normalisation import NormaliseFunc


class UnknownVersionFilterError(ValueError):
    """Raised when `versions` is not one of the version filters we know."""

    def __init__(self, versions: object) -> None:
        """
        Initialise the error

        Parameters
        ----------
        versions
            What was asked for
        """
        self.versions = versions
        known = ", ".join(repr(each.value) for each in VersionFilter)
        super().__init__(
            f"{versions!r} is not a version filter we know. Use one of: {known}."
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


def version_filter(versions: VersionFilter) -> ColumnElement[bool] | None:
    """
    Get the `where` clause which keeps only the datasets a version filter allows

    An `EXISTS` over [DatasetVersion][esmporium.db.schema.DatasetVersion] rather than
    a join, because a dataset has many versions and a join would return it once per
    version.

    Parameters
    ----------
    versions
        Which datasets to keep. See [VersionFilter][(m).VersionFilter].

    Returns
    -------
    :
        The clause, or `None` for
        [VersionFilter.ANY][esmporium.requirements.VersionFilter], which
        filters nothing

    Raises
    ------
    UnknownVersionFilterError
        `versions` is not one of the settings we know
    """
    version_is_ok = version_clause(versions)
    if version_is_ok is None:
        return None

    return (
        select(DatasetVersion.id)
        .where(
            col(DatasetVersion.dataset_id) == col(Dataset.id),
            version_is_ok,
        )
        .exists()
    )


def version_clause(versions: VersionFilter) -> ColumnElement[bool] | None:
    """
    Get the `where` clause which keeps only the versions a version filter allows

    About [DatasetVersion][esmporium.db.schema.DatasetVersion] rows, where
    [version_filter][(m).version_filter] is about the datasets which have one. Three
    readers need the same answer -- which datasets to return, which version's documents
    describe them, and which version's parent to follow -- so the rule is written once
    and they cannot disagree about what a retracted release means.

    Parameters
    ----------
    versions
        Which versions to keep. See
        [VersionFilter][esmporium.requirements.VersionFilter].

    Returns
    -------
    :
        The clause, or `None` for
        [VersionFilter.ANY][esmporium.requirements.VersionFilter], which filters nothing

    Raises
    ------
    UnknownVersionFilterError
        `versions` is not one of the settings we know
    """
    if versions == VersionFilter.ANY:
        return None

    if versions == VersionFilter.NOT_RETRACTED:
        return col(DatasetVersion.retracted).is_(False)

    if versions == VersionFilter.LATEST_NOT_RETRACTED:
        return col(DatasetVersion.retracted).is_(False) & col(
            DatasetVersion.is_latest
        ).is_(True)

    raise UnknownVersionFilterError(versions)


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
# version being published and `versions` changing which version qualifies.
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
# cannot repair it because the dedupe key matches. For ESGF-NG CMIP6, the east and
# west fixtures in `tests/test-data/search/` differ by eleven property names, so this
# is real rather than theoretical. (As of 2026-10-15, this only applies to CMIP6:
# the CMIP7 documents agree on their facets.) Whether that dedupe is right is an open
# question, not a settled decision.
#
# There is no sensible way around the disagreement itself on our side. The APIs should
# not disagree and we have no third source to arbitrate with; picking a winner by node,
# by `retrieved_at` or by majority vote would be a guess dressed up as a policy.
# Merging deterministically is the most we can honestly do. The disagreement is ESGF's
# to fix,
# so **if this bites you, please raise an issue at
# https://github.com/esmporium/esmporium/issues** quoting the dataset and the
# disagreeing values: we cannot ask upstream for a fix without cases.
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
    session: Session, dataset_ids: list[int], versions: VersionFilter
) -> dict[int, list[tuple[dict[str, Any], str]]]:
    """
    Get the raw documents which answer for each dataset, newest available version first

    Parameters
    ----------
    session
        Session to read with

    dataset_ids
        The datasets to get documents for

    versions
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

    version_is_ok = version_clause(versions)
    if version_is_ok is not None:
        statement = statement.where(version_is_ok)

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
    facets_loaded: frozenset[str] | None = None,
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

    facets_loaded
        Which facets this entry was built to answer for, as
        [CatalogueEntry.facets_loaded][esmporium.requirements.CatalogueEntry.facets_loaded]
        describes it. `None`, the default, is an entry built from everything there was
        to read, so it answers `None` for a facet it has no value for rather than
        refusing the question.

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
        facets_loaded=facets_loaded,
    )


def _entries_from_rows(  # noqa: PLR0913 - one argument per thing reading a row needs
    session: Session,
    rows: Sequence[Dataset],
    *,
    versions: VersionFilter,
    needs_facets: Collection[str] | None,
    matched_on_entry: bool,
    normalisers: Mapping[str, NormaliseFunc],
    verbatim_normalisers: Mapping[str, NormaliseFunc],
    project_query_map: Mapping[str, type[QueryProtocol]],
) -> tuple[CatalogueEntry, ...]:
    """
    Turn stored rows into entries, reading their documents only if anybody will ask

    Shared by every lookup which hands rows back, so that a dataset describes itself
    the same way however it was reached -- found by a query, or arrived at by following
    a parent link.

    Parameters
    ----------
    session
        Session to read with

    rows
        The datasets to describe

    versions
        Which versions count, matching the filter the rows were selected with

    needs_facets
        The facets the caller will ask the entries about, or `None` for all of them

    matched_on_entry
        Whether the caller still has matching to do against the entries

        Forces the documents to be read even when `needs_facets` would not, because a
        facet which is about to be matched on has to be there to match.

    normalisers
        How to flatten a stored raw document of each format

    verbatim_normalisers
        The same, keeping each document's own facet names

    project_query_map
        Mapping from projects to the query class which names their facets

    Returns
    -------
    :
        One entry per row, in the order given

    Raises
    ------
    UnknownRawDocFormatTagError
        A stored raw document's format tag has no flattener in `normalisers`
    """
    # `None` means the caller did not say what they would read, so everything is read
    # and the entry answers for everything it could know.
    facets_loaded = (
        None
        if needs_facets is None
        else frozenset(needs_facets) | frozenset(DATASET_FACET_COLUMNS)
    )

    documents_wanted = (
        facets_loaded is None
        or matched_on_entry
        or bool(facets_loaded - frozenset(DATASET_FACET_COLUMNS))
    )

    if not documents_wanted:
        return tuple(
            to_catalogue_entry(row, facets_loaded=facets_loaded) for row in rows
        )

    documents = _documents_by_dataset(
        session, [row.id for row in rows if row.id is not None], versions
    )

    return tuple(
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
    )


def find_datasets(  # noqa: PLR0913 - one argument per thing a find has to be told
    session: Session,
    query: QueryProtocol,
    *,
    versions: VersionFilter = VersionFilter.NOT_RETRACTED,
    needs_facets: Collection[str] | None = None,
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

    versions
        Which of a dataset's versions count. See [VersionFilter][(m).VersionFilter].

    needs_facets
        The facets the caller will ask the entries about. See
        [Catalogue.find][esmporium.requirements.Catalogue.find], which this implements.

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

        Each entry answers for `needs_facets` and for its columns; see
        [CatalogueEntry.facets_loaded][esmporium.requirements.CatalogueEntry.facets_loaded]
        for what it does when asked about anything else.

    Raises
    ------
    ClashingFacetsError
        `query` sets the same facet in more than one place

    UnknownVersionFilterError
        `versions` is not one of the settings we know

    UnknownRawDocFormatTagError
        A stored raw document's format tag has no flattener in `normalisers`
    """
    asked = asked_facets(query)
    # Only modelled facets can become SQL, and only the ones which are columns. A
    # facet from `other_terms` cannot: its name is the search API's spelling, and
    # mapping that back to a column is exactly the translation `other_terms` exists to
    # avoid. So a leaf constrained *only* through `other_terms` pulls every row the
    # versions filter allows, and their documents with it, which is the expensive
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

    keep_available = version_filter(versions)
    if keep_available is not None:
        statement = statement.where(keep_available)

    # By ID, which is the order the rows were saved in, so the result is stable and
    # the ordering costs nothing (it is the primary key).
    rows = list(session.exec(statement.order_by(col(Dataset.id))))

    entries = list(
        _entries_from_rows(
            session,
            rows,
            versions=versions,
            needs_facets=needs_facets,
            matched_on_entry=matched_on_entry,
            normalisers=normalisers,
            verbatim_normalisers=verbatim_normalisers,
            project_query_map=project_query_map,
        )
    )

    if not matched_on_entry:
        return tuple(entries)

    # Matched here rather than in SQL, by the same function the in-memory catalogue
    # uses, so that a facet only a raw document knows about is answered identically
    # whichever catalogue is asked. Nothing here raises: a facet an entry cannot answer
    # for reads as `None` and so does not match, which is what makes the answer the
    # same whether or not any row got far enough to be asked.
    return tuple(entry for entry in entries if matches_facets(asked, entry))


PARENT_STATUS_BY_RESOLUTION: Mapping[ParentResolution, ParentStatus] = {
    ParentResolution.RESOLVED: ParentStatus.FOUND,
    ParentResolution.NO_PARENT_DECLARED: ParentStatus.ROOT,
    ParentResolution.UNRESOLVED: ParentStatus.UNRESOLVED,
    ParentResolution.NOT_ON_ESGF: ParentStatus.PARENT_NOT_FOUND,
    ParentResolution.VARIABLE_NOT_PUBLISHED: ParentStatus.VARIABLE_MISSING,
}
"""
How a stored parent row's state reads to a requirement

Two vocabularies for one idea, kept apart because
[ParentResolution][esmporium.db.schema.ParentResolution] is a *stored* enum -- its
members are strings in the database, and a requirement must not be coupled to them --
and because the requirements side needs two answers this one cannot give. Neither
[NOT_LOOKED_FOR][esmporium.requirements.ParentStatus.NOT_LOOKED_FOR] nor
[NO_USABLE_VERSION][esmporium.requirements.ParentStatus.NO_USABLE_VERSION] is in here:
the first is the absence of a row and so has nothing to map from, and the second is
about the lookup's version filter rather than about what was recorded.

Every member of `ParentResolution` has an entry, which
`test_every_parent_resolution_reads_as_a_status` holds it to -- a resolution with no
entry here would make a stored state invisible to every requirement.
"""

_SOURCE_PHRASES: Mapping[ParentInfoSource, str] = {
    ParentInfoSource.SEARCH_METADATA: "parent published in the search index",
    ParentInfoSource.FILE_HEADER: "parent read from a file header",
    ParentInfoSource.FIX: "parent supplied by a fix of ours",
}
"""
How to describe where a parent fact came from, for a person reading an explanation
"""

_VERSION_CHOICE_PHRASES: Mapping[ParentVersionChoice, str] = {
    ParentVersionChoice.ONLY_VERSION: "",
    ParentVersionChoice.CHILD_VERSION: (
        "parent release matched the child's version string"
    ),
    ParentVersionChoice.DEFAULTED: "parent release defaulted to the newest we hold",
    ParentVersionChoice.CHOSEN: "parent release picked by a version chooser",
}
"""
How to describe which release of the parent was used, for a person reading

[ONLY_VERSION][esmporium.db.schema.ParentVersionChoice.ONLY_VERSION] maps to nothing to
say, deliberately: we hold one release, so nothing was decided and a phrase would imply
otherwise. The other three were all decisions of some kind, and which kind is worth
seeing -- a chain which rests on
[DEFAULTED][esmporium.db.schema.ParentVersionChoice.DEFAULTED] rests on a rule of ours.
"""


class ParentLinkMissingError(RuntimeError):
    """
    A parent row says it was resolved and no link records what to

    Ours to fix rather than the user's to work around: the resolution and the link are
    written together, and a change to the facts deletes the link and puts the row back
    to [UNRESOLVED][esmporium.db.schema.ParentResolution.UNRESOLVED], so the two cannot
    legitimately disagree.
    """

    def __init__(self, parent_info_id: int | None, dataset_version_id: int) -> None:
        """
        Initialise the error

        Parameters
        ----------
        parent_info_id
            ID of the parent row which says it resolved

        dataset_version_id
            ID of the dataset version it belongs to
        """
        self.parent_info_id = parent_info_id
        self.dataset_version_id = dataset_version_id
        super().__init__(
            f"Parent row {parent_info_id} (for dataset version {dataset_version_id}) "
            "is marked resolved, but no link records which dataset it resolved to. "
            "These are written together and cannot disagree, so this is a bug in "
            "esmporium rather than something to work around. Please raise an issue at "
            "https://github.com/esmporium/esmporium/issues quoting those two IDs."
        )


def describing_version(
    session: Session, dataset_id: int, versions: VersionFilter
) -> DatasetVersion | None:
    """
    Get the version of a dataset which speaks for it

    The newest release which passes the filter, which is the same release whose
    documents answer for a dataset's facets (`_documents_by_dataset` in this module
    picks them the same way). That is the point of it being one function: what an entry
    *says* and what its parent *is* come from one release, so the two cannot describe
    different publications of the same dataset.

    Parameters
    ----------
    session
        Session to read with

    dataset_id
        The dataset

    versions
        Which versions count. See
        [VersionFilter][esmporium.requirements.VersionFilter].

    Returns
    -------
    :
        The version, or `None` if the dataset has none which passes the filter

    Raises
    ------
    UnknownVersionFilterError
        `versions` is not one of the settings we know
    """
    statement = select(DatasetVersion).where(DatasetVersion.dataset_id == dataset_id)

    version_is_ok = version_clause(versions)
    if version_is_ok is not None:
        statement = statement.where(version_is_ok)

    # Version descending for the same reason, and compared the same way (as strings),
    # as in `_documents_by_dataset`; then ID ascending so that two releases which
    # somehow carry one version string resolve the same way every time.
    return session.exec(
        statement.order_by(
            col(DatasetVersion.version).desc(), col(DatasetVersion.id).asc()
        )
    ).first()


def _parent_provenance(
    info: DatasetVersionParent, link: DatasetParentLink | None
) -> str:
    """
    Describe how a parent came to be this dataset's parent

    Parameters
    ----------
    info
        The parent row which was believed

    link
        The link it resolved to, if it resolved

    Returns
    -------
    :
        A phrase for a person to read, or an empty string if there is nothing to add
    """
    said = [_SOURCE_PHRASES.get(info.source, f"parent from {info.source!r}")]
    if link is not None:
        said.append(
            _VERSION_CHOICE_PHRASES.get(
                link.version_choice, f"release {link.version_choice!r}"
            )
        )

    return "; ".join(phrase for phrase in said if phrase)


def parent_of_dataset(  # noqa: PLR0913 - one argument per thing reading a row needs
    session: Session,
    entry: CatalogueEntry,
    *,
    versions: VersionFilter = VersionFilter.NOT_RETRACTED,
    needs_facets: Collection[str] | None = None,
    normalisers: Mapping[str, NormaliseFunc] = DEFAULT_NORMALISERS,
    verbatim_normalisers: Mapping[str, NormaliseFunc] = VERBATIM_NORMALISERS,
    project_query_map: Mapping[str, type[QueryProtocol]] = PROJECT_QUERY_MAP_DEFAULT,
) -> ParentLookup:
    """
    Get the dataset a stored dataset branched from

    The session-level primitive behind
    [DatabaseCatalogue.parent_of][(m).DatabaseCatalogue.parent_of], for a caller who
    wants one transaction across a whole solve.

    Three things are *not* decided here, because each is already decided elsewhere and
    a second opinion would be a second answer:

    - **which release of the parent to use.** That is recorded on the link, along with
      how it was picked
      ([DatasetParentLink.version_choice][esmporium.db.schema.DatasetParentLink]).
    - **which source to believe**, when a version carries rows from several.
      [preferred_parent_info][esmporium.db.preferred_parent_info] and
      [SOURCE_PRECEDENCE][esmporium.db.SOURCE_PRECEDENCE] say.
    - **which release of the child to ask.** `versions` already says, through
      [describing_version][(m).describing_version].

    That last one is a choice in principle and almost never one in practice. Parent
    facts belong to a simulation rather than to one of its releases:
    [record_header_parent_facts][esmporium.db.record_header_parent_facts] writes what
    one header said against every version it answers for, and
    [copy_parent_facts][esmporium.db.copy_parent_facts] carries it to the simulation's
    other variables. So a dataset's releases agree about their parent unless a
    republication corrected it, which nothing we do can currently produce.

    Parameters
    ----------
    session
        Session to read with

    entry
        Dataset whose parent is wanted. Only its `id` is read.

    versions
        Which versions count, on both ends of the link. See
        [VersionFilter][esmporium.requirements.VersionFilter].

    needs_facets
        The facets the caller will ask the parent about

    normalisers
        How to flatten a stored raw document of each format

    verbatim_normalisers
        The same, keeping each document's own facet names

    project_query_map
        Mapping from projects to the query class which names their facets

    Returns
    -------
    :
        The parent, or which situation was met. See
        [ParentStatus][esmporium.requirements.ParentStatus].

    Raises
    ------
    ParentLinkMissingError
        A parent row is marked resolved and no link records what to

    UnknownVersionFilterError
        `versions` is not one of the settings we know
    """
    child_version = describing_version(session, entry.id, versions)
    if child_version is None or child_version.id is None:
        # No release of the child qualifies, so there is no release whose parent to
        # ask about. Reported as the question not having been asked rather than as an
        # absence in the data, which is what it is: a different `versions` may well
        # find one.
        return ParentLookup(ParentStatus.NOT_LOOKED_FOR)

    rows = list(
        session.exec(
            select(DatasetVersionParent).where(
                col(DatasetVersionParent.dataset_version_id) == child_version.id
            )
        )
    )
    preferred = preferred_parent_info(rows, SOURCE_PRECEDENCE)
    if preferred is None:
        return ParentLookup(ParentStatus.NOT_LOOKED_FOR)

    # `UNRESOLVED` for a resolution this version does not know about -- a database
    # written by a later one, say. Of the six answers it is the only one which invites
    # looking again rather than closing the question, which is the right thing to say
    # about a state we cannot read.
    status = PARENT_STATUS_BY_RESOLUTION.get(
        preferred.resolution, ParentStatus.UNRESOLVED
    )
    if status is not ParentStatus.FOUND:
        return ParentLookup(status, provenance=_parent_provenance(preferred, None))

    # Found by the preferred row's own ID rather than by `child_dataset_version_id`,
    # which is **not** unique: a fix arriving after a header row was already linked
    # leaves the older link in place beside the new one. Selecting on the child version
    # would pick between them arbitrarily, which would quietly undo the precedence just
    # applied.
    link = session.exec(
        select(DatasetParentLink).where(
            DatasetParentLink.parent_info_id == preferred.id
        )
    ).first()
    if link is None:
        raise ParentLinkMissingError(preferred.id, child_version.id)

    # The same version filter the child was asked under, so one solve is answered from
    # one set of versions at both ends of every link.
    statement = select(Dataset).where(col(Dataset.id) == link.parent_dataset_id)
    keep_available = version_filter(versions)
    if keep_available is not None:
        statement = statement.where(keep_available)

    parent_rows = list(session.exec(statement))
    if not parent_rows:
        return ParentLookup(
            ParentStatus.NO_USABLE_VERSION,
            provenance=_parent_provenance(preferred, link),
        )

    (parent,) = _entries_from_rows(
        session,
        parent_rows,
        versions=versions,
        needs_facets=needs_facets,
        matched_on_entry=False,
        normalisers=normalisers,
        verbatim_normalisers=verbatim_normalisers,
        project_query_map=project_query_map,
    )

    return ParentLookup(
        ParentStatus.FOUND,
        parent=parent,
        provenance=_parent_provenance(preferred, link),
    )


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

    def find(
        self,
        query: QueryProtocol,
        *,
        versions: VersionFilter = VersionFilter.NOT_RETRACTED,
        needs_facets: Collection[str] | None = None,
    ) -> tuple[CatalogueEntry, ...]:
        """
        Find every stored dataset which matches a query

        A session per call, deliberately. A held session would hold a transaction, and
        a transaction's snapshot is the wrong thing here: a catalogue opened before a
        search ran would not see what that search just saved, which is exactly the
        question this exists to answer. Use [find_datasets][(m).find_datasets] if you
        do want one transaction across a whole solve.

        [solve][esmporium.requirements.solve] calls this once per distinct question a
        requirement asks, so one solve reads this database once per leaf however many
        groups it finds, and sees one state of it throughout.

        Parameters
        ----------
        query
            Query to match, in any query style

        versions
            Which of a dataset's versions count.

        needs_facets
            The facets the caller will ask the entries about. See
            [Catalogue.find][esmporium.requirements.Catalogue.find].

            Naming nothing but columns is what makes a lookup cheap here: the stored
            search documents are not opened at all.

        Returns
        -------
        :
            Matching datasets, in a stable order

        Raises
        ------
        ClashingFacetsError
            `query` sets the same facet in more than one place

        UnknownVersionFilterError
            `versions` is not one of the version filters we know
        """
        with Session(self.engine) as session:
            return find_datasets(
                session,
                query,
                versions=versions,
                needs_facets=needs_facets,
                normalisers=self.normalisers,
                verbatim_normalisers=self.verbatim_normalisers,
                project_query_map=self.project_query_map,
            )

    def parent_of(
        self,
        entry: CatalogueEntry,
        *,
        versions: VersionFilter = VersionFilter.NOT_RETRACTED,
        needs_facets: Collection[str] | None = None,
    ) -> ParentLookup:
        """
        Get the dataset a dataset branched from, out of the stored parent links

        Reads what a parent walk recorded (see [esmporium.db.find_parents][]), and
        says so when nothing has: a dataset whose parent has never been read is
        [ParentStatus.NOT_LOOKED_FOR][esmporium.requirements.ParentStatus] rather than
        a dataset with no parent, because those are different facts and only one of
        them is settled.

        One session per call, for the reason [find][(m).DatabaseCatalogue.find] takes
        one: a held session would hold a transaction whose snapshot predates the walk
        whose links this exists to read.

        Worth knowing about the cost, because it is a different shape to `find`'s. A
        solve asks `find` once per distinct question in the requirement, and asks this
        once per hop per dataset which filled a role -- so a requirement with a lineage
        over a thousand models makes a thousand small lookups rather than one large one.
        That is the thing to measure first if a solve becomes slow, and
        [parent_of_dataset][(m).parent_of_dataset] is the seam to batch behind, since it
        takes the session rather than making one.

        Parameters
        ----------
        entry
            Dataset whose parent is wanted

        versions
            Which of a dataset's versions count, on both ends of the link. See
            [VersionFilter][esmporium.requirements.VersionFilter].

        needs_facets
            The facets the caller will ask the parent about

        Returns
        -------
        :
            The parent, or which situation was met

        Raises
        ------
        ParentLinkMissingError
            A parent row is marked resolved and no link records what to

        UnknownVersionFilterError
            `versions` is not one of the version filters we know
        """
        with Session(self.engine) as session:
            return parent_of_dataset(
                session,
                entry,
                versions=versions,
                needs_facets=needs_facets,
                normalisers=self.normalisers,
                verbatim_normalisers=self.verbatim_normalisers,
                project_query_map=self.project_query_map,
            )
