"""
Recording how each search-API call performed.

The search layer emits one
[`SearchApiCallStat`][esmporium.esgf.search.recorder.SearchApiCallStat] per call it
makes and hands it to a [`Recorder`][esmporium.esgf.search.recorder.Recorder]. The
recorder is an injectable seam so that *where* the stat goes is not the search
layer's concern:

- [`NullRecorder`][esmporium.esgf.search.recorder.NullRecorder] drops it (the
  default, so a search needs no database), and
- [`FakeRecorder`][esmporium.esgf.search.recorder.FakeRecorder] keeps it in memory
  for tests to inspect.

A ``DbHealthRecorder`` that writes the stat to a table is deliberately *not* here
yet: persisting these stats (table + migration) is a separate, later step. When it
lands it is just another `Recorder`, and nothing in the search layer changes.
"""

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel

from esmporium.esgf.search.generation import SearchAPIGeneration


class SearchApiCallStat(BaseModel):
    """
    How one search-API call to one node for one project performed.

    An immutable value: the search layer builds one of these per call and records
    it. It carries provenance (which node/generation/project), the outcome
    (ok/status/error), and the performance (elapsed, matched count).
    """

    model_config = {"frozen": True}

    host: str
    """The node host the call went to."""
    generation: SearchAPIGeneration
    """The search-API generation of that node."""
    project: str
    """The project this call searched."""
    timestamp: datetime
    """When the call was made."""
    ok: bool
    """Whether the call succeeded (a usable 2xx response)."""
    elapsed_seconds: float
    """How long the call took."""
    status_code: int | None = None
    """The HTTP status, or ``None`` if the request never got a response."""
    num_matched: int | None = None
    """How many datasets the node reported matched, if known."""
    error: str | None = None
    """A short description of what went wrong, if the call failed."""


class Recorder(Protocol):
    """Something that can record a search-API call stat."""

    def record(self, stat: SearchApiCallStat) -> None:
        """Record one call stat."""
        ...


class NullRecorder:
    """A recorder that drops every stat; the default when nobody is tracking."""

    def record(self, stat: SearchApiCallStat) -> None:
        """Do nothing."""


class FakeRecorder:
    """A recorder that keeps stats in memory, for tests to inspect via ``stats``."""

    def __init__(self) -> None:
        self.stats: list[SearchApiCallStat] = []

    def record(self, stat: SearchApiCallStat) -> None:
        """Append the stat to the in-memory list."""
        self.stats.append(stat)
