"""
Database schema
"""

# # Don't use this here, it breaks SQLModel
# from __future__ import annotations

from sqlmodel import Field, SQLModel


class Dataset(SQLModel, table=True):
    """
    A model output dataset

    Following ESGF's data model, this is always the data for a single variable,
    from a single experiment, from a single climate model...
    """

    # Note: this needs to come from master_id when reading ESGF records.
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

    # TOOD: remove `None` as an option.
    # This column should not allow None values.
    project: str | None = None
    """
    Project to which this data belongs

    For example, "CMIP5", "CMIP6", "CMIP7", "PaleoMIP".

    Not to be confused with mip_era, which is a slightly different concept
    and doesn't apply to all projects we might want to support.
    """

    # Note: this is called source_id in CMIP6,
    # model in CMIP5 and we think also source_id in CMIP7,
    # but we have to check.
    # TOOD: remove `None` as an option.
    # This column should not allow None values.
    model: str | None = Field(default=None, index=True)
    """
    Climate model that generated the dataset
    """

    # TOOD: remove `None` as an option.
    # This column should not allow None values.
    institution: str | None = None
    """
    Institution that generated this dataset

    This is kept to avoid clashes where two different institutes
    ran the same climate model and used the same variant label.
    This isn't meant to happen, but it might,
    so we keep this column to ensure we don't get such clashes.
    """
    # Note: somewhere we need to be careful about clashing rows in our database,
    # this generally should be impossible.

    # TOOD: remove `None` as an option.
    # This column should not allow None values.
    experiment: str | None = Field(default=None, index=True)
    """
    Experiment to which this dataset belongs

    For example, `historical`, `rcp26`, `ssp434`.
    """

    # TOOD: remove `None` as an option.
    # This column should not allow None values.
    variant_label: str | None = Field(default=None, index=True)
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

    # TOOD: remove `None` as an option.
    # This column should not allow None values.
    variable: str | None = Field(default=None, index=True)
    """
    The variable represented by this dataset

    For example, `tas`, `tos`, `rlut`
    """

    # TOOD: remove `None` as an option.
    # This column should not allow None values.
    reporting_interval: str | None = None
    """
    The reporting interval and style used by this dataset

    This is often referred to as "frequency"
    but the units of the values are the opposite of the units for frequency,
    so we use a clearer term.

    For example, `mon`, `yr`, `3hr`, `monC`
    """

    # TOOD: remove `None` as an option.
    # This column should not allow None values.
    grid_label: str | None = None
    """
    The label of the grid on which the dataset is reported

    This is just a label, mapping from label to actual grid is tricky.
    We don't handle this mapping here.

    For example, `gn`, `gr`, `g115`
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
