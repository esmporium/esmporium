"""
Fixtures for the header-reading tests
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from tenacity import Retrying, retry_if_exception, stop_after_attempt

from esmporium.datasets import AccessService, ContainerFormat
from esmporium.downloads import Route
from esmporium.headers import (
    HeaderReadCall,
    HeaderReadCallObserver,
    build_header_retrying,
    is_worth_retrying,
)

NODE = "esgf.example.org"
"""A stand-in data node name, which is deliberately not the host in `URL`"""

URL = "https://dap.esgf.example.org/thredds/fileServer/tas.nc"
"""
A stand-in route URL

Its host differs from `NODE` on purpose: ESGF publishes a node name which is not
necessarily the host that serves the bytes (`ceda.ac.uk` serves over `dap.ceda.ac.uk`),
and health is tracked against the node.
"""


@pytest.fixture
def route() -> Route:
    """
    Get a fetchable route to a stand-in file

    Returns
    -------
    :
        An HTTPS route to netCDF bytes
    """
    return Route(
        url=URL,
        data_node=NODE,
        service=AccessService.HTTPS,
        container_format=ContainerFormat.NETCDF,
        data_access_id=7,
    )


@pytest.fixture
def client_for() -> Callable[[Callable[[httpx.Request], httpx.Response]], httpx.Client]:
    """
    Get a factory for an `httpx.Client` whose answers a test controls

    The handler is called with the `httpx.Request` and returns an `httpx.Response`, or
    raises to simulate a transport failure. The same idiom as
    `tests/unit/downloads/conftest.py`.

    Returns
    -------
    :
        A factory taking a handler and returning a client
    """

    def factory(
        handler: Callable[[httpx.Request], httpx.Response],
    ) -> httpx.Client:
        return httpx.Client(
            transport=httpx.MockTransport(handler), follow_redirects=True
        )

    return factory


@pytest.fixture
def no_retry() -> Retrying:
    """
    Get a retry policy which tries once

    Most classification tests are about *what* a failure is rather than about
    retrying, and a real policy would make them wait out the backoff.

    Returns
    -------
    :
        A policy allowing a single attempt
    """
    return build_header_retrying(1)


@pytest.fixture
def fast_retrying() -> Callable[[int], Retrying]:
    """
    Get a factory for a retry policy with the waiting taken out

    Keeps the rule about *which* failures are retried -- the part under test -- and
    drops only the backoff, so a test of retrying does not sleep for seconds.

    Returns
    -------
    :
        A factory taking a number of attempts and returning a policy
    """

    def factory(attempts: int) -> Retrying:
        return Retrying(
            stop=stop_after_attempt(attempts),
            retry=retry_if_exception(is_worth_retrying),
            reraise=True,
        )

    return factory


@pytest.fixture
def collect_calls() -> tuple[HeaderReadCallObserver, list[HeaderReadCall]]:
    """
    Get an observer which appends every attempt it is told about, and that list

    Returns
    -------
    :
        The observer, and the list it fills
    """
    calls: list[HeaderReadCall] = []

    return calls.append, calls
