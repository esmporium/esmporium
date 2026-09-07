"""
Explaining why two datasets that look identical to us are actually different

When two datasets share every column our [`Dataset`][esmporium.db.schema.Dataset] model
records but have different `id_project_specific` values, they are distinguished by some
project-specific facet we do not model as a column: `product` for CMIP5, `activity_id`
for CMIP6, and (for CMIP7) things like `activity_id`, `region` or the branding labels.

Rather than hard-code any of those names, or try to split the native id on `.` (model
names contain dots, so that is unsafe), we read the facet name and values straight out
of the raw search documents we stored.

The flattening in `_normalise` deliberately lives here, not in the search facade. The
facade's readers are polymorphic over a live search API and only know how to pull the
facets we model as columns; this diagnostic needs *every* facet, runs against the stored
raw JSON with no facade in scope, and the two clashing documents can even come from
different search generations. A generic, shape-based flattener is the right tool here.

[`all_facet_differences`][] is the unfiltered bottom layer (every facet that differs);
[`facet_differences`][] is the higher layer that keeps only the id-linked facet the
load/clash-resolution flow ("which product did you mean?") is built on.
"""

# TODO: for facet differences also list id_project_specific?
# Or just leave this to future higher level function which identifies to the user
# where the clashes are
from __future__ import annotations

from typing import Any


class _Missing:
    """Sentinel for a facet present in one document but absent from the other."""

    def __repr__(self) -> str:
        return "<absent>"


MISSING = _Missing()
"""Marks the absent side when a facet appears in only one of two compared documents."""


# Let's push this into _ingest_document
# and add `get_normalised_facets` as a method on search API or facade
# and add `normalised_facets` or something as a column of DatasetRawDoc
# so this normalisation step is done when we know about facets
# and we don't have this leaking of our facade.
def _normalise(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Flatten one raw search document to `{facet_name: scalar_value}`

    Handles both search generations:

    - Solr (ESGF1 / the ESGF-1.5 bridge) puts facets at the top level, usually as
      single-element lists, e.g. `{"product": ["output1"]}`.
    - STAC (ESGF-NG) puts them inside `properties`, under project-prefixed keys, e.g.
      `{"properties": {"cmip7:activity_id": "ScenarioMIP"}}`.

    Parameters
    ----------
    raw
        One raw document, already parsed from its stored JSON

    Returns
    -------
    :
        The document's facets as a flat mapping of unprefixed name to scalar value
    """
    source = raw.get("properties", raw)  # STAC nests facets; Solr does not
    flat: dict[str, Any] = {}
    for key, value in source.items():
        name = key.split(":", 1)[1] if ":" in key else key  # drop any `cmipN:` prefix
        flat[name] = value[0] if isinstance(value, list) and len(value) == 1 else value

    return flat


def all_facet_differences(
    raw_a: dict[str, Any],
    raw_b: dict[str, Any],
) -> dict[str, tuple[Any, Any]]:
    """
    List every facet that differs between two raw documents

    The unfiltered bottom layer. Each document is flattened with `_normalise` first, so
    a single-element list (`["output1"]`) and its scalar (`"output1"`), or a
    `cmipN:`-prefixed STAC key and its unprefixed name, are never reported as spurious
    differences. Every real difference is returned — the id-linked facet (`product`,
    `activity_id`, ...) plus anything else that differs: `version`, `data_node`,
    `replica`, urls, timestamps.

    A facet present in only one document is reported with `MISSING` on the absent side.

    Higher layers (e.g. `facet_differences`) decide which of these differences matter to
    the user; this one decides nothing.

    Parameters
    ----------
    raw_a, raw_b
        The two raw documents, already parsed from their stored JSON

    Returns
    -------
    :
        `{facet_name: (value_in_a, value_in_b)}` for every facet whose values differ.
        `MISSING` stands in for a facet absent from one document.
    """
    facets_a = _normalise(raw_a)
    facets_b = _normalise(raw_b)

    differences: dict[str, tuple[Any, Any]] = {}
    for name in facets_a.keys() | facets_b.keys():
        value_a = facets_a.get(name, MISSING)
        value_b = facets_b.get(name, MISSING)
        if value_a != value_b:
            differences[name] = (value_a, value_b)

    return differences


def facet_differences(
    raw_a: dict[str, Any],
    raw_b: dict[str, Any],
    id_project_specific_a: str,
    id_project_specific_b: str,
) -> dict[str, tuple[Any, Any]]:
    """
    Find the facets that explain why two native ids differ

    Both documents describe datasets we consider identical (same values in every column
    our model records), yet their `id_project_specific` differs. This returns the facet
    name(s) and the two values behind that difference, read from the raw documents. It
    is the higher layer over `all_facet_differences`, keeping only the id-linked facets.

    A differing facet counts only if its value appears **inside** each document's
    `id_project_specific` but is not the whole id. That is what ties the facet to the
    id difference, and it drops fields that also differ but are not identity
    (`version`, `data_node`, download URLs, timestamps) — none of those appear in the
    native id (the `master_id`). It does this without splitting the id on `.`.

    Parameters
    ----------
    raw_a, raw_b
        The two raw documents, already parsed from their stored JSON

    id_project_specific_a, id_project_specific_b
        The native id of each document's dataset (e.g. the CMIP5/6 `master_id`)

    Returns
    -------
    :
        `{facet_name: (value_in_a, value_in_b)}` for each distinguishing facet. Empty
        if nothing in the raw documents explains the id difference.

    Examples
    --------
    >>> facet_differences(
    ...     {"product": ["output1"]},
    ...     {"product": ["output2"]},
    ...     "cmip5.output1.CMCC.CMCC-CM.piControl.mon.atmos.Amon.r1i1p1",
    ...     "cmip5.output2.CMCC.CMCC-CM.piControl.mon.atmos.Amon.r1i1p1",
    ... )
    {'product': ('output1', 'output2')}
    """
    differences: dict[str, tuple[Any, Any]] = {}
    for name, (value_a, value_b) in all_facet_differences(raw_a, raw_b).items():
        if value_a is MISSING or value_b is MISSING:
            continue  # an id-linked facet is present in both documents

        string_a, string_b = str(value_a), str(value_b)
        in_each_id = (
            string_a in id_project_specific_a and string_b in id_project_specific_b
        )
        is_whole_id = (
            string_a == id_project_specific_a or string_b == id_project_specific_b
        )
        if in_each_id and not is_whole_id:
            differences[name] = (value_a, value_b)

    return differences


# Please build this API.
# We want to be able to pass in one or more pieces of normalised information
# related to datasets
# (we could get clashes over more than just two rows)
# and to get back something which shows all the facets that differ,
# with clear links back to the datasets we started with.
#
# The flow I'm expecting is:
# - ingest datasets, including saving their normalised facets
# - load data
# - discover a clash
# - load normalised facets for each dataset in the clash
# - pass into this function
# - get the differences
# - higher-level wrapper then does something with these differences
#   to make a nice error for the user
#
# I don't mind if you keep or delete the functions above.
def facet_differences(
    normalised_info: tuple[tuple[int, dict[str, Any]], ...],
) -> dict[str, dict[int, Any]]:
    """
    Find the facets that explain why datasets differ

    Parameters
    ----------
    raw_info
        Raw information

        Each element is a tuple with two elements.
        The first is the ID of the dataset
        (or dataset version, Anna please think and decide)
        to which these facets are linked.
        The second is the normalised facets.

    Returns
    -------
    :
        `{facet_name: {id_a: value_in_a, id_b: value_in_b}}`
        for each distinguishing facet.
        Empty if nothing in the raw documents explains the id difference.

    Examples
    --------
    >>> facet_differences(
    ...     ((2015, {"product": ["output1"]}), (1031, {"product": ["output2"]}))
    ... )
    {'product': {2015: 'output1', 1031: 'output2'}}
    """
    # Check that no ID is repeated in normalised_info
    raise NotImplementedError
