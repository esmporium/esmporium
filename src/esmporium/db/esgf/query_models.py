"""
Input dialect skins — the in-adapters (the "N" side of hub-and-spoke).

Each skin is an ergonomic front door in one MIP dialect's vocabulary. Every skin
implements exactly one method, `to_canonical()`, which lowers it into the shared
[`CanonicalQuery`][esmporium.db.esgf.canonical.CanonicalQuery]. There are no
`to_<other_era>` methods anywhere: a cross-era journey is composed by lowering to
canonical and rendering back out through an era profile.

Two independent axes meet here:

- the **class** you pick is only *which words you type* (`source_id` vs `model`);
- the **`project`** field is *which era's data you want back*, and may be a
  collection to target several eras at once.

So "CMIP6 words, CMIP5 data" is just `ESGFQueryCMIP6(..., project="CMIP5")`. Each
era skin defaults `project` to its own era; override it to retarget.
"""

from typing import ClassVar

from pydantic import BaseModel, ValidationInfo, field_validator

from esmporium.db.esgf.canonical import (
    CANONICAL_FACETS,
    CanonicalQuery,
    _normalise_facet_values,
)
from esmporium.db.esgf.mip_translation import get_profile


class _ESGFQueryBase(BaseModel):
    """
    Shared machinery for every dialect skin.

    Holds the two non-facet fields (`project`, `other_terms`), normalises all
    inputs, and provides the generic `to_canonical()` lowering. Subclasses add
    their dialect's facet fields (named with that era's *native* facet names) and
    say how a native name maps to a canonical one.
    """

    # The era(s) whose data to search. A tuple so multi-era is uniform; each era
    # skin overrides the default with its own era.
    project: tuple[str, ...] = ()

    # Escape hatch for facets we have not modelled. Rendered best-effort, as-is.
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

        Facets that map to a canonical name are set on the IR; facets that do not
        (category-3, e.g. CMIP5 `product`) and every `other_terms` entry go into
        the `extra_facets` passthrough. The original query, exactly as typed, is
        recorded in `source_spec` so nothing is ever lost from the record.
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


class _ESGFQueryEra(_ESGFQueryBase):
    """A skin whose native names are translated via that era's profile."""

    # The native era of this dialect (NOT the target — that is `project`).
    _mip_era: ClassVar[str]

    def _dialect(self) -> str:
        return self._mip_era

    def _canonical_name(self, native: str) -> str | None:
        return get_profile(self._mip_era).canonical_facet(native)


class ESGFQuery(_ESGFQueryBase):
    """
    The unified, neutral-vocabulary skin — the recommended multi-era front door.

    Its field names are already the canonical names, so lowering is near-identity.
    `project` has no default: being era-neutral, it is where you say which era(s)
    to search.
    """

    model: tuple[str, ...] = ()
    institution: tuple[str, ...] = ()
    experiment: tuple[str, ...] = ()
    variable: tuple[str, ...] = ()
    variant_label: tuple[str, ...] = ()
    reporting_interval: tuple[str, ...] = ()
    processing_id: tuple[str, ...] = ()
    activity: tuple[str, ...] = ()
    resolution: tuple[str, ...] = ()
    grid_label: tuple[str, ...] = ()
    realm: tuple[str, ...] = ()

    def _dialect(self) -> str:
        return "unified"

    def _canonical_name(self, native: str) -> str | None:
        # The unified skin's names *are* canonical names.
        return native if native in CANONICAL_FACETS else None


class ESGFQueryCMIP5(_ESGFQueryEra):
    """CMIP5-native vocabulary (`model`, `institute`, `ensemble`, ...)."""

    _mip_era: ClassVar[str] = "CMIP5"
    project: tuple[str, ...] = ("CMIP5",)

    model: tuple[str, ...] = ()
    institute: tuple[str, ...] = ()
    experiment: tuple[str, ...] = ()
    ensemble: tuple[str, ...] = ()
    variable: tuple[str, ...] = ()
    time_frequency: tuple[str, ...] = ()
    cmor_table: tuple[str, ...] = ()
    realm: tuple[str, ...] = ()
    product: tuple[str, ...] = ()
    """CMIP5-only facet (e.g. `output1`); has no equivalent in other eras."""


class ESGFQueryCMIP6(_ESGFQueryEra):
    """CMIP6-native vocabulary (`source_id`, `variant_label`, `table_id`, ...)."""

    _mip_era: ClassVar[str] = "CMIP6"
    project: tuple[str, ...] = ("CMIP6",)

    source_id: tuple[str, ...] = ()
    institution_id: tuple[str, ...] = ()
    experiment_id: tuple[str, ...] = ()
    variant_label: tuple[str, ...] = ()
    variable_id: tuple[str, ...] = ()
    frequency: tuple[str, ...] = ()
    grid_label: tuple[str, ...] = ()
    table_id: tuple[str, ...] = ()
    activity_id: tuple[str, ...] = ()
    nominal_resolution: tuple[str, ...] = ()
    realm: tuple[str, ...] = ()


class ESGFQueryCMIP7(_ESGFQueryEra):
    """CMIP7-native vocabulary (as CMIP6 but `branding_suffix` for `table_id`)."""

    _mip_era: ClassVar[str] = "CMIP7"
    project: tuple[str, ...] = ("CMIP7",)

    source_id: tuple[str, ...] = ()
    institution_id: tuple[str, ...] = ()
    experiment_id: tuple[str, ...] = ()
    variant_label: tuple[str, ...] = ()
    variable_id: tuple[str, ...] = ()
    frequency: tuple[str, ...] = ()
    grid_label: tuple[str, ...] = ()
    branding_suffix: tuple[str, ...] = ()
    activity_id: tuple[str, ...] = ()
    nominal_resolution: tuple[str, ...] = ()
    realm: tuple[str, ...] = ()
