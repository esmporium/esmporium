"""
Flattening a stored raw search document into `{facet_name: value}`

This is a rare, off-the-write-path diagnostic. It only runs when a dataset clash is
observed at load time and we need to explain, from the raw JSON we stored at ingest,
which facet distinguishes two datasets our model considers identical (see
[`esmporium.db.dataset_uniqueness.facet_differences`][]).

It runs long after the search, with no live
[`SearchAPI`][esmporium.search.apis.SearchAPI] in scope, so it cannot ask the API how to
read its own format. Instead each raw doc is stored with a `search_api_tag` -- a small
string the producing search API stamps on it at ingest (see
[`SearchAPI.search_api_tag`][esmporium.search.apis.SearchAPI]) -- and
[`normalise_stored_document`][(m).] dispatches on that tag through a registry of
per-format flatteners. No shape sniffing: the format is recorded, not guessed.

The registry ([`DEFAULT_NORMALISERS`][(m).]) covers the search APIs we ship. It is a
parameter, so a user who bypasses our facade with their own search API can inject the
flattener for their own tag; a tag with no registered flattener raises
[`UnknownRawDocFormatTagError`][(m).] rather than guessing.

Living here (in `search`) rather than in `db` keeps the format knowledge out of the
database layer: `db` may import `search`, never the reverse, so `db.facet_differences`
receives an already-flat mapping and never has to know which search API produced it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, TypeAlias

SOLR_FORMAT_TAG = "solr"
"""The `search_api_tag` of our Solr search APIs (ESGF1 and the ESGF-1.5 bridge)."""

STAC_FORMAT_TAG = "stac"
"""The `search_api_tag` of our STAC search API (ESGF-NG)."""

NormaliseFunc = Callable[[dict[str, Any]], dict[str, Any]]
"""Flattens one raw document into `{facet_name: value}`. Keyed by `search_api_tag`."""

NormalisedDocument: TypeAlias = dict[str, Any]
"""Document normalised based on search API (e.g. removes prefix and list)"""


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


DEFAULT_NORMALISERS: Mapping[str, NormaliseFunc] = {
    SOLR_FORMAT_TAG: _normalise_solr,
    STAC_FORMAT_TAG: _normalise_stac,
}
"""
The flattener for each search API we ship, keyed by its `search_api_tag`

Passed as the default to [`normalise_stored_document`][(m).]. To handle a document from
a search API you injected, pass your own mapping -- to keep ours as well as yours,
merge: `{**DEFAULT_NORMALISERS, your_tag: your_flattener}`.
"""


class UnknownRawDocFormatTagError(ValueError):
    """
    Raised when a stored raw doc's `search_api_tag` has no registered flattener

    Every raw document is stored with the tag of the search API that produced it. If we
    are asked to normalise one whose tag is not in the registry, we do not guess: either
    the document came from an injected search API whose flattener was not supplied, or a
    tag was stored that nothing can read.
    """

    def __init__(self, tag: str, normalisers: Mapping[str, NormaliseFunc]) -> None:
        self.tag = tag
        self.known = tuple(sorted(normalisers))
        known = ", ".join(repr(name) for name in self.known) or "(none)"
        super().__init__(
            f"No flattener is registered for search_api_tag {tag!r}. "
            f"Known tags: {known}. If this document came from a search API you "
            "injected, pass a `normalisers` mapping that includes this tag, e.g. "
            "{**DEFAULT_NORMALISERS, <your tag>: <your flattener>}."
        )


def normalise_stored_document(
    raw: dict[str, Any],
    search_api_tag: str,
    normalisers: Mapping[str, NormaliseFunc] = DEFAULT_NORMALISERS,
) -> NormalisedDocument:
    """
    Flatten a stored raw search document, dispatching on its recorded format tag

    Parameters
    ----------
    raw
        One raw search document, already parsed from its stored JSON

    search_api_tag
        The tag stored alongside the document (see
        [`DatasetRawDoc.search_api_tag`][esmporium.db.schema.DatasetRawDoc]), naming the
        format the producing search API returned

    normalisers
        The flattener to use for each tag. Defaults to [`DEFAULT_NORMALISERS`][(m).],
        the search APIs we ship; pass your own (merged with the default) to handle a
        document from a search API you injected.

    Returns
    -------
    :
        The document's facets as a flat mapping of unprefixed name to scalar (or list)
        value, ready for [`esmporium.db.dataset_uniqueness.facet_differences`][]

    Raises
    ------
    UnknownRawDocFormatTagError
        `search_api_tag` has no flattener in `normalisers`
    """
    if search_api_tag not in normalisers:
        raise UnknownRawDocFormatTagError(search_api_tag, normalisers)

    return normalisers[search_api_tag](raw)
