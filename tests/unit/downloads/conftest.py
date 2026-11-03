"""
Shared fixtures for the download tests
"""

import hashlib

import httpx
import pytest
from tenacity import Retrying, retry_if_exception, stop_after_attempt

from esmporium.datasets import AccessService, ContainerFormat
from esmporium.downloads import Route, build_download_retrying, is_transient

NODE = "esgf.example.org"
"""The data node every fixture pretends to talk to"""

URL = f"https://{NODE}/thredds/fileServer/tas.nc"
"""The route every fixture fetches"""


@pytest.fixture
def route():
    """Get an ordinary, fetchable HTTPS route to a netCDF file"""
    return Route(
        url=URL,
        data_node=NODE,
        service=AccessService.HTTPS,
        container_format=ContainerFormat.NETCDF,
        data_access_id=11,
    )


@pytest.fixture
def body():
    """Get some bytes big enough to span several 1 KiB chunks"""
    return b"netcdf-ish-bytes " * 500


@pytest.fixture
def sha256_of():
    """Get a helper which digests bytes the way ESGF publishes digests"""

    def digest(data):
        return hashlib.sha256(data).hexdigest()

    return digest


@pytest.fixture
def client_for():
    """
    Get a factory for an `httpx.Client` whose answers a test controls

    The handler is called with the `httpx.Request` and returns an `httpx.Response`,
    or raises to simulate a transport failure.
    """

    def factory(handler):
        return httpx.Client(
            transport=httpx.MockTransport(handler), follow_redirects=True
        )

    return factory


@pytest.fixture
def no_retry():
    """
    Get a retry policy which tries exactly once

    Most classification tests are about *what* a failure is, not about retrying, and
    a real policy would make them wait out the backoff.
    """
    return build_download_retrying(1)


@pytest.fixture
def collect_calls():
    """Get an observer which appends every observation to a list, and that list"""
    calls = []
    return calls.append, calls


@pytest.fixture
def fast_retrying():
    """
    Get a factory for a retry policy which retries properly but never sleeps

    The real policy's first wait is a second and it doubles, so a test of retrying
    would spend three seconds asleep. This keeps the transient rule -- which is the
    part under test -- and drops only the backoff.
    """

    def factory(attempts):
        return Retrying(
            stop=stop_after_attempt(attempts),
            retry=retry_if_exception(is_transient),
            reraise=True,
        )

    return factory
