"""
Search API implementation

This contains our search API interface,
the pre-built search APIs we know how to talk to
and pre-built options for how to pick between them.
TODO: put dev ref to FUTURE-DOCS.md (to be updated to docs) as comment
"""
# @Developers: we strongly recommend reading the section
# "Search and query and vocab explanation"
# in FUTURE-DOCS.md to help understand why there are multiple instances of SearchAPI
# for a given host: this is a deliberate decision to make error handling simpler,
# even though it means that our SearchAPI does not map one-to-one onto ESGF hosts.
# TODO: update the reference to a docs page
# once we migrate FUTURE-DOCS.md into actual docs.

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from tenacity import Retrying

from esmporium.query import QueryCanonical
from esmporium.search.esgf_generations import (
    ESGF1Solr,
    ESGF15Bridge,
    ESGFNGStac,
    Request,
    SearchAPIGeneration,
    SolrCMIP5Parameters,
    SolrCMIP6Parameters,
    SolrCMIP7Parameters,
    StacCMIP5Parameters,
    StacCMIP6Parameters,
    StacCMIP7Parameters,
)
from esmporium.search.retry import build_transient_retrying


@dataclass(frozen=True)
class SearchAPI:
    """
    One search endpoint we can hit
    """

    host: str
    """The host to send requests to, e.g. `esgf.nci.org.au`"""

    generation: SearchAPIGeneration
    """
    The (request/wire) format this host speaks

    This also handles the vocabulary that this API understands
    """

    retrying: Retrying
    """The retry policy to use when hitting this API"""

    timeout: float = 30.0
    """
    How long to wait on a single request to this host, in seconds

    Most hosts reply quickly e.g. 5 seconds.
    The slowest hosts take around 30 seconds.
    In general, you want to make this as short as possible
    because waiting for a reply that will never come
    can kill the speed in your retries.
    """

    scheme: str = "https"
    """
    The URL scheme to reach this host over
    """

    def url(self, request: Request) -> str:
        """
        Build the full URL for a request against this host

        Parameters
        ----------
        request
            The request to make to this host

        Returns
        -------
        :
            The URL to send `request` to
        """
        return f"{self.scheme}://{self.host}{request.path}"


# Pre-built search API generations.
# See note in docstring at the top of the module for why it is like this.
SOLR_CMIP5 = ESGF1Solr(params=SolrCMIP5Parameters)
"""
Search API that hits ESGF1 using SOLR syntax and expects CMIP5 style queries
"""
SOLR_CMIP6 = ESGF1Solr(params=SolrCMIP6Parameters)
"""
Search API that hits ESGF1 using SOLR syntax and expects CMIP6 style queries
"""
SOLR_CMIP7 = ESGF1Solr(params=SolrCMIP7Parameters)
"""
Search API that hits ESGF1 using SOLR syntax and expects CMIP7 style queries
"""

BRIDGE_CMIP5 = ESGF15Bridge(params=SolrCMIP5Parameters)
"""
Search API that hits ESGF1.5 bridge and expects CMIP5 style queries
"""
BRIDGE_CMIP6 = ESGF15Bridge(params=SolrCMIP6Parameters)
"""
Search API that hits ESGF1.5 bridge and expects CMIP5 style queries
"""

STAC_CMIP5 = ESGFNGStac(params=StacCMIP5Parameters)
"""
Search API that hits ESGF-NG STAC API and expects CMIP5 style queries
"""
STAC_CMIP6 = ESGFNGStac(params=StacCMIP6Parameters)
"""
Search API that hits ESGF-NG STAC API and expects CMIP6 style queries
"""
STAC_CMIP7 = ESGFNGStac(params=StacCMIP7Parameters)
"""
Search API that hits ESGF-NG STAC API and expects CMIP7 style queries
"""


SearchAPISelector = Callable[[QueryCanonical, int], SearchAPI | None]
"""
Chooses which endpoint to try next

Given the canonical query and a 0-based attempt index, returns the next
[SearchAPI][esmporium.search.search_api.SearchAPI] to try, or `None` to stop.
"""


class SelectorOfferedNoAPIError(ValueError):
    """
    Raised when a selector has no endpoint to offer for a query, from the very start

    Asking for a search is asking for it to happen.
    Handing back an empty answer would read as
    "we asked, and nobody had anything for you",
    when what happened is that nobody was asked at all:
    a selector with an empty list, or one whose rules
    rule out every endpoint for this query, is a bug in the calling code
    and is worth saying out loud rather than quietly returning nothing.

    This is only about having nobody to ask.
    Endpoints which were asked and did not answer are a different thing,
    and are reported as such.
    """

    def __init__(self, canonical: QueryCanonical, selector: SearchAPISelector) -> None:
        """
        Initialise the error

        Parameters
        ----------
        canonical
            The query we were going to ask about

        selector
            The selector which had nothing to offer for it
        """
        self.canonical = canonical
        self.selector = selector
        super().__init__(
            "No API was offered on the very first attempt, "
            f"so there was nobody to ask. The selector was: {selector}. "
            f"The query was: {canonical!r}."
        )


def build_list_selector(apis: Sequence[SearchAPI]) -> SearchAPISelector:
    """
    Build a selector that yields the given endpoints in order, then stops

    Every query works through the same list.

    Parameters
    ----------
    apis
        The endpoints to yield, in order

    Returns
    -------
    :
        A selector over `apis`
    """

    def select(canonical: QueryCanonical, attempt: int) -> SearchAPI | None:
        return apis[attempt] if attempt < len(apis) else None

    return select


def build_project_list_selector(
    project_lists: Mapping[str, Sequence[SearchAPI]],
) -> SearchAPISelector:
    """
    Build a selector that works through a project specific list of endpoints

    Parameters
    ----------
    project_lists
        The endpoints to yield for each project, in order

    Returns
    -------
    :
        A selector which yields APIs in an order specific to the query's project
    """

    def select(canonical: QueryCanonical, attempt: int) -> SearchAPI | None:
        """
        Select search API to use

        Parameters
        ----------
        canonical
            Query (in canonical form)

        attempt
            Search attempt

        Returns
        -------
        :
            [SearchAPI][(m).] to use

            If we have run out of APIs to try, we return `None`

        Raises
        ------
        ValueError
            The query specifies a search that is not for exactly one project

        KeyError
            We do not have a list for the input project
        """
        if len(canonical.project) != 1:
            msg = (
                "We can only unambiguously pick the SearchAPI list "
                "if there is exactly one project, "
                f"received: {canonical.project}"
            )
            raise ValueError(msg)

        project = canonical.project[0]

        apis = project_lists[project]

        return apis[attempt] if attempt < len(apis) else None

    return select


@dataclass(frozen=True)
class APIClassification:
    """
    Classification of a search API

    Provides extra classification information (i.e. metadata) which
    [SearchAPI][(m).] doesn't hold.
    """

    search_api: SearchAPI
    """
    Search API
    """

    projects: tuple[str, ...]
    """
    Projects which `search_api` supports
    """


@dataclass(frozen=True)
class SearchAPIStore:
    """
    A store of seach APIs

    Search APIs require specific types of language,
    which effectively makes them coupled to specific ESGF projects.

    This store helps manage a group of APIs
    and get them in more convenient ways than looking through lists.
    """

    api_classifications: tuple[APIClassification, ...]
    """
    Search API classifications
    """

    def get_apis_for_project(self, project: str) -> list[SearchAPI]:
        """
        Get the APIs that can be used to search a specific project

        [TODO fix this cross-ref]
        See [APIClassification.projects][(m).]
        for an explanation of why the name of this function is misleading,
        because it is based on the idea
        that specifying the project also defines the search API.

        Parameters
        ----------
        project
            The project for which we want to get
            all the APIs that can be used to search the project.

        Returns
        -------
        :
            APIs that can be used to search `project`.
        """
        return [v.search_api for v in self.api_classifications if project in v.projects]

    def get_apis_from_host(self, host: str) -> list[SearchAPI]:
        """
        Get the API(s) that use a specific host

        Parameters
        ----------
        host
            The host for which we want to get APIs.

        Returns
        -------
        :
            APIs that use `host`
        """
        return [v.search_api for v in self.api_classifications if v.host == host]

    def get_api_for_project_from_host(self, project: str, host: str) -> SearchAPI:
        """
        Get the API that can be used to search a specific project from a specific host

        [TODO fix this cross-ref]
        See [APIClassification.projects][(m).]
        for an explanation of why the name of this function is misleading,
        because it is based on the idea
        that specifying the project also defines the search API.

        Parameters
        ----------
        project
            The project for which we want to get
            all the APIs that can be used to search the project.

        host
            The host for which we want to get API.

        Returns
        -------
        :
            APIs that use `host`
        """
        matches = [
            v
            for v in self.api_classifications
            if (v.search_api.host == host and project in v.projects)
        ]
        if len(matches) < 1:
            host_projects = {}
            for v in self.api_classifications:
                host_projects.setdefault(v.search_api.host, []).extend(v.projects)

            supported_hosts_and_projects = "\n".join(
                f"  - {host}: {projects}" for host, projects in host_projects.items()
            )
            msg = (
                f"No API from {host=} is associated with {project=}. "
                "Available hosts and supported projects:\n"
                f"{supported_hosts_and_projects}"
            )
            raise ValueError(msg)

        elif len(matches) > 1:
            msg = f"More than one candidate for {host=} and {project=}. {matches=}"
            raise AssertionError(msg)

        return matches[0].search_api


INBUILT_SEARCH_API_STORE = SearchAPIStore(
    api_classifications=tuple(
        APIClassification(
            SearchAPI(host, generation, build_transient_retrying(2)), projects
        )
        for host, generation, projects in (
            # CMIP5 hosts
            ("esg-dn1.nsc.liu.se", SOLR_CMIP5, ("CMIP5",)),
            ("esgf.nci.org.au", SOLR_CMIP5, ("CMIP5",)),
            ("esgf-node.ornl.gov", BRIDGE_CMIP5, ("CMIP5",)),
            ("esgf.ceda.ac.uk", SOLR_CMIP5, ("CMIP5",)),
            ("esgf-data.dkrz.de", SOLR_CMIP5, ("CMIP5",)),
            ("search.east.esgf.io", STAC_CMIP5, ("CMIP5",)),
            ("search.west.esgf.io", STAC_CMIP5, ("CMIP5",)),
            # CMIP6-style hosts
            ("esg-dn1.nsc.liu.se", SOLR_CMIP6, ("CMIP6", "CMIP6Plus")),
            ("esgf-node.ornl.gov", BRIDGE_CMIP6, ("CMIP6", "CMIP6Plus")),
            ("esgf.nci.org.au", SOLR_CMIP6, ("CMIP6", "CMIP6Plus")),
            ("esgf.ceda.ac.uk", SOLR_CMIP6, ("CMIP6", "CMIP6Plus")),
            ("esgf-data.dkrz.de", SOLR_CMIP6, ("CMIP6", "CMIP6Plus")),
            ("search.east.esgf.io", STAC_CMIP6, ("CMIP6", "CMIP6Plus")),
            ("search.west.esgf.io", STAC_CMIP6, ("CMIP6", "CMIP6Plus")),
            # CMIP7 hosts
            ("search.east.esgf.io", STAC_CMIP7, ("CMIP7",)),
            ("search.west.esgf.io", STAC_CMIP7, ("CMIP7",)),
            # Kept for now because they have results.
            # However, ESGF nodes have been told not to publish CMIP7 to old nodes,
            # so these should have zero results quite soon
            # and therefore be taken out of our results quite soon.
            ("esgf.nci.org.au", SOLR_CMIP7, ("CMIP7",)),
            ("esgf-data.dkrz.de", SOLR_CMIP7, ("CMIP7",)),
        )
    )
)
"""
Our in-built search API store.

This should not be taken to be exhaustive.
You may need to add more APIs or adjust retry policies etc. yourself.
"""

DEFAULT_SELECTOR = build_project_list_selector(
    {
        project: INBUILT_SEARCH_API_STORE.get_apis_for_project(project)
        for project in ["CMIP5", "CMIP6", "CMIP7"]
    }
)
"""The selector used when the caller does not choose one"""
