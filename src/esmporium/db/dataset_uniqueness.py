"""
Explaining why two datasets that look identical to us are actually different

When two datasets share every column our [`Dataset`][esmporium.db.schema.Dataset] model
records but have different `id_project_specific` values, they are distinguished by some
project-specific facet we do not model as a column: `product` for CMIP5, `activity_id`
for CMIP6, and (for CMIP7) things like `activity_id`, `region` or the branding labels.

Rather than hard-code any of those names, or try to split the native id on `.` (model
names contain dots, so that is unsafe), we read the facet name and values straight out
of the raw search documents we stored. [`facet_differences`][] is the primitive the
load/clash-resolution flow ("which product did you mean?") is built on.
"""

from __future__ import annotations

from typing import Any


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
    name(s) and the two values behind that difference, read from the raw documents.

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
    facets_a = _normalise(raw_a)
    facets_b = _normalise(raw_b)

    differences: dict[str, tuple[Any, Any]] = {}
    for name in facets_a.keys() & facets_b.keys():
        value_a, value_b = facets_a[name], facets_b[name]
        if value_a == value_b:
            continue

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
