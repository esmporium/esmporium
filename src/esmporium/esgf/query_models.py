"""
Facet search input dialects — the vocabulary a user types their query in.

Users can choose which project language to search in, regardless of which
project they search for. Each search class acts as a skin in one project
dialect's vocabulary, differing only in its native field names and the
translation profile (`_profile`) it carries. A single shared `to_canonical()`
lowers any skin into the shared
[`CanonicalQuery`][esmporium.esgf.canonical.CanonicalQuery]. There are no
`to_<other_project>` methods anywhere: a cross-project journey is composed by
lowering to canonical and rendering back out through a project profile.

Two independent axes meet here:

- the **class** you pick is only *which words you type* (`source_id` vs `model`);
- the **`project`** field is *which project's data you want back*, and may be a
  collection to target several projects at once.

So "CMIP6 words, CMIP5 data" is just `ESGFQueryCMIP6(..., project="CMIP5")`. Each
project skin defaults `project` to its own project; override it to retarget.
"""

from typing import ClassVar

from pydantic import BaseModel, ValidationInfo, field_validator

from esmporium.esgf.canonical import CanonicalQuery, _normalise_facet_values
from esmporium.esgf.project_translation_maps import (
    CMIP5_PROFILE,
    CMIP6_PROFILE,
    CMIP7_PROFILE,
    IDENTITY_PROFILE,
    ProjectProfile,
)


def lower_to_canonical(
    typed_facets: dict[str, tuple[str, ...]],
    profile: ProjectProfile,
    dialect: str,
    other_terms: dict[str, tuple[str, ...]],
) -> CanonicalQuery:
    """
    Build a `CanonicalQuery` from a dialect's set facets

    This is the lowering behaviour on its own, independent of any ESGFQuery* class. A
    native facet is placed on a canonical field when `profile` maps it to a
    canonical one, and otherwise passed through in `extra_facets` (this is how
    project-specific facets like CMIP5 `product` travel). `other_terms` are always
    passthrough. The original per-dialect input is retained in `source_spec`.

    `profile` is the dialect's translation data (see
    [`ProjectProfile`][esmporium.esgf.project_translation_maps.ProjectProfile]);
    its `canonical_facet` maps each native name to a canonical one (or `None`).
    Passing the profile in as data — rather than resolving a mapping from a
    subclass through `self` — is what lets this be a function.
    """
    canonical_fields: dict[str, tuple[str, ...]] = {}
    extra_facets: dict[str, tuple[str, ...]] = {}

    for native, values in typed_facets.items():
        canonical = profile.canonical_facet(native)
        if canonical is not None:
            canonical_fields[canonical] = values
        else:
            extra_facets[native] = values

    # other_terms are always passthrough (best-effort).
    for name, values in other_terms.items():
        extra_facets[name] = values

    return CanonicalQuery(
        **canonical_fields,
        extra_facets=extra_facets,
        source_spec={
            "dialect": dialect,
            "facets": dict(typed_facets),
            "other_terms": dict(other_terms),
        },
    )


class _ESGFQueryBase(BaseModel):
    """
    Shared machinery for every dialect skin.

    Holds the two non-facet fields (`project`, `other_terms`), normalises all
    inputs, and provides the generic `to_canonical()` lowering. Subclasses add
    their dialect's facet fields (named with that project's native facet names) and
    set `_profile` to the profile that maps those names to canonical ones.
    """

    # The dialect's translation data. Each concrete skin sets this to its profile
    # (the unified skin uses IDENTITY_PROFILE); `to_canonical` hands it to
    # `lower_to_canonical`. Not a field — class-level data, not user input.
    _profile: ClassVar[ProjectProfile]

    # The project(s) whose data to search. A tuple so multi-project handling is
    # uniform; each project skin overrides the default with its own project.
    project: tuple[str, ...] = ()

    # Escape hatch for facets we have not modelled. Left as-is, no translation
    # in to_canonical().
    other_terms: dict[str, tuple[str, ...]] = {}

    @field_validator("*", mode="before")
    @classmethod
    def _normalise(cls, value: object, info: ValidationInfo) -> object:
        """Coerce facet/project inputs to tuples and `other_terms` values likewise."""
        if info.field_name == "other_terms":
            if value is None:
                return {}
            if isinstance(value, dict):
                return {key: _normalise_facet_values(val) for key, val in value.items()}
            return value  # let pydantic raise the usual "not a dict" error
        return _normalise_facet_values(value)

    def _facet_field_names(self) -> list[str]:
        """List this skin's facet fields, in declaration order (excluding the above)."""
        return [
            name
            for name in type(self).model_fields
            if name not in ("project", "other_terms")
        ]

    def to_canonical(self) -> CanonicalQuery:
        """
        Lower this dialect query into the canonical IR.

        Facets that map to a canonical name are set on the IR. Facets that do not
        map (such as CMIP5 `product`, which is project-specific) go into
        `extra_facets` and are passed through without translation. The original
        query, exactly as typed, is recorded in `source_spec` so nothing is ever
        lost from the record.
        """
        typed_facets = {
            native: getattr(self, native)
            for native in self._facet_field_names()
            if getattr(self, native)
        }
        return lower_to_canonical(
            typed_facets,
            profile=self._profile,
            dialect=self._profile.project,
            other_terms=self.other_terms,
        )


class ESGFQuery(_ESGFQueryBase):
    """
    The unified, neutral-vocabulary skin — the recommended multi-project front door.

    Its field names are already the canonical names, so lowering is near-identity.
    `project` has no default: being project-neutral, it is where you say which
    project(s) to search.
    """

    _profile: ClassVar[ProjectProfile] = IDENTITY_PROFILE

    model: tuple[str, ...] = ()
    """See [`model`][esmporium.db.schema.Dataset.model]."""
    institution: tuple[str, ...] = ()
    """See [`institution`][esmporium.db.schema.Dataset.institution]."""
    experiment: tuple[str, ...] = ()
    """See [`experiment`][esmporium.db.schema.Dataset.experiment]."""
    variable: tuple[str, ...] = ()
    """See [`variable`][esmporium.db.schema.Dataset.variable]."""
    variant_label: tuple[str, ...] = ()
    """See [`variant_label`][esmporium.db.schema.Dataset.variant_label]."""
    reporting_interval: tuple[str, ...] = ()
    """See [`reporting_interval`][esmporium.db.schema.Dataset.reporting_interval]."""
    processing_id: tuple[str, ...] = ()
    """See [`processing_id`][esmporium.db.schema.Dataset.processing_id]."""
    activity: tuple[str, ...] = ()
    """See [`activity`][esmporium.esgf.canonical.CanonicalQuery.activity]."""
    resolution: tuple[str, ...] = ()
    """See [`resolution`][esmporium.esgf.canonical.CanonicalQuery.resolution]."""
    grid_label: tuple[str, ...] = ()
    """See [`grid_label`][esmporium.db.schema.Dataset.grid_label]."""
    realm: tuple[str, ...] = ()
    """See [`realm`][esmporium.esgf.canonical.CanonicalQuery.realm]."""


class ESGFQueryCMIP5(_ESGFQueryBase):
    """
    Type a query in CMIP5's native vocabulary (`institute`, `ensemble`, ...).

    Its field names are CMIP5's own; `to_canonical()` lowers them to canonical
    names via the CMIP5 profile. `project` defaults to `("CMIP5",)`; override it to
    type in CMIP5 words while targeting other project(s). Each field links to the
    canonical facet it maps to.
    """

    _profile: ClassVar[ProjectProfile] = CMIP5_PROFILE
    project: tuple[str, ...] = ("CMIP5",)

    model: tuple[str, ...] = ()
    """See [`model`][esmporium.db.schema.Dataset.model]."""
    institute: tuple[str, ...] = ()
    """See [`institution`][esmporium.db.schema.Dataset.institution]."""
    experiment: tuple[str, ...] = ()
    """See [`experiment`][esmporium.db.schema.Dataset.experiment]."""
    ensemble: tuple[str, ...] = ()
    """See [`variant_label`][esmporium.db.schema.Dataset.variant_label]."""
    variable: tuple[str, ...] = ()
    """See [`variable`][esmporium.db.schema.Dataset.variable]."""
    time_frequency: tuple[str, ...] = ()
    """See [`reporting_interval`][esmporium.db.schema.Dataset.reporting_interval]."""
    cmor_table: tuple[str, ...] = ()
    """See [`processing_id`][esmporium.db.schema.Dataset.processing_id]."""
    realm: tuple[str, ...] = ()
    """See [`realm`][esmporium.esgf.canonical.CanonicalQuery.realm]."""
    product: tuple[str, ...] = ()
    """CMIP5-only facet (e.g. `output1`); has no equivalent in other projects."""


class ESGFQueryCMIP6(_ESGFQueryBase):
    """
    Type a query in CMIP6's native vocabulary (`source_id`, `table_id`, ...).

    Its field names are CMIP6's own; `to_canonical()` lowers them to canonical
    names via the CMIP6 profile. `project` defaults to `("CMIP6",)`; override it to
    type in CMIP6 words while targeting other project(s). Each field links to the
    canonical facet it maps to.
    """

    _profile: ClassVar[ProjectProfile] = CMIP6_PROFILE
    project: tuple[str, ...] = ("CMIP6",)

    source_id: tuple[str, ...] = ()
    """See [`model`][esmporium.db.schema.Dataset.model]."""
    institution_id: tuple[str, ...] = ()
    """See [`institution`][esmporium.db.schema.Dataset.institution]."""
    experiment_id: tuple[str, ...] = ()
    """See [`experiment`][esmporium.db.schema.Dataset.experiment]."""
    variant_label: tuple[str, ...] = ()
    """See [`variant_label`][esmporium.db.schema.Dataset.variant_label]."""
    variable_id: tuple[str, ...] = ()
    """See [`variable`][esmporium.db.schema.Dataset.variable]."""
    frequency: tuple[str, ...] = ()
    """See [`reporting_interval`][esmporium.db.schema.Dataset.reporting_interval]."""
    grid_label: tuple[str, ...] = ()
    """See [`grid_label`][esmporium.db.schema.Dataset.grid_label]."""
    table_id: tuple[str, ...] = ()
    """See [`processing_id`][esmporium.db.schema.Dataset.processing_id]."""
    activity_id: tuple[str, ...] = ()
    """See [`activity`][esmporium.esgf.canonical.CanonicalQuery.activity]."""
    nominal_resolution: tuple[str, ...] = ()
    """See [`resolution`][esmporium.esgf.canonical.CanonicalQuery.resolution]."""
    realm: tuple[str, ...] = ()
    """See [`realm`][esmporium.esgf.canonical.CanonicalQuery.realm]."""


class ESGFQueryCMIP7(_ESGFQueryBase):
    """
    Type a query in CMIP7's native vocabulary (as CMIP6 but `branding_suffix`).

    Its field names are CMIP7's own; `to_canonical()` lowers them to canonical
    names via the CMIP7 profile. `project` defaults to `("CMIP7",)`; override it to
    type in CMIP7 words while targeting other project(s). Each field links to the
    canonical facet it maps to.
    """

    _profile: ClassVar[ProjectProfile] = CMIP7_PROFILE
    project: tuple[str, ...] = ("CMIP7",)

    source_id: tuple[str, ...] = ()
    """See [`model`][esmporium.db.schema.Dataset.model]."""
    institution_id: tuple[str, ...] = ()
    """See [`institution`][esmporium.db.schema.Dataset.institution]."""
    experiment_id: tuple[str, ...] = ()
    """See [`experiment`][esmporium.db.schema.Dataset.experiment]."""
    variant_label: tuple[str, ...] = ()
    """See [`variant_label`][esmporium.db.schema.Dataset.variant_label]."""
    variable_id: tuple[str, ...] = ()
    """See [`variable`][esmporium.db.schema.Dataset.variable]."""
    frequency: tuple[str, ...] = ()
    """See [`reporting_interval`][esmporium.db.schema.Dataset.reporting_interval]."""
    grid_label: tuple[str, ...] = ()
    """See [`grid_label`][esmporium.db.schema.Dataset.grid_label]."""
    branding_suffix: tuple[str, ...] = ()
    """See [`processing_id`][esmporium.db.schema.Dataset.processing_id]."""
    activity_id: tuple[str, ...] = ()
    """See [`activity`][esmporium.esgf.canonical.CanonicalQuery.activity]."""
    nominal_resolution: tuple[str, ...] = ()
    """See [`resolution`][esmporium.esgf.canonical.CanonicalQuery.resolution]."""
    realm: tuple[str, ...] = ()
    """See [`realm`][esmporium.esgf.canonical.CanonicalQuery.realm]."""
