"""
Maps the translation to/from canonical language to/from project-specific language.

One [`ProjectProfile`][esmporium.esgf.project_translation_maps.ProjectProfile] per
project holds the facet language needed to (a) translate facet names to canonical
vocabulary and (b) render [`CanonicalQuery`][esmporium.esgf.canonical.CanonicalQuery]
back to the specified project's native facet names.

Both directions are driven by a single `field_map` (canonical -> native) per project
There is deliberately no `X -> Y` translator anywhere; a cross-project journey is
composed through the canonical hub.
"""

from collections.abc import Iterable

from pydantic import BaseModel

from esmporium.esgf.canonical import CANONICAL_FACETS, CanonicalQuery


class UnknownProjectError(ValueError):
    """Raised when a query targets a project that has no profile (e.g CMIP4)."""

    def __init__(self, project: str, known: Iterable[str]) -> None:
        self.project = project
        known_projects = ", ".join(sorted(known))
        super().__init__(
            f"No profile for project {project!r}; known projects: {known_projects}"
        )


class FacetNotRepresentableError(ValueError):
    """
    Raised when a facet in a query cannot be expressed in a requested project.

    This is the fail-loud contract: rather than silently dropping a facet a target
    project has no place for (e.g. `grid_label` for CMIP5, or a CMIP5 `product` sent
    to CMIP6), we stop and tell the user, so they decide how to adjust the query.

    Note that the fail-loud contract does not include potential errors in
    `extra_facets`. `extra_facets` will always pass and if this includes incorrect
    facets, this will return no search results from the ESGF search API.
    """

    def __init__(self, facet: str, project: str) -> None:
        self.facet = facet
        self.project = project
        super().__init__(f"facet {facet!r} cannot be represented in {project}")


class ProjectProfile(BaseModel):
    """
    Data to describe a project's facet names. No translation here.
    """

    model_config = {"frozen": True}

    project: str
    """The profile's registry key, selected by a query's `project` field. For
    example, `CMIP5`."""

    project_param: str
    """
    The value written into the ESGF `project` search param when rendering to this
    project.

    Used only at the render stage (`to_native_params`), whose final step is
    `params["project"] = self.project_param`. It is kept separate from `project`
    (our registry key) because that token is ESGF's to define, not ours: today the
    two coincide (both `CMIP5`), but decoupling means ESGF renaming a project does
    not disturb our lookup key.
    """

    field_map: dict[str, str]
    """
    Renamed facets only that require mapping:  Canonical name -> this project's
    native name.

    Facets whose native name is identical to the canonical name (e.g.
    `realm`, `grid_label`) are omitted here and render as-is via `native_facet`.
    """

    absent_facets: frozenset[str]
    """Canonical facets this project does not have (drives the fail-loud gate)."""

    project_specific_facets: frozenset[str]
    """
    Native facet names this project owns that have no canonical equivalent.

    For example, CMIP5 `product`. Used to tell a project-specific facet that belongs
    to this project (emit it) from one that belongs to a different project (fail loud).
    """

    def native_facet(self, canonical: str) -> str:
        """Map a canonical name to the requested project's native name.

        Returns the shared/identity name when not renamed (such as `realm`).
        """
        return self.field_map.get(canonical, canonical)

    def canonical_facet(self, native: str) -> str | None:
        """
        Map this project's native name to a canonical name (`None` if there is none).

        `None` means the native facet is project-specific or otherwise
        unknown to the canonical vocabulary, and so belongs in the `extra_facets`
        rather than on a canonical field.
        """
        inverse = {
            native_name: canonical for canonical, native_name in self.field_map.items()
        }
        if native in inverse:
            return inverse[native]
        # An identity facet: the native spelling is the canonical name and this
        # project does not rename it (covers facets like `realm`).
        if native in CANONICAL_FACETS and native not in self.field_map:
            return native
        return None

    def can_represent(
        self, facet: str, *, known_project_specific: frozenset[str]
    ) -> bool:
        """
        Whether this project can express `facet` (canonical or shared, e.g. `realm`).

        - A canonical facet is representable unless it is in `absent_facets`.
        - A passthrough facet that some project owns (`known_project_specific`) is
          representable only if this project owns it.
        - A passthrough facet no project owns is treated as best-effort and allowed
          through (the `other_terms` escape hatch).
        """
        if facet in CANONICAL_FACETS:
            return facet not in self.absent_facets
        if facet in known_project_specific:
            return facet in self.project_specific_facets
        return True

    def to_native_params(self, canonical: CanonicalQuery) -> dict[str, str]:
        """
        Render a canonical query to the requested project's native params, or fail loud.

        Canonical facets are renamed via `field_map`; passthrough `extra_facets`
        are sent through as-is. Values within a facet
        are comma-joined (ESGF's OR syntax) and the `project` selector is set last.
        """
        known_project_specific = known_project_specific_facets()
        params: dict[str, str] = {}

        # Canonical facets (renamed). Sorted for deterministic output.
        for facet in sorted(CANONICAL_FACETS):
            values = getattr(canonical, facet)
            if not values:
                continue
            if not self.can_represent(
                facet, known_project_specific=known_project_specific
            ):
                raise FacetNotRepresentableError(facet, self.project)
            params[self.native_facet(facet)] = ",".join(values)

        # Passthrough facets (emitted under their native names, as-is).
        for facet, values in canonical.extra_facets.items():
            if not values:
                continue
            if not self.can_represent(
                facet, known_project_specific=known_project_specific
            ):
                raise FacetNotRepresentableError(facet, self.project)
            params[facet] = ",".join(values)

        params["project"] = self.project_param
        return params


CMIP5_PROFILE = ProjectProfile(
    project="CMIP5",
    project_param="CMIP5",
    field_map={
        "institution": "institute",
        "variant_label": "ensemble",
        "reporting_interval": "time_frequency",
        "processing_id": "cmor_table",
        # model / experiment / variable / realm are identical names -> omitted.
    },
    # CMIP5 has no activity, resolution, or grid concept.
    absent_facets=frozenset({"activity", "resolution", "grid_label"}),
    project_specific_facets=frozenset({"product"}),
)

CMIP6_PROFILE = ProjectProfile(
    project="CMIP6",
    project_param="CMIP6",
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
    project_specific_facets=frozenset({"sub_experiment_id"}),
)

CMIP7_PROFILE = ProjectProfile(
    project="CMIP7",
    project_param="CMIP7",
    field_map={
        "model": "source_id",
        "institution": "institution_id",
        "experiment": "experiment_id",
        "variable": "variable_id",
        "reporting_interval": "frequency",
        "processing_id": "branding_suffix",
        "activity": "activity_id",
        "resolution": "nominal_resolution",
        # variant_label / grid_label / realm are identical names -> omitted.
    },
    absent_facets=frozenset(),
    project_specific_facets=frozenset(
        {"temporal_label", "vertical_label", "horizontal_label", "area_label", "region"}
    ),
)

IDENTITY_PROFILE = ProjectProfile(
    project="unified",
    project_param="unified",
    # Empty field_map: every native name that is a canonical facet lowers to
    # itself, and anything else lowers to None (extra_facets). This makes
    # `canonical_facet` behave as the identity on the canonical vocabulary, which
    # is exactly what the unified skin needs.
    field_map={},
    absent_facets=frozenset(),
    project_specific_facets=frozenset(),
)
"""
The lowering profile for the unified, neutral-vocabulary skin.

The unified skin already types in canonical names, so lowering it is the identity
on the canonical vocabulary. Rather than special-casing that skin, it carries this
profile like any other dialect, so every skin lowers by the same rule.

Not registered in `_PROFILE_REGISTRY`: it is a *lowering* profile only (its
`project`/`project_param` are never rendered), so `get_profile` and the
fail-loud rules never see it.
"""

_PROFILE_REGISTRY: dict[str, ProjectProfile] = {
    profile.project: profile
    for profile in (CMIP5_PROFILE, CMIP6_PROFILE, CMIP7_PROFILE)
}


def get_profile(project: str) -> ProjectProfile:
    """Look up the profile for a project, raising a helpful error if unknown."""
    try:
        return _PROFILE_REGISTRY[project]
    except KeyError:
        raise UnknownProjectError(project, _PROFILE_REGISTRY) from None


def known_project_specific_facets() -> frozenset[str]:
    """
    Return the union of every project's `project_specific_facets`.

    A passthrough facet in this set is known to belong to *some* project, so it is
    subject to the fail-loud rule; one not in this set is an unmodelled
    `other_terms` facet and is passed through best-effort.
    """
    return frozenset().union(
        *(p.project_specific_facets for p in _PROFILE_REGISTRY.values())
    )
