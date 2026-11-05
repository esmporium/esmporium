"""
Really reading file headers from the live ESGF data nodes

Opt-in, because it needs somebody else's server to be up: run it with
`--run-reads-file-headers`, or `make test-headers`.

Much cheaper than the download tests beside it -- about 64 KiB per file over a range
request, written nowhere -- but it does depend on a data node answering, and some do
not. Sampled on 2026-11-04, 3 of 47 CMIP5 models timed out. So these tests assert the
*shape* of what comes back and skip when nothing answers, rather than pinning a
particular node.

The point of the unit tests is that the parsers are right against recorded bytes. The
point of these is that what ESGF really serves still looks like those recordings.
"""

from __future__ import annotations

import json

import httpx
import pytest

from esmporium.datasets import AccessService, ContainerFormat, HeaderReadOutcome
from esmporium.downloads import Route, prepare_routes
from esmporium.headers import (
    HeaderLayout,
    HeaderReadError,
    HeaderRequest,
    read_header,
    read_headers,
    sniff_layout,
)

pytestmark = pytest.mark.reads_file_headers

SEARCH_HOST = "esgf.ceda.ac.uk"
"""
Which index to ask for files

One host rather than the facade's failover, because these tests are about the *data*
nodes, and a search that quietly came from somewhere else would make a failure harder
to read.
"""


def find_files(project: str, **facets: str) -> list[dict]:
    """
    Ask the index for some files of a project

    Parameters
    ----------
    project
        The project to search

    facets
        Further facets to narrow by, spelled as that project's Solr index spells them

    Returns
    -------
    :
        The file documents, which may be empty
    """
    params = {
        "type": "File",
        "project": project,
        "limit": 20,
        "format": "application/solr+json",
        "fields": "url,title,size",
        "distrib": "false",
        **facets,
    }
    response = httpx.get(
        f"https://{SEARCH_HOST}/esg-search/search", params=params, timeout=60
    )
    response.raise_for_status()

    # Parsed from the text rather than with `.json()`: these endpoints answer with a
    # `text/json` content type, which httpx will not treat as JSON.
    return json.loads(response.text)["response"]["docs"]


def routes_from(document: dict) -> tuple[Route, ...]:
    """
    Turn one Solr file document's URLs into routes

    Parameters
    ----------
    document
        The file document

    Returns
    -------
    :
        Its fetchable routes
    """
    urls = [
        entry.split("|")[0]
        for entry in document.get("url", [])
        if "HTTPServer" in entry
    ]

    return prepare_routes(
        tuple(
            Route(
                url=url,
                # The real node name is not in a File document's fields here, and
                # these tests do not rank nodes, so the URL's host stands in.
                data_node=httpx.URL(url).host,
                service=AccessService.HTTPS,
                container_format=ContainerFormat.NETCDF,
            )
            for url in urls
        )
    )


def requests_for(project: str, **facets: str) -> list[HeaderRequest]:
    """
    Build a header request per file the index knows about

    Parameters
    ----------
    project
        The project to search

    facets
        Further facets to narrow by

    Returns
    -------
    :
        The requests, skipping files with no fetchable route
    """
    found = []
    for document in find_files(project, **facets):
        routes = routes_from(document)
        if routes:
            found.append(
                HeaderRequest(
                    routes=routes,
                    filename=document["title"],
                    published_size=document.get("size"),
                )
            )

    return found


def first_header(requests: list[HeaderRequest]):
    """
    Read the first header any of these files will give us

    Parameters
    ----------
    requests
        The files to try

    Returns
    -------
    :
        The header

        Skips the test if no file answered, which is a data node being down rather
        than a change in our code.
    """
    if not requests:
        pytest.skip("the index returned no files with a fetchable route")

    results = read_headers(requests[:4])
    for result in results:
        if result.header is not None:
            return result.header

    pytest.skip(
        "no data node served a header for any of "
        f"{[result.request.filename for result in results]}"
    )

    raise AssertionError  # pragma: no cover - `pytest.skip` does not return


def test_a_live_cmip5_header_really_reads():
    """
    Classic netCDF, over the wire, with nothing installed to read it

    The attributes asserted are the ones CMIP5 actually publishes and no more: there
    is no parent model, era or activity in a CMIP5 header, which is why
    `read_cmip5_parent_header` has to infer two of them.
    """
    header = first_header(
        requests_for(
            "CMIP5", experiment="abrupt4xCO2", variable="tas", time_frequency="mon"
        )
    )

    assert sniff_layout(header.bytes_read.prefix) == HeaderLayout.NETCDF3
    assert header.attributes["project_id"] == "CMIP5"
    assert "parent_experiment_id" in header.attributes
    # The spelling that differs from CMIP6's, which is the reason for a reader per
    # project rather than per format.
    assert "parent_experiment_rip" in header.attributes
    assert "branch_time" in header.attributes
    # And the ones CMIP5 does not have.
    assert "parent_source_id" not in header.attributes
    assert "parent_mip_era" not in header.attributes
    assert "parent_time_units" not in header.attributes


def test_a_live_cmip6_header_really_reads():
    """
    netCDF-4/HDF5, over the wire, through the optional `headers` extra

    CMIP6 publishes the parent completely, which is the contrast with CMIP5 above.
    """
    header = first_header(
        requests_for(
            "CMIP6",
            experiment_id="abrupt-4xCO2",
            variable_id="tas",
            frequency="mon",
        )
    )

    assert sniff_layout(header.bytes_read.prefix) == HeaderLayout.NETCDF4_HDF5
    assert header.attributes["mip_era"] == "CMIP6"
    for name in (
        "parent_experiment_id",
        "parent_source_id",
        "parent_variant_label",
        "parent_activity_id",
        "parent_mip_era",
        "parent_time_units",
        "branch_time_in_parent",
        "branch_time_in_child",
    ):
        assert name in header.attributes, name

    # Nothing numpy-shaped escapes the reader.
    assert {type(value) for value in header.attributes.values()} <= {
        str,
        int,
        float,
        tuple,
    }


def test_a_header_costs_a_fraction_of_the_file():
    """
    The claim the whole package rests on, measured against a real file

    A header read moves at most what it asked for, against a file that can be
    hundreds of megabytes. If this ever stopped being true -- a node ignoring every
    range request, say -- reading a header would be no cheaper than downloading.
    """
    header = first_header(
        requests_for(
            "CMIP5", experiment="abrupt4xCO2", variable="tas", time_frequency="mon"
        )
    )

    read = len(header.bytes_read.prefix)
    whole = header.bytes_read.file_size

    assert whole is not None, "the data node reported no file size"
    assert read <= 64 * 1024
    assert read < whole / 10


def test_the_data_nodes_still_honour_a_range_request():
    """
    Sampled at 47 out of 47 on 2026-11-04, and worth knowing if it changes

    A node answering `200` with the whole body still gives us a usable header -- we
    read our prefix and abandon the connection -- so this is not a correctness test.
    It is a cost test: such a node makes us open a connection it is about to pour a
    multi-gigabyte file down.
    """
    header = first_header(
        requests_for(
            "CMIP6",
            experiment_id="abrupt-4xCO2",
            variable_id="tas",
            frequency="mon",
        )
    )

    assert header.bytes_read.range_honoured, (
        f"{header.route.data_node} answered our range request with the whole file; "
        "the header still read, but see HeaderReadCallRecord.range_honoured"
    )


def test_a_url_that_is_not_there_is_reported_as_missing():
    """
    A real 404 from a real node, classified by its status code

    Cheap, and it pins the one classification that cannot be checked offline without
    trusting our own mock: that an ESGF node really does answer 404 rather than, say,
    an HTML error page with a 200.
    """
    header = first_header(
        requests_for(
            "CMIP5", experiment="abrupt4xCO2", variable="tas", time_frequency="mon"
        )
    )
    missing = Route(
        url=f"{header.route.url}.does-not-exist",
        data_node=header.route.data_node,
        service=AccessService.HTTPS,
        container_format=ContainerFormat.NETCDF,
    )

    with httpx.Client(follow_redirects=True) as client:
        with pytest.raises(HeaderReadError) as caught:
            read_header(client, missing)

    assert caught.value.outcome == HeaderReadOutcome.MISSING
    assert not caught.value.outcome.counts_against_data_node
