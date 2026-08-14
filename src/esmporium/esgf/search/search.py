"""
The search entry point: fan a query out over nodes and return raw results.

[`search`][esmporium.esgf.search.search.search] lowers a query once, then for each
project it walks nodes via the
[`EndPointSelector`][esmporium.esgf.search.selector.EndPointSelector]: it builds the
node's request, sends it (retrying the same node on transient failure), records a
performance stat, and — for this step — keeps the first node that returns results
(a placeholder for the eventual merge across nodes). The per-project raw payloads
are combined into a ``{project: raw_json}`` dict.

This raw-JSON dict is a deliberate throwaway intermediate: a later step returns
parsed dataset objects and merges results across nodes rather than taking the first.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from esmporium.esgf.canonical import CanonicalQuery
from esmporium.esgf.query_models import _ESGFQueryBase
from esmporium.esgf.search.client import search_once
from esmporium.esgf.search.generation import get_generation_config
from esmporium.esgf.search.hosts import KNOWN_NODES, IndexNode
from esmporium.esgf.search.recorder import NullRecorder, Recorder, SearchApiCallStat
from esmporium.esgf.search.request import UnrepresentableFacetError, build_request
from esmporium.esgf.search.selector import EndPointSelector, make_default_selector

# Generous per-call timeout for a federated index node (the client is reused across
# the whole fan-out).
_DEFAULT_TIMEOUT = httpx.Timeout(45.0)


class NoProjectToSearchError(ValueError):
    """Raised when a query names no project to search.

    A project is required: it selects which collection/params to search and what to
    key the results under. Every facet is optional; the target project is not.
    """

    def __init__(self) -> None:
        super().__init__("no project to search: set the query's `project`")


@dataclass(frozen=True)
class _SearchContext:
    """The parts of a search that do not vary between projects."""

    client: httpx.Client
    selector: EndPointSelector
    retries: int
    recorder: Recorder
    canonical: CanonicalQuery


def search(  # noqa: PLR0913 - each knob is a distinct, intentional part of the API
    query: _ESGFQueryBase,
    *,
    extra_nodes: Sequence[IndexNode] = (),
    nodes: Sequence[IndexNode] = KNOWN_NODES,
    end_point_selector: EndPointSelector | None = None,
    retries: int = 0,
    recorder: Recorder | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """
    Search a query across index nodes and return each project's raw JSON.

    Parameters
    ----------
    query
        The query, in any dialect. Its ``project`` field selects which project(s) to
        search; each is resolved independently.

    extra_nodes
        Nodes to search in addition to ``nodes`` (the common "defaults plus my own
        host" case). A user host requires its generation, hence an `IndexNode`.

    nodes
        The base candidate nodes; defaults to
        [`KNOWN_NODES`][esmporium.esgf.search.hosts.KNOWN_NODES]. Pass your own to
        replace the defaults entirely.

    end_point_selector
        The fallback policy; defaults to walking ``nodes + extra_nodes`` in order.

    retries
        Extra same-node attempts on a transient failure, passed to each call.

    recorder
        Where per-call performance stats go; defaults to dropping them.

    client
        An httpx client to reuse; if omitted, one is created and closed here.

    Returns
    -------
    :
        ``{project_lowercased: raw_json_or_None}`` — the first results found per
        project, or ``None`` if none were found.

    Raises
    ------
    NoProjectToSearchError
        If the query names no project.
    """
    if not query.project:
        raise NoProjectToSearchError

    selector = end_point_selector or make_default_selector((*nodes, *extra_nodes))
    context_parts = (
        selector,
        retries,
        recorder or NullRecorder(),
        query.to_canonical(),
    )

    if client is not None:
        return _search_all_projects(_SearchContext(client, *context_parts), query)
    with httpx.Client(timeout=_DEFAULT_TIMEOUT) as owned_client:
        return _search_all_projects(_SearchContext(owned_client, *context_parts), query)


def _search_all_projects(
    context: _SearchContext, query: _ESGFQueryBase
) -> dict[str, Any]:
    """Resolve every project the query targets, keyed by lowercased project name."""
    return {
        project.lower(): _search_one_project(context, query, project)
        for project in query.project
    }


def _search_one_project(
    context: _SearchContext, query: _ESGFQueryBase, project: str
) -> dict[str, Any] | None:
    """Walk nodes for one project until one returns results (or none are left)."""
    sub_query = query.model_copy(update={"project": (project,)})
    attempt = 0
    while (node := context.selector(sub_query, attempt)) is not None:
        config = get_generation_config(node.generation)
        try:
            request = build_request(context.canonical, project, config)
        except UnrepresentableFacetError:
            # This generation cannot express the query for this project; there is no
            # call to make or record, so just try the next node.
            attempt += 1
            continue

        result = search_once(
            context.client, node, request, config, retries=context.retries
        )
        context.recorder.record(
            SearchApiCallStat(
                host=node.host,
                generation=node.generation,
                project=project,
                timestamp=datetime.now(timezone.utc),
                ok=result.ok,
                elapsed_seconds=result.elapsed_seconds,
                status_code=result.status_code,
                num_matched=result.num_matched,
                error=result.error,
            )
        )
        if result.has_results:
            return result.data
        attempt += 1
    return None
