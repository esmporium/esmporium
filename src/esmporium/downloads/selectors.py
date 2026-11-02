"""
In-built data node selectors

The twin of [esmporium.search.search_api_facade.selectors][], and the same shape: a
selector is a plain callable taking the candidates and a 0-based attempt index, and
returning `None` to say there is nothing left to try.

Note the one naming difference. Search's default is
[DEFAULT_SEARCH_API_SELECTOR][esmporium.search.DEFAULT_SEARCH_API_SELECTOR] and this
one is [DEFAULT_DATA_NODE_SELECTOR][(m).]; neither is called `DEFAULT_SELECTOR`,
because both are flattened to the top of their package and "the default selector"
would then mean two different things depending on which import you read.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from esmporium.downloads.routes import Route, https_twin, is_fetchable

DataNodeSelector = Callable[[Sequence[Route], int], "Route | None"]
"""
Chooses which route to try next for one file

Given every route we know of for a file and a 0-based attempt index, returns the next
[Route][esmporium.downloads.Route] to try, or `None` to say that there is nothing left
to try for this file.

A selector is handed *one file's* routes, not the whole download, because which mirror
is best is a per-file question: not every data node holds every file.
"""


class SelectorOfferedNoRouteError(ValueError):
    """
    Raised when a selector offers no route for a file from the very start

    Asking for a download is asking for it to happen. Handing back an empty answer
    would read as "we tried, and nobody had it", when what happened is that nobody was
    tried at all: a selector whose rules rule out every route, or a file whose routes
    were all filtered away before the selector saw them, is a bug in the calling code
    and is worth saying out loud rather than quietly returning nothing.

    This is only about having nothing to try. Routes which *were* tried and did not
    serve the bytes are a different thing, and are reported as such -- see
    [DownloadStatus.GIVEN_UP][esmporium.db.schema.DownloadStatus.GIVEN_UP].

    The twin of
    [SelectorOfferedNoAPIFacadeError][esmporium.search.SelectorOfferedNoAPIFacadeError].
    """

    def __init__(self, routes: Sequence[Route], selector: DataNodeSelector) -> None:
        """
        Initialise the error

        Parameters
        ----------
        routes
            The routes the selector was given

        selector
            The selector which had nothing to offer
        """
        self.routes = tuple(routes)
        self.selector = selector
        super().__init__(
            "No route was offered on the very first attempt, so there was nothing to "
            f"try. The selector was: {selector}. It was given {len(self.routes)} "
            f"route(s): {[route.url for route in self.routes]}."
        )


def prepare_routes(
    routes: Sequence[Route], *, add_https_twins: bool = True
) -> tuple[Route, ...]:
    """
    Get the routes worth trying for a file, in a sensible starting order

    Three things happen here, and they are separated from selection on purpose:
    *which* routes exist is a fact about the file, where *what order to try them in*
    is a policy. A selector should not have to re-derive the first to apply the
    second.

    1. Routes which are not bytes over HTTPS are dropped. `DataAccess` records every
       route an API published, including ones that cannot be downloaded, which is the
       point of recording them -- so something has to filter.
    2. An `https://` twin is added for any `http://`-only route. Some replicas are
       indexed over plain HTTP but serve HTTPS on the same path, and trying the secure
       one first costs a single request.
    3. `https://` routes are put before `http://` ones, and otherwise the given order
       is kept.

    Parameters
    ----------
    routes
        Every route we know of for one file

    add_https_twins
        Whether to add an `https://` twin for each `http://`-only route

        On by default. Pass `False` to try only exactly what was published.

    Returns
    -------
    :
        The routes worth trying, best-guess order first

        Possibly empty, if nothing published for this file is fetchable -- which is
        what [SelectorOfferedNoRouteError][(m).] is for at the call site.

    Examples
    --------
    >>> from esmporium.datasets import AccessService, ContainerFormat
    >>> def route(url):
    ...     return Route(
    ...         url=url,
    ...         data_node="esgf.example.org",
    ...         service=AccessService.HTTPS,
    ...         container_format=ContainerFormat.NETCDF,
    ...     )
    >>> prepared = prepare_routes([route("http://esgf.example.org/a.nc")])
    >>> [each.url for each in prepared]
    ['https://esgf.example.org/a.nc', 'http://esgf.example.org/a.nc']
    """
    fetchable = [route for route in routes if is_fetchable(route)]

    if add_https_twins:
        # Insert each twin immediately, then let the sort below float it to the
        # front. Built from the fetchable list so a twin of an unfetchable route is
        # never invented.
        twins = [twin for route in fetchable if (twin := https_twin(route)) is not None]
        seen = {route.url for route in fetchable}
        fetchable += [twin for twin in twins if twin.url not in seen]

    # Stable, so routes which tie keep the order they arrived in: the caller's order
    # is information (it may already be health-ranked) and should not be scrambled.
    return tuple(
        sorted(fetchable, key=lambda route: 0 if route.url.startswith("https:") else 1)
    )


def build_list_selector(routes: Sequence[Route]) -> DataNodeSelector:
    """
    Build a selector that works through a fixed list of routes, ignoring its input

    Mostly useful in tests and for a caller who has already decided the order
    completely.

    Parameters
    ----------
    routes
        The routes to yield, in order

    Returns
    -------
    :
        A selector over `routes`
    """

    def select(candidates: Sequence[Route], attempt: int) -> Route | None:
        return routes[attempt] if attempt < len(routes) else None

    return select


def build_data_node_list_selector(
    data_nodes: Sequence[str], *, only_listed: bool = False
) -> DataNodeSelector:
    """
    Build a selector which tries the named data nodes first, in the order given

    This is the "I know which nodes are good, use mine" seam, and the download twin of
    passing your own ordered list of index nodes to search. It is also what to reach
    for instead of the health-based selector when you would rather not depend on
    whatever health happens to have been recorded.

    Parameters
    ----------
    data_nodes
        The data nodes to prefer, best first

        Matched against [Route.data_node][esmporium.downloads.Route.data_node], which
        is the node's published name, **not** the host in the URL -- the two differ
        (e.g. `ceda.ac.uk` serves over `dap.ceda.ac.uk`).

    only_listed
        Whether to try *only* the named nodes

        `False` (the default) tries the named ones first and then everything else, so
        naming a preference never costs you a file that only an unnamed node holds.
        `True` refuses the rest, which is what you want to pin a download to nodes you
        trust or have an agreement with.

    Returns
    -------
    :
        A selector which orders a file's routes by `data_nodes`
    """
    ranks = {node: index for index, node in enumerate(data_nodes)}

    def select(candidates: Sequence[Route], attempt: int) -> Route | None:
        prepared = prepare_routes(candidates)

        if only_listed:
            prepared = tuple(route for route in prepared if route.data_node in ranks)

        # `len(ranks)` for anything unnamed, so unnamed nodes keep their relative
        # order and follow. Stable sort, so ties are untouched.
        ordered = sorted(
            prepared, key=lambda route: ranks.get(route.data_node, len(ranks))
        )

        return ordered[attempt] if attempt < len(ordered) else None

    return select


def select_as_given(candidates: Sequence[Route], attempt: int) -> Route | None:
    """
    Try a file's routes in the order they arrived, secure ones first

    **This is deliberately not a ranking**, and it is worth being plain about why
    there isn't one. Search's default list of index nodes is hand-curated by how many
    results each returns, and nothing equivalent is honest for data nodes: which nodes
    hold a given file varies per file, ESGF has dozens of them, and until something has
    actually been downloaded there is no measurement to order them by. Inventing an
    order would look like knowledge and be guesswork.

    So this does the two things that are defensible without any measurements -- drop
    what cannot be fetched, and prefer HTTPS -- and otherwise trusts the caller's
    order. Once downloads have happened, use
    [build_data_node_health_selector][esmporium.db.build_data_node_health_selector],
    which does have something to go on. To impose your own order, use
    [build_data_node_list_selector][(m).].

    Parameters
    ----------
    candidates
        Every route we know of for one file

    attempt
        Which attempt this is, 0-based

    Returns
    -------
    :
        The route to try, or `None` if there are no more
    """
    prepared = prepare_routes(candidates)

    return prepared[attempt] if attempt < len(prepared) else None


DEFAULT_DATA_NODE_SELECTOR: DataNodeSelector = select_as_given
"""
The data node selector used when the caller does not choose one

See [select_as_given][(m).] for why the default has no ranking in it.
"""
