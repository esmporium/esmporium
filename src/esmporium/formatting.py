"""
Formatting helpers for the messages we put in front of users
"""

from __future__ import annotations

from collections.abc import Sequence


def readable_list(values: Sequence[str], *, quote: bool = True) -> str:
    """
    Render values as a list a person can read, e.g. `'a', 'b' and 'c'`

    Each value is `repr`'d by default, so a facet name comes out quoted
    and there is no doubt where one name ends and the next begins.

    Parameters
    ----------
    values
        The values to render, in the order they should be read

    quote
        Whether to quote each value

        `True` for anything a user would type back at us -- a facet name, a method
        name -- where the quotes say where one ends. `False` for something already
        unmistakable, where quoting only adds noise: `#1 and #2` rather than
        `'#1' and '#2'`.

    Returns
    -------
    :
        The values, rendered for reading.

        An empty string if there are no values,
        so a caller can check for one before building a sentence around it.

    Examples
    --------
    >>> readable_list(["model", "variable"])
    "'model' and 'variable'"

    >>> readable_list(["#1", "#2", "#3"], quote=False)
    '#1, #2 and #3'
    """
    if not values:
        return ""

    shown = [repr(value) if quote else value for value in values]

    if len(shown) == 1:
        return shown[0]

    comma_separated = ", ".join(shown[:-1])

    return f"{comma_separated} and {shown[-1]}"
