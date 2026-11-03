"""
Observing the health of the ESGF data nodes

Every attempt to fetch bytes from a data node goes through
[fetch_to_path][esmporium.downloads.fetch_to_path], whatever it was driven by.
That is the one place where the route, the response code, any error,
the bytes moved and the time taken all exist together,
so it is where we observe an attempt.

Note that we define a `download` as getting one file, and a `call` as one HTTP attempt
against one route. A single download can result in several calls, across several data
nodes (retrying policy, then mirror fallback).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from esmporium.datasets import DownloadOutcome


@dataclass(frozen=True)
class DataNodeCall:
    """Health information saved per data node per fetch attempt"""

    data_node: str
    """See [DataNodeCallRecord.data_node_id][esmporium.db.schema.DataNodeCallRecord.data_node_id].

    Carried here as the data node's *name*, e.g. `esgf.nci.org.au`, rather than its
    database id, because this layer knows nothing about the database. The observer
    resolves it.
    """  # noqa: E501

    url: str
    """See [DataNodeCallRecord.url][esmporium.db.schema.DataNodeCallRecord.url]."""

    http_method: str
    """See [DataNodeCallRecord.http_method][esmporium.db.schema.DataNodeCallRecord.http_method]."""  # noqa: E501

    response_code: int | None
    """See [DataNodeCallRecord.response_code][esmporium.db.schema.DataNodeCallRecord.response_code]."""  # noqa: E501

    outcome: DownloadOutcome
    """See [DataNodeCallRecord.outcome][esmporium.db.schema.DataNodeCallRecord.outcome]."""  # noqa: E501

    error: Exception | None
    """
    The exception that made the attempt fail, or `None` on success

    Kept as the live exception so an observer can inspect its type or cause.
    """

    bytes_transferred: int
    """See [DataNodeCallRecord.bytes_transferred][esmporium.db.schema.DataNodeCallRecord.bytes_transferred]."""  # noqa: E501

    seconds: float
    """See [DataNodeCallRecord.seconds][esmporium.db.schema.DataNodeCallRecord.seconds]."""  # noqa: E501

    attempt_number: int
    """See [DataNodeCallRecord.attempt_number][esmporium.db.schema.DataNodeCallRecord.attempt_number]."""  # noqa: E501

    resumed: bool = False
    """See [DataNodeCallRecord.resumed][esmporium.db.schema.DataNodeCallRecord.resumed]."""  # noqa: E501

    file_id: int | None = None
    """See [DataNodeCallRecord.file_id][esmporium.db.schema.DataNodeCallRecord.file_id]."""  # noqa: E501

    data_access_id: int | None = None
    """See [DataNodeCallRecord.data_access_id][esmporium.db.schema.DataNodeCallRecord.data_access_id]."""  # noqa: E501

    @property
    def success(self) -> bool:
        """
        Whether the bytes arrived and passed whatever we could check

        Derived from [outcome][(c).outcome] rather than stored beside it, so the two
        cannot disagree. This is also why there is no `success` column on
        [DataNodeCallRecord][esmporium.db.schema.DataNodeCallRecord].

        Returns
        -------
        :
            `True` if this attempt succeeded
        """
        return self.outcome == DownloadOutcome.SUCCESS

    @property
    def throughput_mb_per_s(self) -> float | None:
        """
        How fast the bytes moved, in megabytes per second

        `None` when there is nothing to divide -- no time recorded, or no bytes moved
        -- rather than `0.0`, so that "we have no measurement" is not silently
        reported as "it was slow". That distinction is what stops a failed attempt
        dragging a data node's measured speed down.

        A megabyte here is 1,000,000 bytes, not 1,048,576, so the number means what a
        network engineer would expect.

        Returns
        -------
        :
            The throughput, or `None` if it cannot be worked out
        """
        if self.seconds <= 0.0 or self.bytes_transferred <= 0:
            return None

        return self.bytes_transferred / 1_000_000 / self.seconds


DataNodeCallObserver = Callable[[DataNodeCall], None]
"""Something told about each data node fetch attempt as it happens"""


def fan_out(*observers: DataNodeCallObserver) -> DataNodeCallObserver:
    """
    Turn several observers into one that calls each in turn

    This is how more than one observer is supported:
    [fetch_to_path][esmporium.downloads.fetch_to_path] still calls a single observer,
    and `fan_out` composes many into that one
    (e.g. record to the database *and* print progress to the console).

    The twin of [esmporium.search.fan_out][].

    Parameters
    ----------
    observers
        The observers to call, in order

    Returns
    -------
    :
        A single observer which calls each of `observers`

        With no arguments this is a valid no-op observer.
    """

    def observer(call: DataNodeCall) -> None:
        for obs in observers:
            obs(call)

    return observer
