"""
Store for multiple search API facades
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

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
    ESGFNG_CMIP6_FACADE_PARAMETERS,
    ESGFNG_CMIP7_FACADE_PARAMETERS,
    FacadeParametersProtocol,
)
from esmporium.search.search_api_facade.result_parsers import (
    ESGFNGCMIP6ResultParser,
    ESGFNGCMIP7ResultParser,
    ResultParserProtocol,
    SolrSingleRowResultParser,
    SolrVariableBundleResultParser,
)

RetryingBuilder = Callable[[], Retrying]
"""
Builds a retry policy

Called once per API, so each API gets a policy of its own.
"""

SearchAPIBuilder = Callable[[str, Retrying], SearchAPI]
"""
Builds a search API for a host, with a retry policy of its own
"""
# This does not use `type[SearchAPI]` because
# [SearchAPI][esmporium.search.apis.SearchAPI] is a protocol,
# and `type[<protocol>]` carries no constructor signature for a type checker to call.


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
class FacadeDefinition:
    """
    Everything needed to build one facade, before it is built

    This is a stepping stone inside
    [SearchAPIFacadeStore.initialise_with_default_api_facades][(m).SearchAPIFacadeStore.initialise_with_default_api_facades]:
    the pieces are picked project by project and endpoint by endpoint, then every
    definition is turned into a facade in one place.
    """

    search_api_type: SearchAPIBuilder
    """The search API class to build, called as `search_api_type(host, retrying)`."""

    host: str
    """The host to build it against."""

    facade_parameters: FacadeParametersProtocol
    """The parameters that name this project's facets for this API."""

    result_parser: ResultParserProtocol
    """The parser that reads this project's results from this endpoint."""

    project: str
    """The project the facade supports."""


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

    project: str
    """
    Project which `facade` supports working with
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
        return [v.facade for v in self.classifications if project == v.project]

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
            if v.facade.search_api.host == host and project == v.project
        ]
        if len(matches) < 1:
            host_projects: dict[str, list[str]] = {}
            for v in self.classifications:
                host_projects.setdefault(v.facade.search_api.host, []).append(v.project)

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
                    f"supported project={v.project!r}"
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
    def initialise_with_default_api_facades(  # noqa: PLR0915
        cls,
        create_retrying: RetryingBuilder = build_default_retrying,
    ) -> SearchAPIFacadeStore:
        """
        Initialise with our default API facade set and ordering

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

        # Urgh, intialisation code is the worst
        def create_facade_definition(  # noqa: PLR0912
            project: str, host: str, style: str
        ) -> FacadeDefinition:
            search_api_type: SearchAPIBuilder
            facade_parameters: FacadeParametersProtocol
            result_parser: ResultParserProtocol

            if project == "CMIP5":
                if style in ("ESGF1", "ESGF15Bridge"):
                    facade_parameters = ESGF1_CMIP5_FACADE_PARAMETERS
                    result_parser = SolrVariableBundleResultParser()

                    if style == "ESGF1":
                        search_api_type = SearchAPIESGF1Solr
                    elif style == "ESGF15Bridge":
                        search_api_type = SearchAPIESGF15BridgeSolr
                    else:
                        raise NotImplementedError(style)

                else:
                    # STAC serves no CMIP5 data, and we do not know the shape of a
                    # CMIP5 STAC document, so there is deliberately no CMIP5 STAC
                    # facade (and no CMIP5 STAC result parser to guess at its shape).
                    raise NotImplementedError(style)

            elif project == "CMIP6":
                if style in ("ESGF1", "ESGF15Bridge"):
                    facade_parameters = ESGF1_CMIP6_FACADE_PARAMETERS
                    result_parser = SolrSingleRowResultParser()

                    if style == "ESGF1":
                        search_api_type = SearchAPIESGF1Solr
                    elif style == "ESGF15Bridge":
                        search_api_type = SearchAPIESGF15BridgeSolr
                    else:
                        raise NotImplementedError(style)

                elif style == "ESGF-NG":
                    search_api_type = SearchAPIESGFNGSTAC
                    facade_parameters = ESGFNG_CMIP6_FACADE_PARAMETERS
                    result_parser = ESGFNGCMIP6ResultParser()

                else:
                    raise NotImplementedError(style)

            elif project == "CMIP7":
                if style == "ESGF1":
                    search_api_type = SearchAPIESGF1Solr
                    facade_parameters = ESGF1_CMIP7_FACADE_PARAMETERS
                    result_parser = SolrSingleRowResultParser()

                elif style == "ESGF15Bridge":
                    search_api_type = SearchAPIESGF15BridgeSolr
                    facade_parameters = ESGF1_CMIP7_FACADE_PARAMETERS
                    result_parser = SolrSingleRowResultParser()

                elif style == "ESGF-NG":
                    search_api_type = SearchAPIESGFNGSTAC
                    facade_parameters = ESGFNG_CMIP7_FACADE_PARAMETERS
                    result_parser = ESGFNGCMIP7ResultParser()

                else:
                    raise NotImplementedError(style)

            else:
                raise NotImplementedError(project)

            return FacadeDefinition(
                search_api_type=search_api_type,
                host=host,
                facade_parameters=facade_parameters,
                result_parser=result_parser,
                project=project,
            )

        facade_definitions = [
            create_facade_definition(project, host, style)
            for project, host, style in (
                *(
                    ("CMIP5", host, style)
                    for host, style in (
                        ("esg-dn1.nsc.liu.se", "ESGF1"),
                        ("esgf.nci.org.au", "ESGF1"),
                        ("esgf-node.ornl.gov", "ESGF15Bridge"),
                        ("esgf.ceda.ac.uk", "ESGF1"),
                        ("esgf-data.dkrz.de", "ESGF1"),
                    )
                ),
                *(
                    ("CMIP6", host, style)
                    for host, style in (
                        ("esg-dn1.nsc.liu.se", "ESGF1"),
                        ("esgf.nci.org.au", "ESGF1"),
                        ("esgf-node.ornl.gov", "ESGF15Bridge"),
                        ("esgf.ceda.ac.uk", "ESGF1"),
                        ("esgf-data.dkrz.de", "ESGF1"),
                        ("search.east.esgf.io", "ESGF-NG"),
                        ("search.west.esgf.io", "ESGF-NG"),
                    )
                ),
                *(
                    ("CMIP7", host, style)
                    for host, style in (
                        ("search.east.esgf.io", "ESGF-NG"),
                        ("search.west.esgf.io", "ESGF-NG"),
                        ("esgf.nci.org.au", "ESGF1"),
                        ("esgf-data.dkrz.de", "ESGF1"),
                    )
                ),
            )
        ]

        classifications_l = []
        for facade_definition in facade_definitions:
            # A fresh retry policy per API:
            # tenacity's Retrying carries per-run state.
            search_api = facade_definition.search_api_type(
                facade_definition.host, create_retrying()
            )
            classifications_l.append(
                SearchAPIFacadeClassification(
                    SearchAPIFacade(
                        parameters=facade_definition.facade_parameters,
                        search_api=search_api,
                        result_parser=facade_definition.result_parser,
                    ),
                    project=facade_definition.project,
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
