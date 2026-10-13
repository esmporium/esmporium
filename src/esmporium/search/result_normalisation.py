"""
Flattening a stored raw search document into `{facet_name: value}`

This is a rare thing to need.
It is currently only used when we need to explain, from raw JSON,
which facet distinguishes datasets our model considers identical
(see [esmporium.db.dataset_uniqueness.facet_differences][]).
For example, when saving a dataset clashes with one already stored
(see [esmporium.db.save_dataset][])
or when datasets our model cannot tell apart are loaded.

It is currently used by the database layer, possibly long after the search,
so there is no live [SearchAPI][esmporium.search.apis.SearchAPI] in scope
and it cannot ask the API how to read its own format.
Instead each raw doc should be stored with a `raw_docs_format_tag`.
This string allows [normalise_stored_document][(m).] to dispatch to the right parser
on that tag through a registry of per-format flatteners.

The registry ([DEFAULT_NORMALISERS][(m).]) covers the search APIs we ship.
It is a parameter, so a user who bypasses our facade with their own search API
can inject the flattener for their own tag.
A tag with no registered flattener raises
[UnknownRawDocFormatTagError][(m).] rather than guessing.

Living here (in `search`) rather than in `db`
keeps the format knowledge out of the database layer:
`db` may import `search`, never the reverse,
so `db.facet_differences` receives an already-flat mapping
and never has to know which search API produced it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, TypeAlias

from esmporium.search.apis.protocol import UnreadableResponseError

SOLR_FORMAT_TAG = "solr"
"""The `raw_docs_format_tag` of our Solr search APIs (ESGF1 and the ESGF-1.5 bridge)."""

STAC_FORMAT_TAG = "stac"
"""The `raw_docs_format_tag` of our STAC search API (ESGF-NG)."""

NormaliseFunc = Callable[[dict[str, Any]], dict[str, Any]]
"""Flattens one raw document into `{facet_name: value}`. Keyed by `raw_docs_format_tag`."""  # noqa: E501


def _unreadable_stored_document(
    raw: Any, path: str, what: str
) -> UnreadableResponseError:
    """
    Build the error for a stored document we cannot read

    Parameters
    ----------
    raw
        The stored document we could not read

    path
        Where in `raw` we looked, as a dot-separated path

    what
        What we were trying to read

    Returns
    -------
    :
        The error to raise
    """
    lead = "This stored raw document is not shaped the way its format tag says it is."
    return UnreadableResponseError(
        # A document which is not a mapping cannot be reported on as one, so it is
        # wrapped in order to be shown at all.
        dict(raw) if isinstance(raw, Mapping) else {"document": raw},
        path,
        what=what,
        lead=lead,
    )


NormalisedDocument: TypeAlias = dict[str, Any]
"""
Normalised document

In this context, "normalised" means a basic mapping
from facet names to the values that they take
(removing any project/API prefixes and ensuring that values
are lists if multi-valued, single values otherwise).
"Document" means a single result from a search API.
This does not usually line up with our definition
of dataset or dataset version.
We simply want to normalise the documents we are given,
rather than trying to do any other data format conversion.
"""


def _normalise_solr(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Flatten a Solr document (ESGF1 / the ESGF-1.5 bridge)

    Solr puts facets at the top level, usually as single-element lists, e.g.
    `{"product": ["output1"]}`. Each such list is unwrapped to its scalar; anything
    that is not a single-element list is left as-is.

    Parameters
    ----------
    raw
        One raw Solr document, already parsed from its stored JSON

    Returns
    -------
    :
        The document's facet names with values mapped to scalar (or list)

    Raises
    ------
    UnreadableResponseError
        `raw` is not keyed by facet name at all,
        so there is nothing here to flatten
    """
    if not isinstance(raw, Mapping):
        raise _unreadable_stored_document(
            raw, "document", "the facets of this document"
        )

    return {
        key: value[0] if isinstance(value, list) and len(value) == 1 else value
        for key, value in raw.items()
    }


# TODO: return to this.
# Only flattening by property removes some 'facets' which solr retains
# But only need to be comparable to another STAC.
def _normalise_stac(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Flatten a STAC feature (ESGF-NG)

    STAC nests its facets inside `properties`, under project-prefixed keys, e.g.
    `{"properties": {"cmip7:activity_id": "ScenarioMIP"}}`. The `cmipN:` prefix is
    dropped and any single-element list is unwrapped, so the result is keyed the same
    way [_normalise_solr][(m).]'s is.

    Kept separate from [_normalise_solr][(m).] on purpose: the two search generations
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

    Raises
    ------
    UnreadableResponseError
        `raw`'s facets are not keyed by facet name,
        so there is nothing here to flatten
    """
    # A stored feature with no `properties` at all has no facets.
    # This is a different thing from a feature whose `properties` is not a mapping.
    # The first is an empty document,
    # the second (`properties` is not a mapping) is one we cannot read.
    properties = raw.get("properties", {}) if isinstance(raw, Mapping) else None
    if not isinstance(properties, Mapping):
        raise _unreadable_stored_document(
            raw, "properties", "the facets of this document"
        )

    flat: dict[str, Any] = {}
    for key, value in _normalise_stac_verbatim(raw).items():
        name = key.split(":", 1)[1] if ":" in key else key  # drop any `cmipN:` prefix
        flat[name] = value

    return flat


def _normalise_stac_verbatim(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Flatten a STAC feature without touching its keys

    [_normalise_stac][(m).] does two separable things: it gets to a scalar *value*,
    and it rewrites the *key*. This is the first without the second, so
    `{"properties": {"cmip7:activity_id": ["ScenarioMIP"]}}` flattens to
    `{"cmip7:activity_id": "ScenarioMIP"}` with the prefix intact.

    Getting to a scalar is not optional -- every Solr value is a list, so a flattener
    which skipped it would answer almost nothing -- but rewriting the key is, and for
    [CatalogueEntry.other_terms][esmporium.requirements.CatalogueEntry.other_terms] it
    must not happen: that mapping is matched against
    [Query.other_terms][esmporium.query.Query.other_terms], which is never translated
    either, so the name the user sent to the API is the name which has to match it
    back.

    Parameters
    ----------
    raw
        One raw STAC feature, already parsed from its stored JSON

    Returns
    -------
    :
        The feature's facets as a flat mapping of the document's own name to scalar
        (or list) value

    Raises
    ------
    UnreadableResponseError
        `raw`'s facets are not keyed by facet name,
        so there is nothing here to flatten
    """
    # A stored feature with no `properties` at all has no facets.
    # This is a different thing from a feature whose `properties` is not a mapping.
    # The first is an empty document,
    # the second (`properties` is not a mapping) is one we cannot read.
    properties = raw.get("properties", {}) if isinstance(raw, Mapping) else None
    if not isinstance(properties, Mapping):
        raise _unreadable_stored_document(
            raw, "properties", "the facets of this document"
        )

    return {
        key: value[0] if isinstance(value, list) and len(value) == 1 else value
        for key, value in properties.items()
    }


DEFAULT_NORMALISERS: Mapping[str, NormaliseFunc] = {
    SOLR_FORMAT_TAG: _normalise_solr,
    STAC_FORMAT_TAG: _normalise_stac,
}
"""
The flattener for each known search API format, keyed by its `raw_docs_format_tag`
"""

VERBATIM_NORMALISERS: Mapping[str, NormaliseFunc] = {
    SOLR_FORMAT_TAG: _normalise_solr,
    STAC_FORMAT_TAG: _normalise_stac_verbatim,
}
"""
Flatteners which keep each document's own facet names, keyed by `raw_docs_format_tag`

The same as [DEFAULT_NORMALISERS][(m).DEFAULT_NORMALISERS] except that a STAC feature
keeps its `cmipN:` prefixes. Solr needs no second entry: its documents are already
keyed by their own names, so the one flattener serves both.

Used to fill
[CatalogueEntry.other_terms][esmporium.requirements.CatalogueEntry.other_terms], which
has to answer under the name a user would have sent to the search API. Everything else
wants `DEFAULT_NORMALISERS`, because comparing a STAC document with a Solr one -- which
is what [esmporium.db.dataset_uniqueness.facet_differences][] does -- needs the two
keyed the same way.
"""


class UnknownRawDocFormatTagError(ValueError):
    """
    Raised when a stored raw doc's `raw_docs_format_tag` has no registered flattener
    """

    def __init__(self, tag: str, normalisers: Mapping[str, NormaliseFunc]) -> None:
        self.tag = tag
        self.known = tuple(sorted(normalisers))
        known = ", ".join(repr(name) for name in self.known) or "(none)"
        # Both mappings, deliberately. A reader who reaches this is being asked to
        # register a flattener, and a catalogue flattens each document twice: once
        # canonicalising its keys and once keeping them
        # (see [VERBATIM_NORMALISERS][(m).VERBATIM_NORMALISERS]). Naming only
        # `normalisers` sends them round the loop again -- they do exactly as told and
        # get this same error from the second pass, with nothing to say why.
        super().__init__(
            f"No flattener is registered for raw_docs_format_tag {tag!r}. "
            f"Known tags: {known}. If this document came from a search API you "
            "injected, register the tag in both of the mappings a reader takes: "
            "`normalisers={**DEFAULT_NORMALISERS, <your tag>: <your flattener>}` and "
            "`verbatim_normalisers={**VERBATIM_NORMALISERS, <your tag>: "
            "<your flattener>}`. The same flattener serves both unless your format "
            "puts a prefix on its facet names, which is the only thing the two "
            "built-in mappings differ on."
        )


def normalise_stored_document(
    raw: dict[str, Any],
    raw_docs_format_tag: str,
    normalisers: Mapping[str, NormaliseFunc] = DEFAULT_NORMALISERS,
) -> NormalisedDocument:
    """
    Flatten a stored raw search document based on its recorded format tag

    Parameters
    ----------
    raw
        One raw search document, already parsed from its stored JSON

    raw_docs_format_tag
        See [DatasetRawDoc.raw_docs_format_tag][esmporium.db.schema.DatasetRawDoc])

    normalisers
        The flattener to use for each tag.

    Returns
    -------
    :
        The document's facets as a flat mapping of unprefixed name to scalar (or list)
        value, ready for [esmporium.db.dataset_uniqueness.facet_differences][]

    Raises
    ------
    UnknownRawDocFormatTagError
        `raw_docs_format_tag` has no flattener in `normalisers`
    """
    if raw_docs_format_tag not in normalisers:
        raise UnknownRawDocFormatTagError(raw_docs_format_tag, normalisers)

    return normalisers[raw_docs_format_tag](raw)
