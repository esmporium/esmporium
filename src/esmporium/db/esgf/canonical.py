"""
The canonical intermediate representation (IR) — the hub of hub-and-spoke.

Every input dialect (see [`query_models`][esmporium.db.esgf.query_models]) lowers
into a [`CanonicalQuery`][esmporium.db.esgf.canonical.CanonicalQuery], and every
era profile (see [`mip_translation`][esmporium.db.esgf.mip_translation]) renders a
`CanonicalQuery` back out to that era's native facet names. A journey
`dialect X -> era Y` is therefore *composed* as `render_Y(to_canonical(X))`; there
is never a dedicated `X -> Y` translator.

This module is the bottom of the dependency chain: it imports nothing from its
siblings and knows nothing about eras, endpoints, dialects, or value equivalence.
In particular it *never* rewrites facet **values** (`rcp45` is never turned into
`ssp245`): names are ours to translate, values are the user's.
"""

from collections.abc import Collection
from typing import Any

from pydantic import BaseModel, field_validator

CANONICAL_FACETS: frozenset[str] = frozenset(
    {
        "model",
        "institution",
        "experiment",
        "variable",
        "variant_label",
        "reporting_interval",
        "processing_id",
        "activity",
        "resolution",
        "grid_label",
        "realm",
    }
)
"""
The neutral facet vocabulary that the IR names first-class.

These are the facets that are *shared* across MIP eras, in either of two senses:

- category 1 (renamed): the same concept has a different native name per era,
  e.g. canonical `model` is `source_id` in CMIP6/7 but `model` in CMIP5. Each era
  profile carries a `field_map` entry for these.
- category 2 (universal): the same concept has the *same* native name in every
  era it exists in, e.g. `realm`, `grid_label`. These need no `field_map` entry;
  they render as-is.

Era-specific facets with no cross-era equivalent (category 3, e.g. CMIP5
`product`) are deliberately *not* here. They travel through `extra_facets`.

Adding a facet here means adding a matching field to `CanonicalQuery` below;
this is checked by the structural-invariant tests.
"""


def _normalise_facet_values(value: object) -> tuple[str, ...]:
    """
    Coerce a facet input to a tuple of values.

    A single string becomes a one-tuple (`"tas" -> ("tas",)`) rather than being
    treated as an iterable of characters. `None` becomes the empty tuple, i.e.
    "this facet was not set". Any other collection is turned into a tuple as-is.

    Anything else (e.g. a bare `int`) is wrapped in a one-tuple and left for
    pydantic to reject against the `tuple[str, ...]` annotation, so the user gets
    a clear "input should be a valid string" error naming the field.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Collection):
        return tuple(value)
    return (value,)  # type: ignore[return-value]  # non-str: pydantic rejects it


class CanonicalQuery(BaseModel):
    """
    An immutable query expressed purely in the canonical facet vocabulary.

    This is the IR: the single neutral form that every dialect lowers into and
    that every era renders out of. It is internal — users construct one of the
    dialect skins in [`query_models`][esmporium.db.esgf.query_models], not this.

    Facet fields are tuples: the values within a facet are OR-ed, different facets
    are AND-ed. An empty tuple means the facet was not set.
    """

    # Immutable: once lowered, the IR is a stable value the render side can rely
    # on. Mirrors the note in item 1 of the design that the IR is immutable.
    model_config = {"frozen": True}

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

    extra_facets: dict[str, tuple[str, ...]] = {}
    """
    Passthrough bucket for facets the canonical vocabulary does not name.

    This holds two kinds of facet, distinguished only at render time by the era
    profiles (see [`mip_translation`][esmporium.db.esgf.mip_translation]):

    - category-3 era-specific facets a dialect skin declared but could not map to
      a canonical name (e.g. CMIP5 `product`), and
    - `other_terms` the user injected for facets we have not modelled yet.

    Keys are native facet names, values are OR-ed tuples.
    """

    source_spec: dict[str, Any] = {}
    """
    The original query, as typed, retained for auditing and reconstruction.

    Recorded by each skin's `to_canonical()` so that nothing the user asked for is
    ever lost from the record, even when a later era render refuses a facet under
    the fail-loud rule. JSON-serialisable; holds no live skin object.

    Shape: `{"dialect": <mip era or "unified">, "facets": {...}, "other_terms": {...}}`.
    """

    @field_validator(
        "model",
        "institution",
        "experiment",
        "variable",
        "variant_label",
        "reporting_interval",
        "processing_id",
        "activity",
        "resolution",
        "grid_label",
        "realm",
        mode="before",
    )
    @classmethod
    def _facets_as_tuple(cls, value: object) -> tuple[str, ...]:
        return _normalise_facet_values(value)

    @field_validator("extra_facets", mode="before")
    @classmethod
    def _extra_facet_values_as_tuple(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value  # let pydantic raise the usual "not a dict" error
        return {key: _normalise_facet_values(val) for key, val in value.items()}

    def to_params(self) -> dict[str, str]:
        """
        Render the *canonical* facets to a param dict, with no era logic.

        Only facets that are set are included. Values within a facet are joined
        with commas (ESGF's OR syntax). This is the neutral param view; the
        passthrough `extra_facets` are handled by the era profiles at render time,
        not here.
        """
        params: dict[str, str] = {}
        for facet in sorted(CANONICAL_FACETS):
            values = getattr(self, facet)
            if values:
                params[facet] = ",".join(values)
        return params

    def as_spec(self) -> dict[str, Any]:
        """
        Return a JSON-serialisable snapshot of the whole query, for storing/auditing.

        Includes the canonical facets, the `extra_facets` passthrough, and the
        retained `source_spec` original.
        """
        return self.model_dump(mode="json")
