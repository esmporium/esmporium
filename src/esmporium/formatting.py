"""
Formatting helpers for the messages we put in front of users
"""

from __future__ import annotations

from collections.abc import Sequence


def readable_list(values: Sequence[str]) -> str:
    """
    Render values as a list a person can read, e.g. `'a', 'b' and 'c'`

    Each value is `repr`'d, so a facet name comes out quoted
    and there is no doubt where one name ends and the next begins.

    Parameters
    ----------
    values
        The values to render, in the order they should be read

    Returns
    -------
    :
        The values, rendered for reading.

        An empty string if there are no values,
        so a caller can check for one before building a sentence around it.
    """
    if not values:
        return ""

    if len(values) == 1:
        return repr(values[0])

    comma_separated = ", ".join(repr(value) for value in values[:-1])
    final = repr(values[-1])
    res = f"{comma_separated} and {final}"

    return res
