"""
Observing how the ESGF data nodes behave when asked for a header

Every attempt to read a header goes through
[read_header][esmporium.headers.read_header], whatever drove it. That is
the one place where the route, the response code, any error, the bytes moved and the
time taken all exist together, so it is where an attempt is observed -- the same
argument [esmporium.downloads.health][] makes.

Note the vocabulary, which matches the download layer's on purpose: a **read** is
getting one file's header, and a **call** is one HTTP attempt against one route. A
single read can make several calls, across several routes and several data nodes.

**Observed separately from downloads, into its own table, and the reason is a
measurement rather than a preference.** [esmporium.db.download_health][] ranks a data
node partly by the median throughput of its successful calls. A header read moves
around 64 KiB and spends most of its time on the connection, so it looks like a
fraction of a megabyte per second; and because one header serves a whole simulation
while a download is per file, header calls would still outnumber download calls across
a run. Pooling them would drag every node's measured throughput down to the speed of
its handshake and steer downloads away from the fastest replicas. What *is* worth
sharing is whether a node answers at all, and that is a question both tables can be
asked.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from esmporium.datasets import HeaderReadOutcome


@dataclass(frozen=True)
class HeaderReadCall:
    """Health information saved per data node per header-read attempt"""

    data_node: str
    """
    See [HeaderReadCallRecord.data_node_id][esmporium.db.schema.HeaderReadCallRecord.data_node_id].

    Carried here as the data node's *name*, e.g. `esgf.nci.org.au`, rather than its
    database id, because this layer knows nothing about the database. The observer
    resolves it. **Not the host in the URL**: the two differ, e.g. `ceda.ac.uk` serves
    over `dap.ceda.ac.uk`.
    """  # noqa: E501

    url: str
    """See [HeaderReadCallRecord.url][esmporium.db.schema.HeaderReadCallRecord.url]."""

    http_method: str
    """See [HeaderReadCallRecord.http_method][esmporium.db.schema.HeaderReadCallRecord.http_method]."""  # noqa: E501

    response_code: int | None
    """See [HeaderReadCallRecord.response_code][esmporium.db.schema.HeaderReadCallRecord.response_code]."""  # noqa: E501

    outcome: HeaderReadOutcome
    """See [HeaderReadCallRecord.outcome][esmporium.db.schema.HeaderReadCallRecord.outcome]."""  # noqa: E501

    error: Exception | None
    """
    The exception that made the attempt fail, or `None` on success

    Kept as the live exception so an observer can inspect its type or cause. That
    matters more here than for a download: whether a connection failure was a refusal,
    a DNS failure or a TLS failure is decided by looking at `__cause__`, and an
    observer may want to record which.
    """

    bytes_requested: int
    """See [HeaderReadCallRecord.bytes_requested][esmporium.db.schema.HeaderReadCallRecord.bytes_requested]."""  # noqa: E501

    bytes_transferred: int
    """See [HeaderReadCallRecord.bytes_transferred][esmporium.db.schema.HeaderReadCallRecord.bytes_transferred]."""  # noqa: E501

    seconds: float
    """
    See [HeaderReadCallRecord.seconds][esmporium.db.schema.HeaderReadCallRecord.seconds].

    **The measurement that matters for a header read**, in the way throughput is the
    one that matters for a download. Reading a few kilobytes is almost entirely
    connection setup and one round trip, so this is latency, and a node's typical
    latency is what should decide whether to ask it first.
    """  # noqa: E501

    attempt_number: int
    """See [HeaderReadCallRecord.attempt_number][esmporium.db.schema.HeaderReadCallRecord.attempt_number]."""  # noqa: E501

    range_honoured: bool = True
    """See [HeaderReadCallRecord.range_honoured][esmporium.db.schema.HeaderReadCallRecord.range_honoured]."""  # noqa: E501

    file_id: int | None = None
    """See [HeaderReadCallRecord.file_id][esmporium.db.schema.HeaderReadCallRecord.file_id]."""  # noqa: E501

    data_access_id: int | None = None
    """See [HeaderReadCallRecord.data_access_id][esmporium.db.schema.HeaderReadCallRecord.data_access_id]."""  # noqa: E501

    @property
    def success(self) -> bool:
        """
        Whether the bytes arrived

        Derived from [outcome][(c).outcome] rather than stored beside it, so the two
        cannot disagree. This is also why there is no `success` column on
        [HeaderReadCallRecord][esmporium.db.schema.HeaderReadCallRecord].

        Returns
        -------
        :
            `True` if this attempt succeeded
        """
        return self.outcome == HeaderReadOutcome.SUCCESS


HeaderReadCallObserver = Callable[[HeaderReadCall], None]
"""Something told about each header-read attempt as it happens"""


def fan_out(*observers: HeaderReadCallObserver) -> HeaderReadCallObserver:
    """
    Turn several observers into one that calls each in turn

    This is how more than one observer is supported:
    [read_header][esmporium.headers.read_header] still calls a single
    observer, and `fan_out` composes many into that one (e.g. record to the database
    *and* print progress to the console).

    The twin of [esmporium.downloads.fan_out][] and [esmporium.search.fan_out][].

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

    def observer(call: HeaderReadCall) -> None:
        for obs in observers:
            obs(call)

    return observer
