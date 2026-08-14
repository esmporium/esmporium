"""
Choosing which node to try next for a project's search.

An [`EndPointSelector`][esmporium.esgf.search.selector.EndPointSelector] is given a
single-project query and an attempt number, and returns the node to try on that
attempt — or ``None`` when there are no more to try. This is the injectable seam for
the fallback policy: the orchestrator advances the attempt number each time a node
yields no usable result, so returning nodes in some order *is* the fallback queue.

The default,
[`make_default_selector`][esmporium.esgf.search.selector.make_default_selector],
simply walks the given nodes in order. A user override is any callable of the same
shape and may inspect the query (e.g. its project or facets) to build a smarter
queue; the merge of results across nodes is a separate, later concern.
"""

from collections.abc import Callable, Sequence

from esmporium.esgf.query_models import _ESGFQueryBase
from esmporium.esgf.search.hosts import IndexNode

EndPointSelector = Callable[[_ESGFQueryBase, int], IndexNode | None]
"""Given a single-project query and attempt number, the node to try, or ``None``."""


def make_default_selector(nodes: Sequence[IndexNode]) -> EndPointSelector:
    """
    Build a selector that walks the given nodes in order.

    Parameters
    ----------
    nodes
        The candidate nodes, in the order to try them.

    Returns
    -------
    :
        A selector returning ``nodes[attempt]`` while in range, else ``None``.
    """

    def select(query: _ESGFQueryBase, attempt: int) -> IndexNode | None:
        """Return the node for this attempt, or ``None`` when exhausted."""
        if attempt < len(nodes):
            return nodes[attempt]
        return None

    return select
