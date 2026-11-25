"""
Record the bytes a header and time-bounds read touches, for use as test fixtures

Reads one file's header, and the first and last values of its time coordinate and
bounds, from a live data node, and keeps only the byte ranges the readers asked for:
a few kilobytes of a file that can be hundreds of megabytes. The unit tests replay
them through `esmporium.headers.HeldBytesSource`, so they read real files without a
network connection.

Usage:

    uv run python scripts/record_header_ranges.py URL DATA_NODE OUTPUT_STEM

which writes `OUTPUT_STEM.bin` (the pieces, one after another) and `OUTPUT_STEM.json`
(where each piece sits in the file, and the file's size). The recordings in
`tests/test-data/headers/` were made with it.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import httpx

from esmporium.datasets import AccessService, ContainerFormat
from esmporium.downloads import Route
from esmporium.headers import ByteSource, RangeSource, read_file_header


class RecordingSource:
    """A byte source which remembers every range it is asked for"""

    def __init__(self, inner: ByteSource) -> None:
        self.inner = inner
        self.reads: list[tuple[int, int]] = []

    @property
    def size(self) -> int:
        """The inner source's size"""
        return self.inner.size

    @property
    def bytes_read(self) -> int:
        """The inner source's count of bytes read"""
        return self.inner.bytes_read

    @property
    def n_requests(self) -> int:
        """The inner source's count of requests"""
        return self.inner.n_requests

    def read(self, offset: int, count: int) -> bytes:
        """Note the range, then read it from the inner source"""
        self.reads.append((offset, count))

        return self.inner.read(offset, count)


def merged(reads: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """
    Merge overlapping and touching ranges

    Parameters
    ----------
    reads
        `(offset, count)` pairs

    Returns
    -------
    :
        `(start, end)` pairs, sorted and disjoint, `end` exclusive
    """
    spans: list[list[int]] = []
    for start, end in sorted((offset, offset + count) for offset, count in reads):
        if spans and start <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], end)
        else:
            spans.append([start, end])

    return [(start, end) for start, end in spans]


def main(url: str, data_node: str, stem: Path) -> None:
    """
    Record one file

    Parameters
    ----------
    url
        The file's HTTPS URL

    data_node
        The data node serving it

    stem
        Where to write, without a suffix
    """
    route = Route(
        url=url,
        data_node=data_node,
        service=AccessService.HTTPS,
        container_format=ContainerFormat.NETCDF,
        data_access_id=None,
    )
    with (
        httpx.Client(follow_redirects=True) as client,
        RangeSource(client, route) as inner,
    ):
        recording = RecordingSource(inner)
        header = read_file_header(recording, time_bounds=True)
        spans = merged(recording.reads)
        blob = b"".join(inner.read(start, end - start) for start, end in spans)

    stem.with_suffix(".bin").write_bytes(blob)
    manifest = {
        "description": (
            "The byte ranges that reading this file's header and its first and last "
            "time values touched, for replaying that read offline. Each piece is "
            "stored in the .bin beside this, one after another, in this order; every "
            "other byte of the file was not read."
        ),
        "url": url,
        "file": url.rsplit("/", 1)[-1],
        "file_size": header.file_size,
        "fetched": dt.date.today().isoformat(),
        "pieces": [{"offset": start, "length": end - start} for start, end in spans],
    }
    stem.with_suffix(".json").write_text(json.dumps(manifest, indent=2) + "\n")

    print(
        f"{header.layout.value}: {header.n_requests} request(s), "
        f"{header.bytes_read} bytes fetched, {len(blob)} kept in {len(spans)} piece(s)"
    )
    print(header.time_bounds)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], Path(sys.argv[3]))
