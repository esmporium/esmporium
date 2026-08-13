"""
Era profiles — the declarative out-adapters (the "M" side of hub-and-spoke).

One [`EraProfile`][esmporium.esgf.mip_translation.EraProfile] per MIP era holds
the pure data needed to (a) lower a dialect's native facet names *up* to the
canonical vocabulary and (b) render a
[`CanonicalQuery`][esmporium.esgf.canonical.CanonicalQuery] back *down* to that
era's native facet names.

Both directions are driven by a single `field_map` (canonical -> native) per era:
the forward map renders out, the derived inverse lowers in. There is deliberately
no `X -> Y` translator anywhere; a cross-era journey is composed through the
canonical hub.

Scope for this iteration is facet translation only. The search/ingestion fields
from the wider design (`supported_flavours`, `multi_variable`, `reconstruct`,
`parent_strategy`, ...) are intentionally absent and will be added when we wire up
searching.
"""

from collections.abc import Iterable

from pydantic import BaseModel

from esmporium.esgf.canonical import CANONICAL_FACETS, CanonicalQuery


class UnknownEraError(ValueError):
    """Raised when a query targets an era that has no profile."""

    def __init__(self, mip_era: str, known: Iterable[str]) -> None:
        self.mip_era = mip_era
        known_eras = ", ".join(sorted(known))
        super().__init__(f"No era profile for {mip_era!r}; known eras: {known_eras}")


class FacetNotRepresentableError(ValueError):
    """
    Raised when a facet in a query cannot be expressed in a requested era.

    This is the fail-loud contract: rather than silently dropping a facet a target
    era has no place for (e.g. `grid_label` for CMIP5, or a CMIP5 `product` sent to
    CMIP6), we stop and tell the user, so *they* decide how to adjust the query.
    """

    def __init__(self, facet: str, mip_era: str) -> None:
        self.facet = facet
        self.mip_era = mip_era
        super().__init__(f"facet {facet!r} cannot be represented in {mip_era}")


class EraProfile(BaseModel):
    """
    Pure data (plus tiny rename helpers) describing one MIP era's facet names.
    """

    model_config = {"frozen": True}

    mip_era: str
    """The era key, selected by a query's `project` field. For example, `CMIP5`."""

    project_facet: str
    """The value emitted for the `project` param when rendering to this era."""

    field_map: dict[str, str]
    """
    Canonical name -> this era's native name, for renamed (category-1) facets only.

    Facets whose native name is identical to the canonical name (category-2, e.g.
    `realm`, `grid_label`) are omitted here and render as-is via `native_facet`.
    """

    absent_facets: frozenset[str]
    """Canonical facets this era does not have (drives the fail-loud gate)."""

    era_specific_facets: frozenset[str]
    """
    Native facet names this era owns that have no canonical equivalent (category 3).

    For example, CMIP5 `product`. Used to tell an era-specific facet that belongs
    to *this* era (emit it) from one that belongs to a *different* era (fail loud).
    """

    def native_facet(self, canonical: str) -> str:
        """Canonical name -> this era's native name (identity if not renamed)."""
        return self.field_map.get(canonical, canonical)

    def canonical_facet(self, native: str) -> str | None:
        """
        Map this era's native name to a canonical name (`None` if there is none).

        `None` means the native facet is category-3 (era-specific) or otherwise
        unknown to the canonical vocabulary, and so belongs in the passthrough
        bucket rather than on a canonical field.
        """
        inverse = {
            native_name: canonical for canonical, native_name in self.field_map.items()
        }
        if native in inverse:
            return inverse[native]
        # An identity facet: the native spelling *is* the canonical name and this
        # era does not rename it (covers category-2 facets like `realm`).
        if native in CANONICAL_FACETS and native not in self.field_map:
            return native
        return None

    def can_represent(self, facet: str, *, known_era_specific: frozenset[str]) -> bool:
        """
        Whether this era can express `facet` (a canonical *or* passthrough name).

        - A canonical facet is representable unless it is in `absent_facets`.
        - A passthrough facet that some era owns (`known_era_specific`) is
          representable only if *this* era owns it.
        - A passthrough facet no era owns is treated as best-effort and allowed
          through (the `other_terms` escape hatch).
        """
        if facet in CANONICAL_FACETS:
            return facet not in self.absent_facets
        if facet in known_era_specific:
            return facet in self.era_specific_facets
        return True

    def to_native_params(self, canonical: CanonicalQuery) -> dict[str, str]:
        """
        Render a canonical query to this era's native params, or fail loud.

        Canonical facets are renamed via `field_map`; passthrough `extra_facets`
        are emitted as-is (subject to the fail-loud rule). Values within a facet
        are comma-joined (ESGF's OR syntax) and the `project` selector is set last.
        """
        known_era_specific = known_era_specific_facets()
        params: dict[str, str] = {}

        # Canonical facets (renamed). Sorted for deterministic output.
        for facet in sorted(CANONICAL_FACETS):
            values = getattr(canonical, facet)
            if not values:
                continue
            if not self.can_represent(facet, known_era_specific=known_era_specific):
                raise FacetNotRepresentableError(facet, self.mip_era)
            params[self.native_facet(facet)] = ",".join(values)

        # Passthrough facets (emitted under their native names, as-is).
        for facet, values in canonical.extra_facets.items():
            if not values:
                continue
            if not self.can_represent(facet, known_era_specific=known_era_specific):
                raise FacetNotRepresentableError(facet, self.mip_era)
            params[facet] = ",".join(values)

        params["project"] = self.project_facet
        return params


CMIP5_PROFILE = EraProfile(
    mip_era="CMIP5",
    project_facet="CMIP5",
    field_map={
        "institution": "institute",
        "variant_label": "ensemble",
        "reporting_interval": "time_frequency",
        "processing_id": "cmor_table",
        # model / experiment / variable / realm are identical names -> omitted.
    },
    # CMIP5 has no activity, resolution, or grid concept.
    absent_facets=frozenset({"activity", "resolution", "grid_label"}),
    era_specific_facets=frozenset({"product"}),
)

CMIP6_PROFILE = EraProfile(
    mip_era="CMIP6",
    project_facet="CMIP6",
    field_map={
        "model": "source_id",
        "institution": "institution_id",
        "experiment": "experiment_id",
        "variable": "variable_id",
        "reporting_interval": "frequency",
        "processing_id": "table_id",
        "activity": "activity_id",
        "resolution": "nominal_resolution",
        # variant_label / grid_label / realm are identical names -> omitted.
    },
    absent_facets=frozenset(),
    era_specific_facets=frozenset({"sub_experiment_id"}),
)

CMIP7_PROFILE = EraProfile(
    mip_era="CMIP7",
    project_facet="CMIP7",
    field_map={
        "model": "source_id",
        "institution": "institution_id",
        "experiment": "experiment_id",
        "variable": "variable_id",
        "reporting_interval": "frequency",
        # CMIP7 replaced CMIP6's monolithic table_id with a composite branding_suffix.
        "processing_id": "branding_suffix",
        "activity": "activity_id",
        "resolution": "nominal_resolution",
        # variant_label / grid_label / realm are identical names -> omitted.
    },
    absent_facets=frozenset(),
    # The components that build the branding_suffix, plus region, are category-3.
    era_specific_facets=frozenset(
        {"temporal_label", "vertical_label", "horizontal_label", "area_label", "region"}
    ),
)


_PROFILE_REGISTRY: dict[str, EraProfile] = {
    profile.mip_era: profile
    for profile in (CMIP5_PROFILE, CMIP6_PROFILE, CMIP7_PROFILE)
}


def get_profile(mip_era: str) -> EraProfile:
    """Look up the profile for an era, raising a helpful error if unknown."""
    try:
        return _PROFILE_REGISTRY[mip_era]
    except KeyError:
        raise UnknownEraError(mip_era, _PROFILE_REGISTRY) from None


def known_era_specific_facets() -> frozenset[str]:
    """
    Return the union of every era's `era_specific_facets`.

    A passthrough facet in this set is known to belong to *some* era, so it is
    subject to the fail-loud rule; one not in this set is an unmodelled
    `other_terms` facet and is passed through best-effort.
    """
    return frozenset().union(
        *(p.era_specific_facets for p in _PROFILE_REGISTRY.values())
    )
