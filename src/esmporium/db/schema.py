"""
Database schema
"""

# # Don't use this here, it breaks SQLModel
# from __future__ import annotations

import datetime

from sqlalchemy import MetaData, UniqueConstraint
from sqlalchemy.orm import registry
from sqlmodel import Field, SQLModel

from esmporium.search.health import SearchAPICall


def _utcnow() -> datetime.datetime:
    """
    Get the current time, as a timezone-aware UTC datetime

    Used as the default for recorded-at columns.
    We store UTC so rows from different machines are comparable,
    and keep it timezone-aware so the offset is never left to guess.
    """
    return datetime.datetime.now(datetime.timezone.utc)


NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
"""
Naming convention for indexes and constraints

Without this, the database picks the names,
which means our migrations end up referring to constraints
by names we never chose and can't predict.
SQLite in particular can then not drop or alter them.

See https://alembic.sqlalchemy.org/en/latest/naming.html
"""

REGISTRY = registry(metadata=MetaData(naming_convention=NAMING_CONVENTION))
"""
The registry that our tables belong to

We deliberately do not use `SQLModel`'s default registry.
That one is a process-wide global,
so if we registered our tables on it,
any application that used both esmporium and SQLModel
would find our `dataset` table
turning up in *their* database when they called `create_all`.
Owning a registry means our tables are ours.
"""

METADATA: MetaData = REGISTRY.metadata
"""
The metadata describing every table we define

This is what migrations are generated against,
see [`esmporium.db.migrate`][].
"""


class EsmporiumBase(SQLModel, registry=REGISTRY):
    """
    Base class for our tables
    """


# TODO: work out how we handle clashes,
# i.e. two entries that describe the same data but have different IDs.
# We had a warn-on-commit check here (`warn_on_facet_clashes`), now removed:
# the problem is much more complicated than a warning at commit time can cover,
# so we will deal with it as we go.
# Some notes on things we'll need to deal with:
# - clashes on import
#   - if we import data that has a clash on import, we want to warn the user
#     and ideally explain to them why there is a clash
#     based on looking at project-specific metadata (or raw JSON or something)
# - clashes on update
#   - if we update an entry, and that update would lead to a clash,
#     we want to warn the user
#     and ideally explain to them why there is a clash
#     based on looking at project-specific metadata (or raw JSON or something)
# - resolving clashes by deleting an entry
#   - users should be able to pick one of the clashing entries
#     and remove all others easily
# - clashes on data loading
#   - if we load data that has a clash,
#     we want to warn the user
#     and ideally explain to them why there is a clash
#     based on looking at project-specific metadata (or raw JSON or something).
#     The user needs to then somehow be able to specify which dataset to load
#     or how to modify the loading process so the resulting data can be differentiated.


class Dataset(EsmporiumBase, table=True):
    """
    A model output dataset

    We model a dataset as having a single value for each of the facets defined below
    i.e. a single variable, experiment, climate model etc.
    This is our model and it makes sense in our workflows.
    It obviously has implications for how the rest of this package works.
    If you need something different, you will need to use a different package.

    Unfortunately, this data model
    doesn't always match the definition of a dataset used on ESGF.
    For example, CMIP5 datasets contained multiple variables.
    This is unfortunate, but there is nothing we can do:
    there is no universally applicable dataset model for ESGF datasets.
    """

    # SQLModel does not validate table models on construction
    # and has no setting which turns that on directly.
    # Validating assignment gets most of it back, though,
    # because SQLModel's `__init__` sets fields via `setattr`:
    # passing a bad *value* is therefore caught here, at construction,
    # rather than at commit time a long way from wherever the mistake was made.
    #
    # What still slips through is an *omitted* field.
    # It is never assigned, so it is never checked.
    # That is precisely why every facet below is NOT NULL in the database:
    # catching the omission is the database's job, and only the database's.
    model_config = {"validate_assignment": True}

    # TODO: once we parse ESGF records, split the facets out into a base model.
    # Models *without* `table=True` are validated normally, so:
    #
    #     class DatasetBase(EsmporiumBase):
    #         project: str
    #         ...
    #
    #     class Dataset(DatasetBase, table=True):
    #         id: str = Field(primary_key=True)
    #
    # Parsing then builds a `DatasetBase`
    # and promotes it with `Dataset.model_validate(parsed)`.
    # A malformed record then fails where it is read, naming the field at fault,
    # instead of turning up as an IntegrityError from a commit much later,
    # after we have already thrown away the context needed to explain it.
    # Note that `Dataset.model_validate` validates today,
    # so this is a question of where the facets are declared,
    # not of adding new machinery.

    id: str = Field(primary_key=True)
    """
    Unique identifier of the dataset

    Note here that this doesn't include version information.
    A single dataset can have more than one version.
    This version information is handled elsewhere
    (see [`DatasetVersionSpecific`][esmporium.db.schema.DatasetVersionSpecific]).

    Similarly, data access (e.g. node information)
    is also not covered by this ID.
    That lives elsewhere
    (see [`DatasetAccessInformation`][esmporium.db.schema.DatasetAccessInformation]).

    This ID is built from the facet columns, including the variable,
    so it is unique per row even for CMIP5,
    where several variables share a single ESGF dataset.
    See [`id_project_specific`][esmporium.db.schema.Dataset.id_project_specific]
    for the ESGF-side identifier.
    """

    id_project_specific: str = Field(index=True)
    """
    Identifier of the dataset in the project's language

    This is a grouping key, and is deliberately NOT unique.
    Row identity is the primary key
    [`id`][esmporium.db.schema.Dataset.id] (which includes the variable);
    `id_project_specific` instead records which ESGF dataset a row belongs to.

    For CMIP6 and CMIP7 it comes straight from ESGF's
    version- and node-independent id
    (`master_id`, or the STAC feature's version-free id),
    which already includes the variable,
    so it happens to be one-to-one with our rows.

    For CMIP5 it is the `master_id`, which does NOT include a variable.
    A CMIP5 ESGF dataset bundles many variables,
    so every per-variable row we derive from it shares the same `master_id`.
    That is exactly why this column cannot be unique:
    `tas` and `pr` from one CMIP5 dataset legitimately carry the same value.
    It is indexed (not unique) so that "have we seen this ESGF dataset before?"
    stays a cheap lookup without loading project-specific tables.
    """

    project: str
    """
    Project to which this data belongs

    For example, "CMIP5", "CMIP6", "CMIP7", "PaleoMIP".

    Not to be confused with mip_era, which is a slightly different concept
    and doesn't apply to all projects we might want to support.
    """

    model: str
    """
    Climate model that generated the dataset
    """

    institution: str
    """
    Institution that generated this dataset

    This is kept to distinguish the case where two different institutes
    ran the same climate model and used the same variant label.
    This isn't meant to happen, but it might,
    so we keep this column so that we can tell such datasets apart.
    """

    experiment: str
    """
    Experiment to which this dataset belongs

    For example, `historical`, `rcp26`, `ssp434`.
    """

    variant_label: str
    """
    The label for the simulation variant

    This encodes a lot of information, which varies by project.
    For example, it can tell you about whether two simulations
    only differ in initial conditions or the forcings they use.
    We just store the string as-is here.
    Parsing into more detailed information is tricky
    and will have to be done in other layers.

    For example, `r1i1p1f1`, `r1i1p1`
    """

    variable: str
    """
    The variable represented by this dataset

    For example, `tas`, `tos`, `rlut`
    """

    reporting_interval: str
    """
    The reporting interval and style used by this dataset

    This is often referred to as "frequency"
    but the units of the values are the opposite of the units for frequency,
    so we use a clearer term.

    For example, `mon`, `yr`, `3hr`, `monC`
    """

    grid_label: str | None = None
    """
    The label of the grid on which the dataset is reported

    This is just a label, mapping from label to actual grid is tricky.
    We don't handle this mapping here.

    For example, `gn`, `gr`, `g115`

    CMIP5 has no concept of grid_label
    """

    processing_id: str
    """
    The label describing the processing of variables

    This was known as `table_id` for CMIP5 and CMIP6.

    The confusion here is that variable alone does not define the data that will
    be provided. Instead, it is the variable and processing_id that defines the data
    uniquely. Usually, variables are only reported based on one processing_id, so as
    a user you don't notice. However, sometimes variables are reported under more than
    one, which introduces ambiguity.

    As a concrete example, in CMIP5 the variable `ta` is reported for both a
    processing_id="CFmon" and processing_id="Amon". The variable is the same,
    but for processing_id="CFmon" the data is reported on model levels, while for
    processing_id="Amon" the data is reported on pressure levels.

    This processing ID has essentially changed meaning over successive CMIP phases.
    It implies certain characteristics of the data, but it is very hard to check
    what exactly is implied by any given label (without investigating the underlying
    CMOR tables upon which each variable is built).

    For CMIP6, refer to https://github.com/PCMDI/cmip6-cmor-tables/blob/main/Tables
    to verify if a single variable has a unique processing_id.

    As we learn more about this, we will add more details and references here.

    For example, `Amon`, `CFmon`, `3hr`, `AERmon`.
    """

    # # Put this on the version information instead
    # retracted: bool
    # """
    # Whether this dataset has been retracted or not
    #
    # Retracted means that it has been marked as not fit for scientific use.
    #
    # Note that there are other ideas of 'superceded'
    # that ESGF tries to convey (e.g. with the deprecated column).
    # We don't know exactly what these mean,
    # so we don't include them on our core `Dataset` class.
    # The values can be retrieved from project-specific tables
    # if they are needed.
    # """

    # @Anna note: we don't need region. It didn't exist in previous CMIP phases
    # and is tightly coupled to grid in CMIP7
    # so I don't think there's a benefit of adding it in now.
    # If we think it will be useful to add later,
    # we won't add it here.
    # We'll use some wrapper instead as figuring out the region for e.g. CMIP6 data
    # could be super complicated as we'll have to actually look at the data to be sure.

    # # TODO: bring these back in once we start doing searches
    # first_seen_run_id: int | None = Field(default=None, foreign_key="searchrun.id")
    # last_seen_run_id: int | None = Field(default=None, foreign_key="searchrun.id")

    # # TODO: bring this back in once we start doing versions
    # versions: list["DatasetVersion"] = Relationship(
    #     back_populates="dataset",
    #     sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    # )


DATASET_FACET_COLUMNS: tuple[str, ...] = (
    "project",
    "model",
    "institution",
    "experiment",
    "variant_label",
    "variable",
    "reporting_interval",
    "grid_label",
    "processing_id",
)
"""
The columns of [`Dataset`][esmporium.db.schema.Dataset] that describe the data itself

In other words, everything except the ID(s)
and (in future) the bookkeeping columns such as when we last saw the dataset.

Two rows agreeing on all of these is allowed:
the same dataset can legitimately turn up under more than one ID
(ESGF's IDs are not ours to control, and we do not always parse one).
It is unusual enough to be worth telling the user about, though,
which is the open question recorded in the note above
[`Dataset`][esmporium.db.schema.Dataset].

This list is written out rather than derived from the table
because not every future column will be a facet.
Adding a facet to [`Dataset`][esmporium.db.schema.Dataset] means adding it here too.
This is checked by the tests explicitly,
see `test_facet_columns_are_the_declared_facets`.
"""


class SearchAPICallRecord(EsmporiumBase, table=True):
    """
    One recorded request to one search API

    An append-only log.
    We never update these rows, so they accumulate a history:
    which host was asked what, when, how it answered and how long it took.
    That is enough to tell, later and with a plain `GROUP BY`,
    which nodes are fast, which are flaky, and which have data for a project.
    """

    # See the note on `Dataset.model_config`: this catches a bad *value* at
    # construction rather than at commit time.
    model_config = {"validate_assignment": True}

    id: int | None = Field(default=None, primary_key=True)
    """Surrogate key; assigned by the database"""

    created_at: datetime.datetime = Field(default_factory=_utcnow, index=True)
    """
    When the call was recorded (UTC)

    Indexed so that "how did things look today?" is a cheap query.
    """

    host: str = Field(index=True)
    """
    The host the request went to, e.g. `esgf.nci.org.au`

    Indexed so that "how has this node behaved?" is a cheap query.
    """

    http_method: str
    """The HTTP method used, e.g. `GET` or `POST`"""

    url: str
    """The full URL the request went to, including any query string"""

    request_body: str | None = None
    """The request body that was sent (STAC `POST`s), or `None` (Solr `GET`s)"""

    response_code: int | None = None
    """
    The HTTP status code the host answered with

    `None` when nothing answered at all (a transport error or a timeout),
    which is how "the host said no" is told apart from "the host never spoke".
    """

    success: bool
    """Whether we got a usable answer back"""

    error: str | None = None
    """
    The failure's type and message, or `None` on success
    """

    num_results: int | None = None
    """
    The number of records the host reported matched, if it reported one

    `None` when the response carries no count we can read.
    """

    response_time_seconds: float
    """How long the call took, in wall-clock seconds"""

    attempt_number: int
    """
    Which attempt this row is, 1-based

    One row is recorded per HTTP attempt, so a host that had to be retried
    leaves several rows for one logical request: attempt 1, attempt 2, and so on.
    The successful attempt (if any) is the last one, and is the only one that
    carries a result count.
    """

    @classmethod
    def from_call(cls, call: SearchAPICall) -> "SearchAPICallRecord":
        """
        Initialise from the search layer's plain record of a call

        Parameters
        ----------
        call
            What the search layer observed about one call

        Returns
        -------
        :
            Initialised object
        """
        return cls(
            host=call.host,
            http_method=call.http_method,
            url=call.url,
            request_body=call.request_body,
            response_code=call.response_code,
            success=call.success,
            error=(
                f"{type(call.error).__name__}: {call.error}"
                if call.error is not None
                else None
            ),
            num_results=call.num_results,
            response_time_seconds=call.response_time_seconds,
            attempt_number=call.attempt_number,
        )


class DatasetVersionSpecific(EsmporiumBase, table=True):
    """
    One published version (edition) of a [`Dataset`][esmporium.db.schema.Dataset]

    A dataset can be published more than once over time
    (a rerun, a fix, or more variables added).
    Each such edition is a row here, dated by its `version`.

    Versions are per-variable, i.e. per `Dataset`, on purpose.
    In CMIP5 the set of variables can change between editions,
    so "which editions exist" is genuinely a per-variable question:
    `tas` may appear in three editions while another variable appears in only one.
    Keying versions to the (per-variable) `Dataset` is what records that faithfully.

    Version-specific information does not include data access (e.g. data node)
    information. That lives in
    [`DatasetAccessInformation`][esmporium.db.schema.DatasetAccessInformation].
    """

    # See the note on `Dataset.model_config`: this catches a bad *value* at
    # construction rather than at commit time.
    model_config = {"validate_assignment": True}

    version_id: str = Field(primary_key=True)
    """
    Unique identifier of this version

    Built deterministically as `f"{dataset.id}.v{version}"`,
    so re-ingesting the same edition upserts the same row
    rather than creating a duplicate.
    """

    dataset_id: str = Field(foreign_key="dataset.id", index=True)
    """
    The [`Dataset`][esmporium.db.schema.Dataset] this is a version of

    See [`Dataset.id`][esmporium.db.schema.Dataset.id].
    """

    version: str
    """
    The version string, as ESGF reports it

    Usually a date, e.g. `20200623`.
    Note that CMIP5 replicas can carry inconsistent version strings across nodes
    (a date on one node, `1` on another),
    so this is not always comparable across data nodes.
    """

    is_latest: bool
    """
    Whether this was the latest edition when we searched

    A snapshot: ESGF flips this to `False` when a newer edition is published,
    so a re-search may need to update it.
    "Latest among the editions we hold" can always be recomputed from these rows.
    """

    retracted: bool
    """
    Whether this edition was retracted when we searched

    Also a snapshot; a retraction can happen after we recorded the row.
    """


class DatasetAccessInformation(EsmporiumBase, table=True):
    """
    Where one version can be downloaded

    A single [version][esmporium.db.schema.DatasetVersionSpecific]
    can be hosted on several data nodes (replicas),
    so there is one row here per (version, data node).
    This is purely about access:
    which node, whether it is a replica, and the URLs.
    """

    # See the note on `Dataset.model_config`.
    model_config = {"validate_assignment": True}

    id: int | None = Field(default=None, primary_key=True)
    """Surrogate key; assigned by the database"""

    version_id: str = Field(foreign_key="datasetversionspecific.version_id", index=True)
    """
    The version this copy is of

    See [`DatasetVersionSpecific`][esmporium.db.schema.DatasetVersionSpecific].
    """

    data_node: str
    """The data node hosting this copy, e.g. `esgf.nci.org.au`"""

    index_node: str | None = None
    """The index node that reported this copy (Solr), if known"""

    replica: bool
    """Whether this copy is a replica (a copy of an original published elsewhere)"""

    access_urls: str
    """
    The download URLs for this copy, as a JSON-encoded list

    Stored as text because SQLite has no native JSON column type.
    """

    __table_args__ = (UniqueConstraint("version_id", "data_node"),)


class DatasetRawDoc(EsmporiumBase, table=True):
    """
    The raw search document behind a version

    We keep the exact JSON a search API returned,
    so nothing a record carried is lost to our column choices.
    One row per distinct source document, deduplicated by `esgf_doc_id`.

    The raw document is anchored on the *version* because that is the one grain
    both ESGF generations share:
    Solr returns one document per (version, node),
    while STAC returns one per version (with node/replica info inside its assets).
    So a version can have several raw documents
    (one per node, or one per search generation),
    which is why this is a many-to-one link from here to the version.

    For CMIP5 a single document bundles many variables
    and therefore describes several of our per-variable versions at once.
    That many-to-many case is out of scope here (we ingest one variable)
    and would be handled by a future `(raw_id, version_id)` link table;
    see `docs/further-background/results-to-database.md`.
    """

    # See the note on `Dataset.model_config`.
    model_config = {"validate_assignment": True}

    id: int | None = Field(default=None, primary_key=True)
    """Surrogate key; assigned by the database"""

    version_id: str = Field(foreign_key="datasetversionspecific.version_id", index=True)
    """
    The version this document describes

    See [`DatasetVersionSpecific`][esmporium.db.schema.DatasetVersionSpecific].
    """

    esgf_doc_id: str = Field(unique=True, index=True)
    """
    The source document's own ESGF id

    For Solr this is the record `id`, `<instance_id>|<data_node>`,
    so the data node can be recovered as `esgf_doc_id.rsplit("|", 1)[-1]`.
    For STAC it is the feature `id`.
    Unique, so re-ingesting the same document reuses this row
    rather than duplicating the JSON.
    """

    source_api: str
    """Which search API/generation produced this, e.g. `esgf1-solr`, `esgf-ng-stac`"""

    search_host: str
    """The endpoint queried, e.g. `esgf.nci.org.au` or `search.east.esgf.io`"""

    raw_json: str
    """The document exactly as returned, JSON-encoded"""

    retrieved_at: datetime.datetime = Field(default_factory=_utcnow)
    """When we stored this document (UTC)"""
