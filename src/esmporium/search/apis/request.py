"""
Definition of requests to send to search APIs
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# Note for developers: we might need to move this elsewhere
# if it is needed for downloads too.
# Let's wait until we know we need that before we make such a move.
@dataclass(frozen=True)
class Request:
    """
    A ready-to-send HTTP request, minus the host
    """

    method: str
    """
    The HTTP method to use
    """

    path: str
    """The path to request, i.e. everything after the host"""

    params: dict[str, Any] | None = None
    """The query parameters to send, if any"""

    json_body: dict[str, Any] | None = None
    """The JSON body to send, if any"""
