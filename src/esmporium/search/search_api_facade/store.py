"""
Store for multiple search API facades
"""

from __future__ import annotations

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
            msg = f"More than one candidate for {host=} and {project=}. {matches=}"
            raise AssertionError(msg)

        return matches[0].facade

    @classmethod
    def initialise_with_default_api_facades(
        cls, retrying: Retrying | None = None
    ) -> SearchAPIFacadeStore:
        """
        Initialise with our default API facade set

        Parameters
        ----------
        retrying
            Retrying strategy to use with all the APIs.

            If `None` (the default), a fresh
            [build_transient_retrying][esmporium.search.retry.build_transient_retrying]
            is built for each API. This matters because a `Retrying` carries
            per-run state, so sharing one across APIs is not safe once calls can
            be made in parallel; pass your own only if you know you want it shared.

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
        # (its own STAC vocabulary with a `cmip6plus` prefix would be needed,
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
                # A fresh retry policy per API unless the caller shared one:
                # tenacity's Retrying carries per-run state.
                api_retrying = (
                    retrying if retrying is not None else build_transient_retrying(3)
                )
                search_api = cast(
                    "SearchAPI", search_api_type(host=host, retrying=api_retrying)
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
