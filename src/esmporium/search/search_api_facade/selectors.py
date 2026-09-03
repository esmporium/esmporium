"""
In-built search API facade selectors
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from esmporium.query import (
    QueryCanonical,
)
from esmporium.search.search_api_facade.core import SearchAPIFacade
from esmporium.search.search_api_facade.store import INBUILT_SEARCH_API_FACADE_STORE

SearchAPIFacadeSelector = Callable[[QueryCanonical, int], SearchAPIFacade | None]
"""
Chooses which facade to try next

Given the canonical query and a 0-based attempt index,
returns the next
[SearchAPIFacade][esmporium.search.search_api_facade.SearchAPIFacade] to try,
or `None` to say that there is nothing to try for this query and attempt number.
"""


class SelectorOfferedNoAPIFacadeError(ValueError):
    """
    Raised when a selector offers no search API facade for a query from the very start

    Asking for a search is asking for it to happen.
    Handing back an empty answer would read as
    "we asked, and nobody had anything for you",
    when what happened is that nobody was asked at all:
    a selector with an empty list,
    or one whose rules rule out every endpoint for this query,
    is a bug in the calling code
    and is worth saying out loud rather than quietly returning nothing.

    This is only about having nobody to ask.
    Endpoints which were asked and did not answer are a different thing,
    and are reported as such.
    """

    def __init__(
        self, canonical: QueryCanonical, selector: SearchAPIFacadeSelector
    ) -> None:
        """
        Initialise the error

        Parameters
        ----------
        canonical
            The query we were going to ask about

        selector
            The selector which had nothing to offer for it
        """
        self.canonical = canonical
        self.selector = selector
        super().__init__(
            "No API facade was offered on the very first attempt, "
            "so there was nobody to ask. "
            f"The selector was: {selector}. The query was: {canonical!r}."
        )


def build_list_selector(facades: Sequence[SearchAPIFacade]) -> SearchAPIFacadeSelector:
    """
    Build a selector that yields search API facades in order

    Every query works through the same list.

    Parameters
    ----------
    facades
        The search API facades to yield, in order

    Returns
    -------
    :
        A selector over `facades`
    """

    def select(canonical: QueryCanonical, attempt: int) -> SearchAPIFacade | None:
        return facades[attempt] if attempt < len(facades) else None

    return select


def build_project_list_selector(
    project_lists: Mapping[str, Sequence[SearchAPIFacade]],
) -> SearchAPIFacadeSelector:
    """
    Build a selector that works through a project specific list of facades

    Parameters
    ----------
    project_lists
        The facades to yield for each project, in order

    Returns
    -------
    :
        A selector which yields facades in an order specific to the query's project
    """

    def select(canonical: QueryCanonical, attempt: int) -> SearchAPIFacade | None:
        """
        Select search API facade to use

        Parameters
        ----------
        canonical
            Query (in canonical form)

        attempt
            Search attempt

        Returns
        -------
        :
            [SearchAPIFacade][(m).] to use

            If we have run out of APIs to try, we return `None`

        Raises
        ------
        ValueError
            The query specifies a search that is not for exactly one project

        KeyError
            We do not have a list of facades to try for the input project
        """
        if len(canonical.project) != 1:
            msg = (
                "We can only unambiguously pick the SearchAPI list "
                "if there is exactly one project, "
                f"received: {canonical.project}"
            )
            raise ValueError(msg)

        project = canonical.project[0]

        apis = project_lists[project]

        return apis[attempt] if attempt < len(apis) else None

    return select


DEFAULT_SEARCH_API_FACADES_BY_PROJECT: Mapping[str, Sequence[SearchAPIFacade]] = {
    project: INBUILT_SEARCH_API_FACADE_STORE.get_api_facades_for_project(project)
    for project in ("CMIP5", "CMIP6", "CMIP7")
}
"""
Default search APIs to use, grouped by project
"""

DEFAULT_SELECTOR = build_project_list_selector(DEFAULT_SEARCH_API_FACADES_BY_PROJECT)
"""The selector used when the caller does not choose one"""
