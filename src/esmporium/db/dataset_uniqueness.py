"""
Explaining why datasets that look identical to us are actually different

When datasets share every column our [`Dataset`][esmporium.db.schema.Dataset] model
records but have different `id_project_specific` values, they are distinguished by some
project-specific facet we do not model as a column: `product` for CMIP5, `activity_id`
for CMIP6, and (for CMIP7) things like `activity_id`, `region` or the branding labels.

Rather than hard-code any of those names, or try to split the native id on `.` (model
names contain dots, so that is unsafe), we compare the facets read out of the raw search
documents we stored.

This module is deliberately generation-agnostic: it never sees raw JSON and never
sniffs Solr vs STAC. The flattening of a stored document into `{facet_name: value}`
lives in the search layer ([`esmporium.search.normalise_stored_document`][]), which
knows the response shapes; here we only compare the already-flat mappings it produces.
That keeps the database layer free of any search-generation knowledge.
"""

# TODO: for facet differences also list id_project_specific?
# Or just leave this to future higher level function which identifies to the user
# where the clashes are
from __future__ import annotations

from typing import Any


class _Missing:
    """Sentinel for a facet present in some documents but absent from another."""

    def __repr__(self) -> str:
        return "<absent>"


MISSING = _Missing()
"""Marks a dataset whose document lacks a facet that others in the comparison carry."""


def facet_differences(
    normalised_info: tuple[tuple[int, dict[str, Any]], ...],
) -> dict[str, dict[int, Any]]:
    """
    Find the facets that explain why datasets differ

    Given the normalised facets of two or more datasets that our model considers
    identical (same values in every column we record, yet different
    `id_project_specific`), this reports every facet on which they do not all agree,
    keyed back to the datasets it came from. The higher-level clash-resolution flow
    ("which product did you mean?") uses this to tell the user what actually differs.

    A facet is reported whenever the datasets do not all share one value for it,
    including when some carry it and others do not (the absent side is marked
    [`MISSING`][(m).]). Nothing here decides which of those differences matter to the
    user; that filtering is left to the caller.

    Parameters
    ----------
    normalised_info
        One entry per dataset in the clash. Each is a tuple of the dataset's id
        (currently [`Dataset.id`][esmporium.db.schema.Dataset]; see the note below) and
        its normalised facets, as produced by
        [`esmporium.search.normalise_stored_document`][].

    Returns
    -------
    :
        `{facet_name: {id: value}}` for each facet the datasets do not all agree on,
        with one entry per dataset id (its value, or [`MISSING`][(m).] if its document
        lacks the facet). Empty if the datasets agree on every facet -- meaning nothing
        in the raw documents explains their `id_project_specific` difference.

    Raises
    ------
    ValueError
        The same dataset id appears more than once in `normalised_info`, so results
        keyed by id would be ambiguous.

    Examples
    --------
    >>> facet_differences(
    ...     ((2015, {"product": "output1"}), (1031, {"product": "output2"}))
    ... )
    {'product': {2015: 'output1', 1031: 'output2'}}

    Notes
    -----
    The id is currently the [`Dataset.id`][esmporium.db.schema.Dataset] of each clashing
    row -- a clash is a `Dataset`-level event (all our columns equal, with
    `id_project_specific` differing), so the distinguishing facet is a property of the
    dataset, not of any one edition. When the higher-level clash-resolution wrapper is
    built we may need to key on (or additionally carry) a `DatasetVersionSpecific.id` if
    versions turn out to distinguish a clash; revisit the key then.
    """
    ids = [dataset_id for dataset_id, _ in normalised_info]
    if len(ids) != len(set(ids)):
        msg = (
            "Every dataset id in normalised_info must be unique, but at least one is "
            f"repeated: {ids}. Results are keyed by id, so duplicates are ambiguous."
        )
        raise ValueError(msg)

    all_facet_names: set[str] = set().union(
        *(facets.keys() for _, facets in normalised_info)
    )

    differences: dict[str, dict[int, Any]] = {}
    for name in all_facet_names:
        values_by_id = {
            dataset_id: facets.get(name, MISSING)
            for dataset_id, facets in normalised_info
        }
        distinct_values = list(values_by_id.values())
        if any(value != distinct_values[0] for value in distinct_values[1:]):
            differences[name] = values_by_id

    return differences
