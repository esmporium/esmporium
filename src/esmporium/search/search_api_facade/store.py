"""
Store for multiple search API facades
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from tenacity import Retrying

from esmporium.search.apis import (
    SearchAPI,
    SearchAPIESGF1Solr,
    SearchAPIESGF15BridgeSolr,
    SearchAPIESGFNGSTAC,
)
from esmporium.search.retry import build_transient_retrying
from esmporium.search.search_api_facade.core import SearchAPIFacade
from esmporium.search.search_api_facade.parameters import (
    ESGF1_CMIP5_FACADE_PARAMETERS,
    ESGF1_CMIP6_FACADE_PARAMETERS,
    ESGF1_CMIP7_FACADE_PARAMETERS,
    ESGFNG_CMIP5_FACADE_PARAMETERS,
    ESGFNG_CMIP6_FACADE_PARAMETERS,
    ESGFNG_CMIP7_FACADE_PARAMETERS,
)

RetryingBuilder = Callable[[], Retrying]
"""
Builds a retry policy

Called once per API, so each API gets a policy of its own.
"""


def build_default_retrying(attempts: int = 3) -> Retrying:
    """
    Build the retry policy our default API facades use

    Parameters
    ----------
    attempts
        Maximum number of attempts to allow before giving up

    Returns
    -------
    :
        A [build_transient_retrying][esmporium.search.retry.build_transient_retrying]
        policy allowing `attempts` attempts.
    """
    return build_transient_retrying(attempts)


@dataclass(frozen=True)
class SearchAPIFacadeClassification:
    """
    Classification of a search API facade

    Provides extra classification information (i.e. metadata) which
    [SearchAPIFacade][(m).] doesn't hold.

    Note that these classifications are generally based on experience.
    If we were 100% sure about this metadata,
    we would adjust the underlying classes directly instead.
    """

    facade: SearchAPIFacade
    """
    Search API facade
    """

    projects: tuple[str, ...]
    """
    Projects which `facade` supports working with
    """


@dataclass(frozen=True)
class SearchAPIFacadeStore:
    """
    A store of search API facades

    This store helps manage a set of API facades
    and get them in more convenient ways than looking through lists.
    """

    classifications: tuple[SearchAPIFacadeClassification, ...]
    """
    Search API facade classifications
    """

    def get_api_facades_for_project(self, project: str) -> list[SearchAPIFacade]:
        """
        Get the API facades that can be used to search a specific project

        Parameters
        ----------
        project
            The project for which we want to get
            all the API facades that can be used to search the project.

        Returns
        -------
        :
            API facades that can be used to search `project`.
        """
        return [v.facade for v in self.classifications if project in v.projects]

    def get_api_facades_from_host(self, host: str) -> list[SearchAPIFacade]:
        """
        Get the API facades that use a specific host

        Parameters
        ----------
        host
            The host for which we want to get API facades.

        Returns
        -------
        :
            API facades that use `host`
        """
        return [
            v.facade for v in self.classifications if v.facade.search_api.host == host
        ]

    def get_api_facade_for_project_from_host(
        self, project: str, host: str
    ) -> SearchAPIFacade:
        """
        Get the API facade that can be used to search a project from a specific host

        Parameters
        ----------
        project
            The project for which we want to get the API facade.

        host
            The host for which we want to get API facade.

        Returns
        -------
        :
            API facade for `project` that uses `host`

        Raises
        ------
        ValueError
            We have no API facade which pairs `host` with `project`.

            The message lists every host we do have,
            with the projects each of them supports.

        AssertionError
            We have more than one API facade
            which pairs `host` with `project`, so the answer is ambiguous.

            This is a bug in whoever built the store rather than a caller error:
            a host should be classified against a given project only once.
        """
        matches = [
            v
            for v in self.classifications
            if v.facade.search_api.host == host and project in v.projects
        ]
        if len(matches) < 1:
            host_projects: dict[str, list[str]] = {}
            for v in self.classifications:
                host_projects.setdefault(v.facade.search_api.host, []).extend(
                    v.projects
                )

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
            matches_summary = [
                (
                    f"facade host={v.facade.search_api.host!r}, "
                    f"facade API type={type(v.facade.search_api).__name__!r}, "
                    f"supported projects={v.projects!r}"
                )
                for v in matches
            ]
            msg = (
                f"More than one candidate for {host=} and {project=}. "
                f"{matches_summary=}. {matches=}"
            )

            raise AssertionError(msg)

        return matches[0].facade

    @classmethod
    def initialise_with_default_api_facades(
        cls, create_retrying: RetryingBuilder = build_default_retrying
    ) -> SearchAPIFacadeStore:
        """
        Initialise with our default API facade set

        Parameters
        ----------
        create_retrying
            Builds the retrying strategy to use with an API.

            We call this once per API, so each API gets a policy of its own.

        Returns
        -------
        :
            Initialised object
        """
        classifications_l = []

        # There are probably clearer ways to do this.
        # One for another day.
        cmip5_facades = (
            (
                ESGF1_CMIP5_FACADE_PARAMETERS,
                SearchAPIESGF1Solr,
                "esg-dn1.nsc.liu.se",
            ),
            (
                ESGF1_CMIP5_FACADE_PARAMETERS,
                SearchAPIESGF1Solr,
                "esgf.nci.org.au",
            ),
            (
                ESGF1_CMIP5_FACADE_PARAMETERS,
                # Currently assuming that ESGF1.5 bridge
                # can use ESGF1 parameters.
                SearchAPIESGF15BridgeSolr,
                "esgf-node.ornl.gov",
            ),
            (
                ESGF1_CMIP5_FACADE_PARAMETERS,
                SearchAPIESGF1Solr,
                "esgf.ceda.ac.uk",
            ),
            (
                ESGF1_CMIP5_FACADE_PARAMETERS,
                SearchAPIESGF1Solr,
                "esgf-data.dkrz.de",
            ),
            (
                ESGFNG_CMIP5_FACADE_PARAMETERS,
                SearchAPIESGFNGSTAC,
                "search.east.esgf.io",
            ),
            (
                ESGFNG_CMIP5_FACADE_PARAMETERS,
                SearchAPIESGFNGSTAC,
                "search.west.esgf.io",
            ),
        )

        cmip6_facades = (
            (
                ESGF1_CMIP6_FACADE_PARAMETERS,
                SearchAPIESGF1Solr,
                "esg-dn1.nsc.liu.se",
            ),
            (
                ESGF1_CMIP6_FACADE_PARAMETERS,
                SearchAPIESGF1Solr,
                "esgf.nci.org.au",
            ),
            (
                ESGF1_CMIP6_FACADE_PARAMETERS,
                # Currently assuming that ESGF1.5 bridge
                # can use ESGF1 parameters.
                SearchAPIESGF15BridgeSolr,
                "esgf-node.ornl.gov",
            ),
            (
                ESGF1_CMIP6_FACADE_PARAMETERS,
                SearchAPIESGF1Solr,
                "esgf.ceda.ac.uk",
            ),
            (
                ESGF1_CMIP6_FACADE_PARAMETERS,
                SearchAPIESGF1Solr,
                "esgf-data.dkrz.de",
            ),
            (
                ESGFNG_CMIP6_FACADE_PARAMETERS,
                SearchAPIESGFNGSTAC,
                "search.east.esgf.io",
            ),
            (
                ESGFNG_CMIP6_FACADE_PARAMETERS,
                SearchAPIESGFNGSTAC,
                "search.west.esgf.io",
            ),
        )

        cmip7_facades = (
            (
                ESGF1_CMIP7_FACADE_PARAMETERS,
                SearchAPIESGF1Solr,
                "esgf.nci.org.au",
            ),
            (
                ESGF1_CMIP7_FACADE_PARAMETERS,
                SearchAPIESGF1Solr,
                "esgf-data.dkrz.de",
            ),
            (
                ESGFNG_CMIP7_FACADE_PARAMETERS,
                SearchAPIESGFNGSTAC,
                "search.east.esgf.io",
            ),
            (
                ESGFNG_CMIP7_FACADE_PARAMETERS,
                SearchAPIESGFNGSTAC,
                "search.west.esgf.io",
            ),
        )

        # To add CMIP6Plus support in future:
        # add a `cmip6plus_facades` block here
        # (its own STAC query style with a `cmip6plus` prefix would be needed,
        # as ESGFNG_CMIP6_FACADE_PARAMETERS is tied to the `cmip6` collection),
        # classify it against `("CMIP6Plus",)`
        # in the loop below,
        # and add "CMIP6Plus" to DEFAULT_SEARCH_API_FACADES_BY_PROJECT.
        for projects, facade_definitions in (
            (("CMIP5",), cmip5_facades),
            (("CMIP6",), cmip6_facades),
            (("CMIP7",), cmip7_facades),
        ):
            for facade_parameters, search_api_type, host in facade_definitions:
                # A fresh retry policy per API:
                # tenacity's Retrying carries per-run state.
                search_api = cast(
                    "SearchAPI", search_api_type(host=host, retrying=create_retrying())
                )
                classifications_l.append(
                    SearchAPIFacadeClassification(
                        SearchAPIFacade(
                            parameters=facade_parameters,
                            search_api=search_api,
                        ),
                        projects=projects,
                    )
                )

        res = cls(classifications=tuple(classifications_l))

        return res


INBUILT_SEARCH_API_FACADE_STORE = (
    SearchAPIFacadeStore.initialise_with_default_api_facades()
)
"""
Our in-built search API facade store.

This should not be taken to be exhaustive.
You may need to add more APIs or adjust retry policies etc. yourself.
"""
