"""
Database schema
"""

# # Don't use this here, it breaks SQLModel
# from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import registry
from sqlmodel import Field, SQLModel, UniqueConstraint

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

    Inheriting from this rather than `SQLModel` directly
    is what puts a table in [`REGISTRY`][esmporium.db.schema.REGISTRY].
    """


class Dataset(EsmporiumBase, table=True):
    """
    A model output dataset

    Following ESGF's data model, this is always the data for a single variable,
    from a single experiment, from a single climate model...
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

    __table_args__ = (
        UniqueConstraint(
            # The column order here is chosen for querying, not for readability.
            # An index can be used for any *leading prefix* of its columns,
            # so this one also answers a search on variable alone,
            # or on variable plus experiment, and so on.
            # The columns are therefore ordered
            # roughly by how often we expect to filter on them.
            "variable",
            "experiment",
            "model",
            "variant_label",
            "reporting_interval",
            "grid_label",
            "project",
            "institution",
            name="uq_dataset_facets",
        ),
    )

    # Note: this needs to come from master_id when reading ESGF records (for CMIP6).
    # Likely will need to be created manually for CMIP5
    # We call it id beacuse that's what it is and we think the ESGF name is confusing.
    id: str = Field(primary_key=True)
    """
    Unique identifier of the dataset

    Note here that this doesn't include version information.
    A single dataset can have more than one version.
    This version information is handled elsewhere (i.e. not in this ID).

    Similarly, data access (e.g. node information)
    is also not covered by this ID.
    That lives elsewhere.
    """

    project: str
    """
    Project to which this data belongs

    For example, "CMIP5", "CMIP6", "CMIP7", "PaleoMIP".

    Not to be confused with mip_era, which is a slightly different concept
    and doesn't apply to all projects we might want to support.
    """

    # Note: this is called source_id in CMIP6,
    # model in CMIP5 and we think also source_id in CMIP7,
    # but we have to check.
    model: str
    """
    Climate model that generated the dataset
    """

    institution: str
    """
    Institution that generated this dataset

    This is kept to avoid clashes where two different institutes
    ran the same climate model and used the same variant label.
    This isn't meant to happen, but it might,
    so we keep this column to ensure we don't get such clashes.
    """
    # Note: clashing rows are handled by `uq_dataset_facets` above.
    # That constraint depends on every facet being NOT NULL,
    # because SQL treats two NULLs as different values,
    # so a nullable facet would let duplicates slip past it.
    # In other words, the facets below are not optional by accident:
    # making any of them optional again would quietly weaken that constraint.

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

    # TODO: decide what to put here for projects that have no grid label.
    # CMIP5 has no such concept, so ingesting CMIP5 will need to pick a value
    # (a sentinel such as "unknown") rather than leaving this unset.
    # Likely manually add 'gn' for CMIP5, as all are considered native
    # grids, even if on cartesian coords.
    # Making the column optional instead is not a fix, see the note on
    # `institution` above: it would put the hole back in `uq_dataset_facets`.
    grid_label: str
    """
    The label of the grid on which the dataset is reported

    This is just a label, mapping from label to actual grid is tricky.
    We don't handle this mapping here.

    For example, `gn`, `gr`, `g115`
    """

    # TODO: does branding suffix kind of replace table_id for CMIP7?
    processing_id: str
    """
    The label describing the dimensions of variables

    Includes the reporting interval (frequency) and may include information relating
    to latitude, longitude or vertical dimension.

    A dataset for a single variable may differ by processing_id.
    Known as `table_id` for CMIP5 and CMIP6.

    For example, `Amon`, `CFmon`, `3hr`
    """

    # # TODO: check whether this is easily available from CMIP6
    # # and whether we should include it here.
    # # It might have to come during metadata from header enrichment
    # # because it isn't returned by the search API.
    # # It might also not be necessary because it is implied by the grid...
    # region: str | None = None
    # """
    # The region over which the dataset is reported
    #
    # For example, `glb`, `gr`, `g115`
    # """

    # # TODO: bring these back in once we start doing searches
    # first_seen_run_id: int | None = Field(default=None, foreign_key="searchrun.id")
    # last_seen_run_id: int | None = Field(default=None, foreign_key="searchrun.id")

    # # TODO: bring this back in once we start doing versions
    # versions: list["DatasetVersion"] = Relationship(
    #     back_populates="dataset",
    #     sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    # )
