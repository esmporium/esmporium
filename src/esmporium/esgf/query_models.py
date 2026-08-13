"""
Facet search input dialects — the vocabulary a user types their query in.

Users can choose which project language to search in, regardless of which
project they search for. Each search class acts as a skin in one project
dialect's vocabulary. Every skin implements exactly one method,
`to_canonical()`, which lowers it into the shared
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

from esmporium.esgf.canonical import (
    CANONICAL_FACETS,
    CanonicalQuery,
    _normalise_facet_values,
)
from esmporium.esgf.project_translation_maps import get_profile


class _ESGFQueryBase(BaseModel):
    """
    Shared machinery for every dialect skin.

    Holds the two non-facet fields (`project`, `other_terms`), normalises all
    inputs, and provides the generic `to_canonical()` lowering. Subclasses add
    their dialect's facet fields (named with that project's native facet names) and
    say how a native name maps to a canonical one.
    """

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

    def _dialect(self) -> str:
        """Return a label for the input dialect, recorded in the IR's `source_spec`."""
        raise NotImplementedError

    def _canonical_name(self, native: str) -> str | None:
        """Map one of this skin's native facet names to a canonical name, or `None`."""
        raise NotImplementedError(
            f"{type(self).__name__} must implement _canonical_name (native={native!r})"
        )

    def to_canonical(self) -> CanonicalQuery:
        """
        Lower this dialect query into the canonical IR.

        Facets that map to a canonical name are set on the IR. Facets that do not
        map (such as CMIP5 `product`, which is project-specific) go into
        `extra_facets` and are passed through without translation. The original
        query, exactly as typed, is recorded in `source_spec` so nothing is ever
        lost from the record.
        """
        canonical_fields: dict[str, tuple[str, ...]] = {}
        extra_facets: dict[str, tuple[str, ...]] = {}
        typed_facets: dict[str, tuple[str, ...]] = {}

        for native in self._facet_field_names():
            values = getattr(self, native)
            if not values:
                continue
            typed_facets[native] = values
            canonical = self._canonical_name(native)
            if canonical is not None:
                canonical_fields[canonical] = values
            else:
                extra_facets[native] = values

        # other_terms are always passthrough (best-effort).
        for name, values in self.other_terms.items():
            extra_facets[name] = values

        return CanonicalQuery(
            **canonical_fields,
            extra_facets=extra_facets,
            source_spec={
                "dialect": self._dialect(),
                "facets": typed_facets,
                "other_terms": dict(self.other_terms),
            },
        )


class _ESGFQueryProject(_ESGFQueryBase):
    """A skin whose native names are translated via that project's profile."""

    # The native project of this dialect (NOT the target — that is `project`).
    _project: ClassVar[str]

    def _dialect(self) -> str:
        return self._project

    def _canonical_name(self, native: str) -> str | None:
        return get_profile(self._project).canonical_facet(native)


class ESGFQuery(_ESGFQueryBase):
    """
    The unified, neutral-vocabulary skin — the recommended multi-project front door.

    Its field names are already the canonical names, so lowering is near-identity.
    `project` has no default: being project-neutral, it is where you say which
    project(s) to search.
    """

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

    def _dialect(self) -> str:
        return "unified"

    def _canonical_name(self, native: str) -> str | None:
        # The unified skin's names are canonical names.
        return native if native in CANONICAL_FACETS else None


class ESGFQueryCMIP5(_ESGFQueryProject):
    """
    Type a query in CMIP5's native vocabulary (`institute`, `ensemble`, ...).

    Its field names are CMIP5's own; `to_canonical()` lowers them to canonical
    names via the CMIP5 profile. `project` defaults to `("CMIP5",)`; override it to
    type in CMIP5 words while targeting other project(s). Each field links to the
    canonical facet it maps to.
    """

    _project: ClassVar[str] = "CMIP5"
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


class ESGFQueryCMIP6(_ESGFQueryProject):
    """
    Type a query in CMIP6's native vocabulary (`source_id`, `table_id`, ...).

    Its field names are CMIP6's own; `to_canonical()` lowers them to canonical
    names via the CMIP6 profile. `project` defaults to `("CMIP6",)`; override it to
    type in CMIP6 words while targeting other project(s). Each field links to the
    canonical facet it maps to.
    """

    _project: ClassVar[str] = "CMIP6"
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


class ESGFQueryCMIP7(_ESGFQueryProject):
    """
    Type a query in CMIP7's native vocabulary (as CMIP6 but `branding_suffix`).

    Its field names are CMIP7's own; `to_canonical()` lowers them to canonical
    names via the CMIP7 profile. `project` defaults to `("CMIP7",)`; override it to
    type in CMIP7 words while targeting other project(s). Each field links to the
    canonical facet it maps to.
    """

    _project: ClassVar[str] = "CMIP7"
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
