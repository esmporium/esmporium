"""
Facade keys: the identity we group a search's results and failures by
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeAlias

if TYPE_CHECKING:
    from esmporium.search.search_api_facade import SearchAPIFacade

FacadeKey: TypeAlias = tuple[str, str, str]
"""
A key which identifies a given facade

The first element is the host.
The second is the type of search API this facade uses/assumes.
The third is the query style used by this facade.
"""


def get_facade_key(facade: SearchAPIFacade) -> FacadeKey:
    """
    Get the key which identifies a facade

    Each element can be served by more than one facade,
    which is why we group them like this.

    Parameters
    ----------
    facade
        The facade to identify

    Returns
    -------
    :
        The facade's key
    """
    return (
        facade.search_api.host,
        type(facade.search_api).__name__,
        facade.parameters.base_query_style.__name__,
    )
