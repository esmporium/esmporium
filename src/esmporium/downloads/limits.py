"""
Limiting how much we ask of one data node at once
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext

DataNodeLimiter = Callable[[str], AbstractContextManager[None]]
"""
Decides when a fetch against one data node may start

Given a data node's name, hands back a context manager which is entered for as long as
a fetch against that node is in flight. A limiter which has nothing to limit hands back
a context manager that enters immediately.

The context manager is entered around the whole of
[fetch_to_path][esmporium.downloads.fetch_to_path], so a route's retries are inside it
too: a node which asked us to slow down is not then hit by that attempt's retries on
top of everything else in flight.
"""

DEFAULT_PER_DATA_NODE = 4
"""
How many fetches may run at once against any one data node, by default

A judgement, not a measurement: enough that a single node can fill a reasonable
connection, few enough to stay a polite guest on a shared archive. A data node which
starts refusing us records
[BLOCKED][esmporium.datasets.DownloadOutcome.BLOCKED] attempts, so there is something
to look at if this turns out to be too high for a particular host.
"""


def no_data_node_limit(data_node: str) -> AbstractContextManager[None]:
    """
    Let every fetch start straight away, whatever node it is for

    Parameters
    ----------
    data_node
        The data node a fetch is about to be made against. Ignored.

    Returns
    -------
    :
        A context manager which enters immediately and does nothing
    """
    return nullcontext()


def build_data_node_limiter(
    per_data_node: int = DEFAULT_PER_DATA_NODE,
) -> DataNodeLimiter:
    """
    Build a limiter which allows a fixed number of fetches at once per data node

    A worker whose data node is already at its limit **waits** for one of that node's
    fetches to finish, rather than being sent to a different node. Which node serves a
    file stays the selector's decision
    (see [DataNodeSelector][esmporium.downloads.DataNodeSelector]), so a run never
    silently fetches from a node it ranked worse.

    Parameters
    ----------
    per_data_node
        How many fetches may be in flight against one data node at once

    Returns
    -------
    :
        A limiter, safe to share between threads

        Nodes are counted separately, so one busy node does not hold up another, and a
        node is tracked from the first time it is asked about.

    Raises
    ------
    ValueError
        `per_data_node` is less than one, which would stop every download rather than
        pace it. Pass [no_data_node_limit][(m).] to not limit anything.

    Examples
    --------
    >>> limiter = build_data_node_limiter(2)
    >>> with limiter("esgf.example.org"):
    ...     print("fetching")
    fetching
    """
    if per_data_node < 1:
        msg = (
            f"per_data_node must be at least 1, got {per_data_node}. "
            "A limit of zero would stop every download rather than pace it; "
            "to not limit anything, use esmporium.downloads.no_data_node_limit."
        )
        raise ValueError(msg)

    semaphores: dict[str, threading.Semaphore] = {}
    guard = threading.Lock()

    @contextmanager
    def slot(data_node: str) -> Iterator[None]:
        # The lock is held only long enough to find or make this node's semaphore,
        # never while fetching, so workers for different nodes do not queue behind
        # each other here.
        with guard:
            semaphore = semaphores.setdefault(
                data_node, threading.Semaphore(per_data_node)
            )

        with semaphore:
            yield

    return slot
