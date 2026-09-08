"""
Flattening a stored raw search document into `{facet_name: value}`

This is a rare, off-the-write-path diagnostic. It only runs when a dataset clash is
observed at load time and we need to explain, from the raw JSON we stored at ingest,
which facet distinguishes two datasets our model considers identical (see
[`esmporium.db.dataset_uniqueness.facet_differences`][]).

Because it runs long after the search, with no live facade in scope, it works off the
stored document's *shape* rather than any generation flag: a STAC feature nests its
facets under `properties` with `cmipN:`-prefixed keys, while a Solr document puts them
at the top level, usually as single-element lists. [`normalise_stored_document`][(m).]
is a two-line router that sniffs that shape and dispatches to one of two deliberately
**uncoupled** flatteners -- [`_normalise_solr`][(m).] and [`_normalise_stac`][(m).] --
so the Solr and STAC handling never share code and can drift independently.

Living here (in `search`) rather than in `db` keeps the generation knowledge out of the
database layer: `db` may import `search`, never the reverse, so `db.facet_differences`
receives an already-flat mapping and never has to know which search API produced it.
"""

from __future__ import annotations

from typing import Any


def _normalise_solr(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Flatten a Solr document (ESGF1 / the ESGF-1.5 bridge)

    Solr puts facets at the top level, usually as single-element lists, e.g.
    `{"product": ["output1"]}`. Each such list is unwrapped to its scalar; anything
    that is not a single-element list is left as-is.

    Kept separate from [`_normalise_stac`][(m).] on purpose: the two search generations
    are handled by independent code so neither is coupled to the other's shape.

    Parameters
    ----------
    raw
        One raw Solr document, already parsed from its stored JSON

    Returns
    -------
    :
        The document's facets as a flat mapping of name to scalar (or list) value
    """
    return {
        key: value[0] if isinstance(value, list) and len(value) == 1 else value
        for key, value in raw.items()
    }


def _normalise_stac(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Flatten a STAC feature (ESGF-NG)

    STAC nests its facets inside `properties`, under project-prefixed keys, e.g.
    `{"properties": {"cmip7:activity_id": "ScenarioMIP"}}`. The `cmipN:` prefix is
    dropped and any single-element list is unwrapped, so the result is keyed the same
    way [`_normalise_solr`][(m).]'s is.

    Kept separate from [`_normalise_solr`][(m).] on purpose: the two search generations
    are handled by independent code so neither is coupled to the other's shape.

    Parameters
    ----------
    raw
        One raw STAC feature, already parsed from its stored JSON

    Returns
    -------
    :
        The feature's facets as a flat mapping of unprefixed name to scalar (or list)
        value
    """
    properties = raw.get("properties", {})
    flat: dict[str, Any] = {}
    for key, value in properties.items():
        name = key.split(":", 1)[1] if ":" in key else key  # drop any `cmipN:` prefix
        flat[name] = value[0] if isinstance(value, list) and len(value) == 1 else value

    return flat


def normalise_stored_document(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Flatten a stored raw search document, picking the flattener by its shape

    Routes to [`_normalise_stac`][(m).] for a STAC feature (it carries a `properties`
    key) and to [`_normalise_solr`][(m).] otherwise. The two search generations that
    Solr covers (ESGF1 and the ESGF-1.5 bridge) share a shape and a flattener, so this
    only has to tell Solr from STAC, which the presence of `properties` settles.

    Parameters
    ----------
    raw
        One raw search document, already parsed from its stored JSON

    Returns
    -------
    :
        The document's facets as a flat mapping of unprefixed name to scalar (or list)
        value, ready for [`esmporium.db.dataset_uniqueness.facet_differences`][]
    """
    if "properties" in raw:
        return _normalise_stac(raw)

    return _normalise_solr(raw)
