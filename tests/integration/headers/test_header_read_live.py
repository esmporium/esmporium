"""
Really reading file headers from the live ESGF data nodes
"""

from __future__ import annotations

import json

import httpx
import pytest

from esmporium.datasets import AccessService, ContainerFormat, HeaderReadOutcome
from esmporium.downloads import Route, prepare_routes
from esmporium.headers import (
    DEFAULT_BLOCK_BYTES,
    HeaderLayout,
    HeaderReadCall,
    HeaderReadError,
    HeaderRequest,
    read_header,
    read_headers,
)

pytestmark = pytest.mark.reads_file_headers

SEARCH_HOST = "esgf.ceda.ac.uk"
"""
Which index to ask for files
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


def first_header(
    requests: list[HeaderRequest],
    *,
    time_bounds: bool = False,
    calls: list[HeaderReadCall] | None = None,
):
    """
    Read the first header any of these files will give us

    Parameters
    ----------
    requests
        The files to try

    time_bounds
        Whether to read the first and last time values too

    calls
        Where to collect every call made, if anywhere

    Returns
    -------
    :
        The header

        Skips the test if no file answered, which is a data node being down rather
        than a change in our code.
    """
    if not requests:
        pytest.skip("the index returned no files with a fetchable route")

    results = read_headers(
        requests[:4],
        time_bounds=time_bounds,
        observer=None if calls is None else calls.append,
    )
    for result in results:
        if result.header is not None:
            return result.request, result.header

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
    `parse_cmip5_file_header` has to infer two of them.
    """
    _, header = first_header(
        requests_for(
            "CMIP5", experiment="abrupt4xCO2", variable="tas", time_frequency="mon"
        )
    )

    assert header.layout == HeaderLayout.NETCDF3
    assert header.global_attributes["project_id"] == "CMIP5"
    assert "parent_experiment_id" in header.global_attributes
    # The spelling that differs from CMIP6's, which is the reason for a reader per
    # project rather than per format.
    assert "parent_experiment_rip" in header.global_attributes
    assert "branch_time" in header.global_attributes
    # And the ones CMIP5 does not have.
    assert "parent_source_id" not in header.global_attributes
    assert "parent_mip_era" not in header.global_attributes
    assert "parent_time_units" not in header.global_attributes


def test_a_live_cmip6_header_really_reads():
    """
    netCDF-4/HDF5, over the wire, through the optional `headers` extra

    CMIP6 publishes the parent completely, which is the contrast with CMIP5 above.
    """
    _, header = first_header(
        requests_for(
            "CMIP6",
            experiment_id="abrupt-4xCO2",
            variable_id="tas",
            frequency="mon",
        )
    )

    assert header.layout == HeaderLayout.NETCDF4_HDF5
    assert header.global_attributes["mip_era"] == "CMIP6"
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
        assert name in header.global_attributes, name

    # Nothing numpy-shaped escapes the reader.
    assert {type(value) for value in header.global_attributes.values()} <= {
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
    _, header = first_header(
        requests_for(
            "CMIP5", experiment="abrupt4xCO2", variable="tas", time_frequency="mon"
        )
    )

    assert header.n_requests == 1
    assert header.bytes_read <= DEFAULT_BLOCK_BYTES
    assert header.bytes_read < header.file_size / 10
    assert "calendar" in header.variable_attributes["time"]


def test_the_data_nodes_still_honour_a_range_request():
    """
    Sampled at 47 out of 47 on 2026-11-04, and worth knowing if it changes

    A node answering `200` with the whole body still gives us a usable header -- we
    read our first block and abandon the connection -- but nothing further into the
    file, so its time values cannot be read. And it makes us open a connection it is
    about to pour a multi-gigabyte file down.
    """
    calls: list[HeaderReadCall] = []
    _, header = first_header(
        requests_for(
            "CMIP6",
            experiment_id="abrupt-4xCO2",
            variable_id="tas",
            frequency="mon",
        ),
        calls=calls,
    )

    assert header.route is not None
    served = [call for call in calls if call.url == header.route.url]
    assert all(call.range_honoured for call in served), (
        f"{header.route.data_node} answered our range request with the whole file; "
        "see HeaderReadCallRecord.range_honoured"
    )


def months_between_stamps(filename: str) -> int:
    """
    How many months a monthly file's `YYYYMM-YYYYMM` stamps span, both included

    Parameters
    ----------
    filename
        The file's name

    Returns
    -------
    :
        The number of months, the same in every calendar
    """
    first, last = filename.removesuffix(".nc").rsplit("_", 1)[-1].split("-")

    return (
        (int(last[:-2]) - int(first[:-2])) * 12 + int(last[-2:]) - int(first[-2:]) + 1
    )


@pytest.mark.parametrize(
    ("project", "facets"),
    [
        pytest.param(
            "CMIP5",
            {"experiment": "abrupt4xCO2", "variable": "tas", "time_frequency": "mon"},
            id="cmip5-classic",
        ),
        pytest.param(
            "CMIP6",
            {"experiment_id": "abrupt-4xCO2", "variable_id": "tas", "frequency": "mon"},
            id="cmip6-netcdf4",
        ),
    ],
)
def test_first_and_last_time_bounds_agree_with_the_filename(project, facets):
    """
    A monthly file has one time step per month its filename stamps span

    Months are the same in every calendar, so this checks the bounds against the
    filename without converting anything.
    """
    request, header = first_header(requests_for(project, **facets), time_bounds=True)

    bounds = header.time_bounds
    assert bounds is not None
    assert bounds.first_bounds is not None
    assert bounds.last_bounds is not None
    assert request.filename is not None
    assert bounds.n_steps == months_between_stamps(request.filename)
    assert bounds.first_bounds[0] < bounds.first_bounds[1] <= bounds.last_bounds[0]
    assert bounds.last_bounds[0] < bounds.last_bounds[1]


def test_a_url_that_is_not_there_is_reported_as_missing():
    """
    A real 404 from a real node, classified by its status code
    """
    _, header = first_header(
        requests_for(
            "CMIP5", experiment="abrupt4xCO2", variable="tas", time_frequency="mon"
        )
    )
    assert header.route is not None
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
