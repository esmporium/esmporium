"""
Database schema
"""

# # Don't use this here, it breaks SQLModel
# from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import registry
from sqlmodel import Field, SQLModel

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
    This version information is handled elsewhere.

    Similarly, data access (e.g. node information)
    is also not covered by this ID.
    That lives elsewhere.
    """

    id_project_specific: str = Field(unique=True)
    """
    Unique identifier of the dataset in the project's language

    This is useful for being able to quickly check
    if we have seen a given dataset before
    (e.g. when re-running a search)
    without having to load other information from project-specific tables.

    This is a bit complicated.
    Where possible, e.g. CMIP6, we just use the unique ID from the project
    (which in the case of CMIP6 just comes from ESGF's `master_id`).
    In trickier cases, e.g. CMIP5, we have to do a bit more work.
    There is no ID on ESGF that fits our use case:
    the `master_id` does not include a variable
    (so would not be unique for most use cases)
    and other IDs are file-specific i.e. are too high granularity.
    Thus, we will have to create these IDs at ingestion time.
    That is a bit painful, but unavoidable given the mismatch
    between the data model we use and the data model ESGF uses.
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

    grid_label: str
    """
    The label of the grid on which the dataset is reported

    This is just a label, mapping from label to actual grid is tricky.
    We don't handle this mapping here.

    For example, `gn`, `gr`, `g115`
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
