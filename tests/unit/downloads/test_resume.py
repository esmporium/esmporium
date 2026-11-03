"""
Tests of resuming an interrupted download
"""

import httpx
import pytest

from esmporium.datasets import DownloadOutcome
from esmporium.downloads import (
    DataNodeTransportError,
    ShortReadError,
    SizeMismatchError,
    fetch_to_path,
    part_path,
)


def test_a_partial_download_is_resumed_and_the_digest_covers_the_whole_file(  # noqa: PLR0913 - fixtures
    tmp_path, route, body, sha256_of, client_for, no_retry
):
    """
    A `.part` is continued with a `Range` request, and verified as a whole

    The digest has to be seeded with the bytes already on disk, or it would only
    describe the part fetched this time and a correct file would look corrupt.
    """
    dest = tmp_path / "tas.nc"
    part = part_path(dest)
    part.write_bytes(body[:400])

    seen = {}

    def handler(request):
        seen["range"] = request.headers.get("Range")
        return httpx.Response(
            206,
            content=body[400:],
            headers={"Content-Range": f"bytes 400-{len(body) - 1}/{len(body)}"},
        )

    outcome = fetch_to_path(
        client_for(handler),
        route,
        dest,
        expected_size=len(body),
        checksum=sha256_of(body),
        checksum_type="SHA256",
        retrying=no_retry,
    )

    assert seen["range"] == "bytes=400-"
    assert outcome.resumed
    # Only the bytes this call moved, so throughput is not inflated by what was
    # already on disk.
    assert outcome.bytes_written == len(body) - 400
    assert outcome.size_on_disk == len(body)
    assert outcome.checksum_verified
    assert dest.read_bytes() == body


def test_a_stale_part_at_full_size_is_discarded_rather_than_resumed(  # noqa: PLR0913 - fixtures
    tmp_path, route, body, sha256_of, client_for, no_retry
):
    """
    A `.part` already at the published size is thrown away and re-fetched

    Resuming it would ask for a range starting at the end of the file, which a
    correct server answers 416. Discarding first means we never ask.
    """
    dest = tmp_path / "tas.nc"
    part = part_path(dest)
    # As long as the real file, but not the real bytes.
    part.write_bytes(b"x" * len(body))

    seen = {}

    def handler(request):
        seen["range"] = request.headers.get("Range")
        return httpx.Response(200, content=body)

    outcome = fetch_to_path(
        client_for(handler),
        route,
        dest,
        expected_size=len(body),
        checksum=sha256_of(body),
        checksum_type="SHA256",
        retrying=no_retry,
    )

    # No Range header at all: we decided not to resume before asking.
    assert seen["range"] is None
    assert not outcome.resumed
    assert dest.read_bytes() == body
    assert outcome.checksum_verified


def test_a_416_discards_the_part_so_the_next_try_starts_clean(
    tmp_path, route, body, client_for, no_retry
):
    """
    A rejected range is recoverable, not permanent

    We cannot always know a `.part` is stale in advance -- without a published size
    there is nothing to compare against -- so the 416 path has to clean up after
    itself.
    """
    dest = tmp_path / "tas.nc"
    part = part_path(dest)
    part.write_bytes(body)

    client = client_for(lambda request: httpx.Response(416))

    # No `expected_size`, so the stale-part check above cannot help: this really does
    # reach the server and get a 416.
    with pytest.raises(DataNodeTransportError):
        fetch_to_path(client, route, dest, retrying=no_retry)

    # The blocker is gone, which is what makes the *next* attempt able to succeed.
    assert not part.exists()

    outcome = fetch_to_path(
        client_for(lambda request: httpx.Response(200, content=body)),
        route,
        dest,
        retrying=no_retry,
    )
    assert dest.read_bytes() == body
    assert not outcome.resumed


def test_a_server_ignoring_range_restarts_instead_of_appending(  # noqa: PLR0913 - fixtures
    tmp_path, route, body, sha256_of, client_for, no_retry
):
    """
    A `200` answer to a `Range` request truncates rather than appends

    Appending would duplicate the overlap and produce a file longer than the real
    one, which no checksum would catch if the file had none.
    """
    dest = tmp_path / "tas.nc"
    part = part_path(dest)
    part.write_bytes(body[:400])

    # 200, not 206: the whole body, as though we had never asked for a range.
    client = client_for(lambda request: httpx.Response(200, content=body))

    outcome = fetch_to_path(
        client,
        route,
        dest,
        expected_size=len(body),
        checksum=sha256_of(body),
        checksum_type="SHA256",
        retrying=no_retry,
    )

    assert not outcome.resumed
    assert outcome.size_on_disk == len(body)
    assert dest.read_bytes() == body


def test_resume_can_be_turned_off(tmp_path, route, body, client_for, no_retry):
    """`resume=False` ignores an existing `.part` entirely"""
    dest = tmp_path / "tas.nc"
    part_path(dest).write_bytes(body[:400])

    seen = {}

    def handler(request):
        seen["range"] = request.headers.get("Range")
        return httpx.Response(200, content=body)

    fetch_to_path(client_for(handler), route, dest, resume=False, retrying=no_retry)

    assert seen["range"] is None
    assert dest.read_bytes() == body


def test_a_resumed_transfer_that_comes_up_short_still_raises(
    tmp_path, route, body, client_for, no_retry
):
    """The size check counts the whole file, not just what this call added"""
    dest = tmp_path / "tas.nc"
    part_path(dest).write_bytes(body[:400])

    def handler(request):
        # Claims to continue, but stops early.
        return httpx.Response(
            206,
            content=body[400:600],
            headers={"Content-Range": f"bytes 400-599/{len(body)}"},
        )

    with pytest.raises(ShortReadError) as caught:
        fetch_to_path(
            client_for(handler),
            route,
            dest,
            expected_size=len(body),
            retrying=no_retry,
        )

    assert caught.value.got == 600
    assert caught.value.expected == len(body)


def test_a_node_which_will_not_say_how_big_the_file_is_is_still_held_to_it(
    tmp_path, route, body, client_for, no_retry
):
    """
    `Content-Range: bytes 400-8499/*` is a node declining to state the total

    The whole size then has to be worked out as what was already on disk plus what
    this response carries. Getting that wrong is not harmless: with no promise to
    compare against, a complete-but-wrong-sized file is reported as a
    `ShortReadError`, which *counts against the data node* and would slowly retire
    a working replica over what is really a publication error in the index.
    """
    dest = tmp_path / "tas.nc"
    part_path(dest).write_bytes(body[:400])

    def handler(request):
        return httpx.Response(
            206,
            content=body[400:],
            # `*` is what a node sends when it does not know the total.
            headers={"Content-Range": f"bytes 400-{len(body) - 1}/*"},
        )

    with pytest.raises(SizeMismatchError) as caught:
        fetch_to_path(
            client_for(handler),
            route,
            dest,
            # The index says the file is longer than the node actually serves.
            expected_size=len(body) + 50,
            retrying=no_retry,
        )

    # The node delivered every byte it said it would, so the disagreement is with
    # the index and the node's health must not move.
    assert caught.value.outcome == DownloadOutcome.SIZE_MISMATCH
    assert caught.value.published == len(body) + 50
    assert caught.value.served == len(body)
    assert not DownloadOutcome.SIZE_MISMATCH.counts_against_data_node
