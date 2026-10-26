"""
DELETE ME once downloading lands

A one-off probe that takes the data access routes esmporium recorded and proves they
really lead to data we can read. It exists to answer one question that the schema
cannot answer by itself:

> A netCDF file and a kerchunk reference set are recorded as two different
> `container_format`s reached by the same `service`. Do they actually converge when
> you open them, or are they different worlds?

So it opens one of each and prints the same kind of summary for both. If the two
summaries look alike, the two-column `service`/`container_format` model is carrying
its weight; if they do not, whatever is different is something the download step
needs to know before it is written.

What it found when it was written (2026-10-26), which is the answer the download step
actually needs:

- **The netCDF routes are real and our record of them is right.** `HEAD` returns the
  exact `content-length` we recorded, the magic bytes are netCDF's own, and
  `xarray.open_dataset` over HTTPS gives back a real dataset -- dims, data vars and
  coords -- without downloading the file first.
- **Every kerchunk route is advertised and dead.** All twelve west CMIP6 items
  sampled publish a `reference_file` asset, and all twelve `.kerchunk.parq` hrefs
  return 404. So virtual access via ESGF-NG is announced but not yet served, and the
  convergence question cannot be answered for it yet -- not because the two formats
  differ, but because one of them is not there.
- **`container_format` is a routing hint, not a complete instruction.** `netcdf` is
  two formats: classic needs the `scipy` engine, netCDF-4/HDF5 needs `h5netcdf`, and
  ESGF serves both -- the first file this script picked was classic and the smallest
  was HDF5. So even a classified route needs its first four bytes read before it can
  be opened. That is the same argument as this PR's two columns, one level further
  down, and the download step will meet it.

None of that makes the whole-version branch of the model wrong -- the assets are
really published, and a real zarr or icechunk store would land in the same place --
but it does mean nothing can be fetched through it today, and a download step which
assumed otherwise would fail on every dataset.

esmporium itself records *where* the bytes are and deliberately does not open them,
which is why this is a script and not a feature, and why `xarray`, `zarr`, `fsspec`
and `kerchunk` are dev-only dependencies. When the download step lands, it supersedes
this and both the script and those four dependencies should go.

Usage
-----
    python scripts/verify_data_access_routes.py --search
    python scripts/verify_data_access_routes.py --database path/to.db
"""

from __future__ import annotations

import argparse
import logging
from typing import TYPE_CHECKING, Any

import fsspec
import httpx
import xarray as xr
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

from esmporium.datasets import AccessService, ContainerFormat
from esmporium.db import (
    DataAccess,
    File,
    configure_sqlite_for_concurrency,
    ingest_parsed_documents,
    record_solr_file_access,
)
from esmporium.db.migrate import upgrade_to_head
from esmporium.query import QueryCMIP6
from esmporium.search import (
    INBUILT_SEARCH_API_FACADE_STORE,
    SOLR_FORMAT_TAG,
    build_list_selector,
    search_single_project,
)

if TYPE_CHECKING:
    from sqlalchemy import Engine

logger = logging.getLogger(__name__)

MAGIC_NUMBERS = {
    b"CDF\x01": "netCDF classic",
    b"CDF\x02": "netCDF 64-bit offset",
    b"\x89HDF": "HDF5 (netCDF-4)",
    b"PAR1": "Apache Parquet",
}
"""What the first four bytes of a file say it is"""

ENGINE_FOR_MAGIC = {
    b"CDF\x01": "scipy",
    b"CDF\x02": "scipy",
    b"\x89HDF": "h5netcdf",
}
"""
Which xarray engine reads a file, given what its first four bytes say it is

Worth noticing, because it is the same argument as this PR's two columns one level
down: ESGF records `netcdf` as the container format, but "netCDF" is two formats --
classic and netCDF-4/HDF5 -- read by two different engines. So even a route we have
classified still needs its bytes looked at before it can be opened, and
`container_format` is a routing hint rather than a complete instruction.
"""

WEST = "search.west.esgf.io"
"""The one deployment publishing kerchunk routes, so the only place to find one"""


def fill_a_database(engine: Engine) -> None:
    """
    Run one small live search into `engine`, so there is something to probe

    Searches west CMIP6, because that is the only deployment which publishes a
    kerchunk route, which is half of what this script is for.
    """
    upgrade_to_head(engine)
    facade = INBUILT_SEARCH_API_FACADE_STORE.get_api_facade_for_project_from_host(
        "CMIP6", WEST
    )
    query = QueryCMIP6(experiment_id="historical", variable_id="tas", frequency="mon")

    outcome = search_single_project(
        query, build_list_selector([facade]), limit=2, warn_on_pagination=False
    )
    documents = [doc for pile in outcome.parsed_docs.values() for doc in pile]
    print(f"searched {WEST}: {len(documents)} dataset version(s)")

    with Session(engine) as session:
        ingested = ingest_parsed_documents(session, documents)
        if facade.search_api.raw_docs_format_tag == SOLR_FORMAT_TAG:
            record_solr_file_access(session, facade, ingested)
        session.commit()


def first_four_bytes(client: httpx.Client, url: str) -> bytes | None:
    """
    Fetch only the first four bytes of `url`, with a ranged GET

    `None` if the server will not serve them. Not an error: a route which is
    advertised and dead is one of the things this script exists to discover.
    """
    response = client.get(url, headers={"Range": "bytes=0-3"})
    if response.is_error:
        return None

    return response.content[:4]


def describe_bytes(
    client: httpx.Client, route: DataAccess, size: int | None
) -> bytes | None:
    """
    Say what is really at the far end of a route, cheaply

    A `HEAD` to check the size we recorded, and four bytes to check the format. Four
    bytes rather than the whole thing because a CMIP file is routinely hundreds of
    megabytes, and the magic number is as convincing as the rest of it.
    """
    head = client.head(route.url)
    reported = head.headers.get("content-length")
    print(f"    HEAD {head.status_code}, content-length {reported}")
    if head.is_error:
        print("    *** advertised but not there: nothing is served at this URL ***")
        return None

    if size is not None and reported is not None:
        agrees = "agrees with" if int(reported) == size else "DISAGREES with"
        print(f"    {agrees} the recorded size of {size}")

    magic = first_four_bytes(client, route.url)
    if magic is None:
        print("    would not serve a byte range")
        return None

    print(f"    first four bytes {magic!r}: {MAGIC_NUMBERS.get(magic, 'unrecognised')}")

    return magic


def open_netcdf(url: str, magic: bytes | None) -> Any:
    """
    Open a netCDF route over HTTPS, rather than downloading it first

    The engine comes from the magic bytes, because `container_format` of `netcdf`
    does not say which netCDF: classic needs `scipy`, netCDF-4/HDF5 needs
    `h5netcdf`, and ESGF serves both.
    """
    engine = ENGINE_FOR_MAGIC.get(magic or b"")
    if engine is None:
        msg = f"no engine known for a file starting {magic!r}"
        raise ValueError(msg)

    # A generous timeout because this reads over HTTP rather than from disk, and
    # scipy's classic-netCDF reader makes many small reads rather than a few big
    # ones. The smallest recorded file is chosen for the same reason.
    opened = fsspec.open(url, timeout=600).open()

    return xr.open_dataset(opened, engine=engine)


def open_kerchunk(url: str, magic: bytes | None = None) -> Any:
    """
    Open a kerchunk route as a dataset

    The reference set is a map from zarr chunk keys to byte ranges in the original
    netCDF files, so this reads as zarr while the bytes still come from the files the
    file-level routes point at. That is the interesting part: the same bytes, reached
    two ways.
    """
    del magic  # the reference set's own format is fixed; only netCDF needs the hint

    reference_filesystem = fsspec.filesystem(
        "reference",
        fo=url,
        remote_protocol="https",
        target_options={"asynchronous": False},
        remote_options={"asynchronous": False},
    )

    return xr.open_dataset(
        reference_filesystem.get_mapper(""), engine="zarr", consolidated=False
    )


def summarise(opened: Any) -> None:
    """Print the few things worth comparing between two ways of opening data"""
    print(f"    dims       {dict(opened.sizes)}")
    print(f"    data vars  {list(opened.data_vars)}")
    print(f"    coords     {list(opened.coords)}")


def probe(engine: Engine) -> None:
    """Probe one route of each kind recorded in `engine`"""
    with (
        Session(engine) as session,
        httpx.Client(follow_redirects=True, timeout=120.0) as client,
    ):
        size_of = {
            stored.id: stored.size for stored in session.exec(select(File)).all()
        }

        # The smallest one: this opens it over HTTP, so size is the difference
        # between a few seconds and a few minutes.
        netcdf = session.exec(
            select(DataAccess)
            .join(File, DataAccess.file_id == File.id)
            .where(
                DataAccess.service == AccessService.HTTPS,
                DataAccess.container_format == ContainerFormat.NETCDF,
            )
            .order_by(File.size)
        ).first()
        kerchunk = session.exec(
            select(DataAccess).where(
                DataAccess.container_format == ContainerFormat.KERCHUNK,
                DataAccess.file_id.is_(None),
            )
        ).first()

        magic_of: dict[str, bytes | None] = {}
        for label, route in (("netCDF file", netcdf), ("kerchunk store", kerchunk)):
            print(f"\n{label}")
            if route is None:
                print("    no such route recorded, so nothing to probe")
                continue

            print(f"    {route.service} / {route.container_format}")
            print(f"    {route.url}")
            magic_of[label] = describe_bytes(client, route, size_of.get(route.file_id))

        print("\nopening them")
        for label, route, opener in (
            ("netCDF file", netcdf, open_netcdf),
            ("kerchunk store", kerchunk, open_kerchunk),
        ):
            print(f"\n  {label}")
            if route is None:
                print("    nothing to open")
                continue

            if magic_of.get(label) is None:
                print(
                    "    nothing is served at this URL, so there is nothing to "
                    "open -- see above"
                )
                continue

            try:
                summarise(opener(route.url, magic_of.get(label)))
            except Exception as error:
                # Printed rather than raised: this script exists to find out whether
                # these two converge, and "it does not open like that" is an answer
                # the download step needs, not a crash to be fixed here.
                print(f"    could not open: {type(error).__name__}: {error}")


def main() -> None:
    """Probe the routes in a database, filling a temporary one first if asked"""
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--database", help="an existing esmporium database to probe")
    group.add_argument(
        "--search",
        action="store_true",
        help="run one small live search into a temporary database first",
    )
    arguments = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    if arguments.search:
        engine = configure_sqlite_for_concurrency(
            create_engine(
                "sqlite://",
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
        )
        # The schema comes from the migrations, not `create_all`: this probes what a
        # real database holds, and `fill_a_database` migrates it.
        fill_a_database(engine)
    else:
        engine = configure_sqlite_for_concurrency(
            create_engine(f"sqlite:///{arguments.database}")
        )

    probe(engine)


if __name__ == "__main__":
    main()
