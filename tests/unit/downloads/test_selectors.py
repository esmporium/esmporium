"""
Tests of choosing which data node to try next
"""

import pytest

from esmporium.datasets import AccessService, ContainerFormat
from esmporium.downloads import (
    DEFAULT_DATA_NODE_SELECTOR,
    Route,
    SelectorOfferedNoRouteError,
    build_data_node_list_selector,
    build_list_selector,
    https_twin,
    is_fetchable,
    prepare_routes,
    select_as_given,
)


def route_on(
    data_node,
    *,
    scheme="https",
    service=AccessService.HTTPS,
    container_format=ContainerFormat.NETCDF,
    data_access_id=1,
):
    """Build a route served by a given data node"""
    return Route(
        url=f"{scheme}://{data_node}/tas.nc",
        data_node=data_node,
        service=service,
        container_format=container_format,
        data_access_id=data_access_id,
    )


def nodes_offered(selector, routes):
    """Work a selector to exhaustion, giving back the data nodes it offered in order"""
    offered = []
    attempt = 0
    while (route := selector(routes, attempt)) is not None:
        offered.append(route.data_node)
        attempt += 1

    return offered


def test_unfetchable_routes_are_dropped_before_selection():
    """
    `DataAccess` records every route an API published, including undownloadable ones

    That is the point of recording them, so something has to filter -- and it is
    `prepare_routes`, not the selector, so that a selector of your own gets a clean
    list.
    """
    routes = [
        route_on(
            "a.example.org",
            service=AccessService.OPENDAP,
            container_format=ContainerFormat.HTML,
        ),
        route_on(
            "b.example.org",
            service=AccessService.GLOBUS,
            container_format=ContainerFormat.HTML,
        ),
        route_on("c.example.org", service=AccessService.GRIDFTP),
        route_on("d.example.org"),
    ]

    assert [r.data_node for r in prepare_routes(routes)] == ["d.example.org"]


def test_an_https_twin_is_added_and_tried_first():
    """
    Some replicas are indexed over plain HTTP but serve HTTPS on the same path

    Trying the secure one first costs a single request and sometimes turns a node we
    would have given up on into one that works.
    """
    prepared = prepare_routes([route_on("a.example.org", scheme="http")])

    assert [r.url for r in prepared] == [
        "https://a.example.org/tas.nc",
        "http://a.example.org/tas.nc",
    ]
    # The twin fetches a URL the stored row does not hold, so it claims no id.
    assert prepared[0].data_access_id is None
    assert prepared[1].data_access_id == 1


def test_https_twins_can_be_turned_off():
    """For a caller who wants exactly what was published and nothing invented"""
    prepared = prepare_routes(
        [route_on("a.example.org", scheme="http")], add_https_twins=False
    )

    assert [r.url for r in prepared] == ["http://a.example.org/tas.nc"]


def test_a_twin_is_not_added_when_the_https_route_already_exists():
    """No duplicate requests to the same URL"""
    prepared = prepare_routes(
        [
            route_on("a.example.org", scheme="http"),
            route_on("a.example.org", scheme="https"),
        ]
    )

    assert [r.url for r in prepared].count("https://a.example.org/tas.nc") == 1


def test_https_twin_of_an_https_route_is_none():
    """There is nothing to upgrade"""
    assert https_twin(route_on("a.example.org")) is None


def test_the_default_selector_keeps_the_given_order():
    """
    Deliberately not a ranking

    There is no honest default order for data nodes: which hold a given file varies
    per file, and until something has been downloaded there is nothing to rank by.
    Inventing one would look like knowledge and be guesswork.
    """
    routes = [route_on(f"n{index}.example.org") for index in range(3)]

    assert nodes_offered(DEFAULT_DATA_NODE_SELECTOR, routes) == [
        "n0.example.org",
        "n1.example.org",
        "n2.example.org",
    ]
    assert DEFAULT_DATA_NODE_SELECTOR is select_as_given


def test_a_selector_returns_none_once_exhausted():
    """How the caller knows to stop, rather than looping forever"""
    routes = [route_on("a.example.org")]

    assert select_as_given(routes, 0) is not None
    assert select_as_given(routes, 1) is None


def test_an_injected_list_puts_the_named_nodes_first():
    """
    The requirement: a user supplies their own ranked list of data nodes

    Matched on the *published node name*, not the URL's host -- the two differ, e.g.
    `ceda.ac.uk` serves over `dap.ceda.ac.uk`.
    """
    routes = [
        route_on("slow.example.org"),
        route_on("fast.example.org"),
        route_on("other.example.org"),
    ]
    selector = build_data_node_list_selector(["fast.example.org", "slow.example.org"])

    assert nodes_offered(selector, routes) == [
        "fast.example.org",
        "slow.example.org",
        # Unnamed, so it follows -- naming a preference never costs you a file that
        # only an unnamed node holds.
        "other.example.org",
    ]


def test_an_injected_list_can_refuse_everything_else():
    """
    `only_listed` pins a download to nodes you trust or have an agreement with

    Off by default, because silently refusing the only node that holds a file is a
    surprising way to lose a download.
    """
    routes = [route_on("mine.example.org"), route_on("other.example.org")]
    selector = build_data_node_list_selector(["mine.example.org"], only_listed=True)

    assert nodes_offered(selector, routes) == ["mine.example.org"]


def test_an_injected_list_still_drops_unfetchable_routes():
    """Preferring a node does not make its OPeNDAP landing page downloadable"""
    routes = [
        route_on(
            "mine.example.org",
            service=AccessService.OPENDAP,
            container_format=ContainerFormat.HTML,
        ),
        route_on("other.example.org"),
    ]
    selector = build_data_node_list_selector(["mine.example.org"])

    assert nodes_offered(selector, routes) == ["other.example.org"]


def test_an_injected_list_naming_nothing_we_hold_still_tries_everything():
    """A preference for nodes that do not serve this file is not a refusal"""
    routes = [route_on("a.example.org")]
    selector = build_data_node_list_selector(["somewhere.else.org"])

    assert nodes_offered(selector, routes) == ["a.example.org"]


def test_only_listed_with_no_matches_offers_nothing():
    """
    Which the caller surfaces as `SelectorOfferedNoRouteError`

    Pinning a download to nodes that do not have the file is a mistake worth being
    told about rather than a silent empty result.
    """
    routes = [route_on("a.example.org")]
    selector = build_data_node_list_selector(["somewhere.else.org"], only_listed=True)

    assert selector(routes, 0) is None


def test_a_fixed_list_selector_ignores_what_it_is_given():
    """For a caller who has decided the order completely"""
    chosen = [route_on("chosen.example.org")]
    selector = build_list_selector(chosen)

    assert nodes_offered(selector, [route_on("ignored.example.org")]) == [
        "chosen.example.org"
    ]


def test_the_error_names_what_it_was_given():
    """So "nothing to try" is debuggable rather than mysterious"""
    routes = (route_on("a.example.org"),)
    selector = build_list_selector([])

    error = SelectorOfferedNoRouteError(routes, selector)

    assert "a.example.org" in str(error)
    assert error.routes == routes


@pytest.mark.parametrize(
    "container_format, fetchable",
    (
        (ContainerFormat.NETCDF, True),
        (ContainerFormat.KERCHUNK, True),
        (ContainerFormat.ZARR, False),
        (ContainerFormat.ICECHUNK, False),
        (ContainerFormat.HTML, False),
    ),
)
def test_which_container_formats_are_fetchable(container_format, fetchable):
    """
    A store is not a file, and a web page is not data

    `KERCHUNK` is fetchable here even though every kerchunk route ESGF-NG advertises
    answered 404 when sampled: a 404 is a better answer than refusing to look, and it
    is recorded as `MISSING` rather than held against the node.
    """
    assert (
        is_fetchable(route_on("a.example.org", container_format=container_format))
        is fetchable
    )
