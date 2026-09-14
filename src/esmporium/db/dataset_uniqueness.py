"""
Identify the facets which make datasets unique, even if they look identical to us

For example,
when datasets share every facet column our [`Dataset`][esmporium.db.schema.Dataset]
model records but have different `id_project_specific` values,
they are usually distinguished by some project-specific facet
we do not model as a column:
e.g. `product` for CMIP5, `activity_id` for CMIP6, and
(for CMIP7) things like `activity_id`, `region` or the branding labels.
Our model stores these as separate datasets,
but a query on our columns alone cannot tell them apart.

When datasets share every column, `id_project_specific` included, our model cannot
store them separately at all (see
[`UnhandledDatasetClashError`][esmporium.db.UnhandledDatasetClashError]).
This is data of a shape we did not expect: it differs in a facet
that appears neither in our columns nor in `id_project_specific`.
Diffing the raw documents shows which facet our model is missing.
"""

from __future__ import annotations

from collections.abc import Hashable
from typing import Any, TypeVar

K = TypeVar("K", bound=Hashable)
"""Type of the keys that identify each entry passed to `facet_differences`"""


class _Missing:
    __slots__ = ()

    def __repr__(self) -> str:
        return "<absent>"


MISSING = _Missing()
"""
Marks a dataset whose document lacks a facet that others in the comparison carry.

We make this a unique object so it can never accidentally match a real facet value
(e.g. we would never accidentally match a facet whose actual value was "<absent>").
Keeping this as a class means we get uniqueness by identity plus a friendly repr.
"""


def facet_differences(
    normalised_info: tuple[tuple[K, dict[str, Any]], ...],
) -> dict[str, dict[K, Any]]:
    """
    Find all the facets that explain why datasets differ

    Given the normalised facets of two or more datasets (or raw documents)
    this reports every facet on which they do not all agree,
    keyed back to the entries they came from.

    Parameters
    ----------
    normalised_info
        One entry per dataset (or raw document) in the clash.
        Each is a tuple of a key identifying the entry
        (e.g. the dataset's [`Dataset.id`][esmporium.db.schema.Dataset])
        and its normalised facets, as produced by
        [`esmporium.search.normalise_stored_document`][].

    Returns
    -------
    :
        `{facet_name: {key: value}}` for each facet the entries do not all agree on,
        with one entry per key
        (its value, or [`MISSING`][(m).] if its document lacks the facet).
        Empty if the entries agree on every facet.

    Raises
    ------
    ValueError
        The same key appears more than once in `normalised_info`, so results
        keyed by it would be ambiguous.

    Examples
    --------
    >>> facet_differences(
    ...     ((201545, {"product": "output1"}), (103137, {"product": "output2"}))
    ... )
    {'product': {201545: 'output1', 103137: 'output2'}}
    """
    keys = [key for key, _ in normalised_info]
    if len(keys) != len(set(keys)):
        msg = (
            "Every key in normalised_info must be unique, but at least one is "
            f"repeated: {keys}. Results are keyed by these, "
            "so duplicates are ambiguous."
        )
        raise ValueError(msg)

    all_facet_names: set[str] = set().union(
        *(facets.keys() for _, facets in normalised_info)
    )

    differences: dict[str, dict[K, Any]] = {}
    for name in all_facet_names:
        values_by_key = {
            key: facets.get(name, MISSING) for key, facets in normalised_info
        }
        distinct_values = list(values_by_key.values())
        if any(value != distinct_values[0] for value in distinct_values[1:]):
            differences[name] = values_by_key

    return differences
