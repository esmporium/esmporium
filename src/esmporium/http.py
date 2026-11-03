"""
Shared HTTP helpers

Rendering a request so a human can reproduce it is not specific to searching or to
downloading, so it lives here rather than in either.
"""

from __future__ import annotations

import logging
import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx


def curl_equivalent(request: httpx.Request) -> str:
    """
    Render an httpx request as a `curl` command which reproduces it

    Every piece is shell-quoted, so the result is safe to paste into a terminal.

    Parameters
    ----------
    request
        The request to render

    Returns
    -------
    :
        A `curl` command equivalent to `request`
    """
    parts = ["curl", "-X", request.method]
    for name, value in request.headers.items():
        parts += ["-H", shlex.quote(f"{name}: {value}")]

    body = request.content
    if body:
        parts += ["--data", shlex.quote(body.decode("utf-8", errors="replace"))]

    parts.append(shlex.quote(str(request.url)))

    return " ".join(parts)


def log_request_as_url_and_curl(  # noqa: PLR0913 - one argument per thing a log line needs
    logger: logging.Logger,
    host: str,
    request: httpx.Request,
    *,
    what: str,
    log_level: int = logging.DEBUG,
    host_field: str,
    extra: dict[str, object] | None = None,
) -> None:
    """
    Log a request we are about to send, at `DEBUG` by default

    Both the human-readable message and a structured `extra` carry the URL and the
    `curl` equivalent, so a request can be reproduced either by reading the log or by
    machine.

    Parameters
    ----------
    logger
        The logger to log to, i.e. the *caller's* logger

        Taken as an argument rather than this module having its own, so that a record
        is attributed to the code that made the request
        (`esmporium.search.search.firing`, `esmporium.downloads.fetching`) and not to
        this helper. Filtering logs by module is a thing people do, and
        `esmporium.http` would be a less useful name than either of those.

    host
        The host the request is going to

    request
        The request

    what
        What kind of request this is, e.g. `"search"` or `"download"`

        Used to open the log message, so phrase it as a noun.

    log_level
        Level at which to log

    host_field
        What to call `host` in the structured `extra`, e.g. `"search_api_host"` or
        `"data_node"`

        Named by the caller rather than fixed, because a log aggregator should not
        have to guess whether a `host` field means an index node or a data node.

    extra
        Any further structured fields to attach

        Merged under the ones this function always sets, which therefore win: a
        caller cannot accidentally overwrite the URL with something else.
    """
    if not logger.isEnabledFor(log_level):
        # Skip rendering if the logger is not enabled for the given level: building
        # the curl equivalent is not free, and this is on the hot path.
        return

    url = str(request.url)
    curl = curl_equivalent(request)
    logger.log(
        log_level,
        "%s request to %s\n%s %s\n%s",
        what,
        host,
        request.method,
        url,
        curl,
        extra={
            **(extra or {}),
            host_field: host,
            "http_method": request.method,
            "http_url": url,
            "http_curl": curl,
        },
    )
