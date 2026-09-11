"""
Identify the facets which make datasets unique, even if they look identical to us

When datasets share every column our [`Dataset`][esmporium.db.schema.Dataset] model
records but have different `id_project_specific` values, they are distinguished by some
project-specific facet we do not model as a column: `product` for CMIP5, `activity_id`
for CMIP6, and (for CMIP7) things like `activity_id`, `region` or the branding labels.
"""

from __future__ import annotations

from typing import Any

# TODO Zeb: can delete the below comment if makes sense?
# Alternatively can make inline rather than a class if a parameter returning "<absent>"
# is not a real risk


# A sentinel, not the string "<absent>": a unique object can never equal a real facet
# value (a facet that genuinely held "<absent>" would otherwise be mistaken for
# missing), while the `__repr__` keeps it readable in output. That is the whole reason
# it is a class -- uniqueness by identity plus a friendly repr -- so it is kept, shrunk.
class _Missing:
    __slots__ = ()

    def __repr__(self) -> str:
        return "<absent>"


MISSING = _Missing()
"""Marks a dataset whose document lacks a facet that others in the comparison carry."""


def facet_differences(
    normalised_info: tuple[tuple[int, dict[str, Any]], ...],
) -> dict[str, dict[int, Any]]:
    """
    Find all the facets that explain why datasets differ

    Given the normalised facets of two or more datasets that our model considers
    identical (same values in every column we record, yet different
    `id_project_specific`), this reports every facet on which they do not all agree,
    keyed back to the datasets it came from.

    Parameters
    ----------
    normalised_info
        One entry per dataset in the clash. Each is a tuple of the dataset's id
        [`Dataset.id`][esmporium.db.schema.Dataset] and
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
    ...     ((201545, {"product": "output1"}), (103137, {"product": "output2"}))
    ... )
    {'product': {201545: 'output1', 103137: 'output2'}}
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
