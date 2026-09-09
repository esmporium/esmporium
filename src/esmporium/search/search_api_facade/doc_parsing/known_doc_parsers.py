"""
The doc parsers we know how to build, and a store that picks the right one

There are only two document shapes we parse, so there are only two parsers:

- [SingleRowDocParser][(m).] -- one document is one dataset row. This is CMIP6 and
  CMIP7, on both Solr and STAC. Every facet, `variable` included, is read as a scalar,
  so a document that somehow carried several variables would fail loudly rather than
  quietly becoming several rows.
- [VariableBundleDocParser][(m).] -- one document bundles many variables and explodes
  into one row per variable. This is CMIP5 on Solr.

Which parser a facade uses is a function of *both* the project and the response format,
so [DocParserStore][(m).] keys them by `(project, search_api_tag)`, mirroring
[SearchAPIFacadeStore][esmporium.search.search_api_facade.SearchAPIFacadeStore].
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from esmporium.search.result_normalisation import SOLR_FORMAT_TAG, STAC_FORMAT_TAG
from esmporium.search.search_api_facade.doc_parsing.protocol import (
    DocParserProtocol,
    get_single_value_columns_from_doc,
)

if TYPE_CHECKING:
    from collections.abc import Collection

    from esmporium.search.apis import SearchAPI
    from esmporium.search.search_api_facade.parameters import (
        FacadeParametersProtocol,
    )


@dataclass(frozen=True)
class SingleRowDocParser:
    """
    Parse a document that maps to exactly one dataset row (CMIP6/CMIP7, any format)

    Every facet is read as a scalar, `variable` included: there is no bundling here, so
    a document carrying more than one value for any facet is a loud
    [MultipleFacetValuesError][esmporium.search.apis.MultipleFacetValuesError] rather
    than a silent extra row.
    """

    def get_dataset_rows_from_doc(
        self,
        doc: dict[str, Any],
        *,
        api: SearchAPI,
        facade_parameters: FacadeParametersProtocol,
        exclude_columns: Collection[str],
    ) -> tuple[dict[str, str | None], ...]:
        """
        See [DocParserProtocol.get_dataset_rows_from_doc][esmporium.search.search_api_facade.doc_parsing.DocParserProtocol.get_dataset_rows_from_doc].
        """  # noqa: E501
        return (
            get_single_value_columns_from_doc(
                doc, api, facade_parameters, exclude=exclude_columns
            ),
        )


@dataclass(frozen=True)
class VariableBundleDocParser:
    """
    Parse a document that bundles many variables into one row per variable (CMIP5 Solr)

    Every facet except `variable` is a single value shared by the rows; `variable` is
    read as a list and the document explodes into one row per value.
    """

    def get_dataset_rows_from_doc(
        self,
        doc: dict[str, Any],
        *,
        api: SearchAPI,
        facade_parameters: FacadeParametersProtocol,
        exclude_columns: Collection[str],
    ) -> tuple[dict[str, str | None], ...]:
        """
        See [DocParserProtocol.get_dataset_rows_from_doc][esmporium.search.search_api_facade.doc_parsing.DocParserProtocol.get_dataset_rows_from_doc].
        """  # noqa: E501
        # `variable` is the one facet read as a list: a CMIP5 Solr document bundles many
        # variables, so it is excluded from the shared scalar columns and read on its
        # own to explode the document into one row per variable.
        shared = get_single_value_columns_from_doc(
            doc, api, facade_parameters, exclude={*exclude_columns, "variable"}
        )
        variable_field = facade_parameters.get_mapping_to_api_facet_names({"variable"})[
            "variable"
        ]
        return tuple(
            {**shared, "variable": variable}
            for variable in api.read_facet_list(doc, variable_field)
        )


SINGLE_ROW_DOC_PARSER = SingleRowDocParser()
"""The one-row parser shared by CMIP6 and CMIP7 on both Solr and STAC."""

VARIABLE_BUNDLE_DOC_PARSER = VariableBundleDocParser()
"""The variable-exploding parser used for CMIP5 on Solr."""


class NoDocParserError(ValueError):
    """
    Raised when a store has no doc parser for a project and response format
    """

    def __init__(
        self,
        project: str,
        search_api_tag: str,
        classifications: tuple[DocParserClassification, ...],
    ) -> None:
        """
        Initialise the error

        Parameters
        ----------
        project
            The project we wanted a parser for

        search_api_tag
            The response format we wanted a parser for

        classifications
            The classifications we do have, listed so the message says what is available
        """
        self.project = project
        self.search_api_tag = search_api_tag
        self.classifications = classifications
        available = "\n".join(
            f"  - {c.search_api_tag}: {list(c.projects)}" for c in classifications
        )
        super().__init__(
            f"No doc parser is registered for {project=} with {search_api_tag=}. "
            "Available formats and the projects each parses:\n"
            f"{available}"
        )


@dataclass(frozen=True)
class DocParserClassification:
    """
    Classification of a doc parser: which projects and format it parses
    """

    doc_parser: DocParserProtocol
    """The doc parser being classified."""

    projects: tuple[str, ...]
    """Projects `doc_parser` parses documents for."""

    search_api_tag: str
    """
    The response format `doc_parser` parses, named as
    [SearchAPI.search_api_tag][esmporium.search.apis.SearchAPI.search_api_tag].
    """


@dataclass(frozen=True)
class DocParserStore:
    """
    A store of doc parsers, looked up by project and response format

    Mirrors [SearchAPIFacadeStore][esmporium.search.search_api_facade.SearchAPIFacadeStore]:
    a hand-built set of classifications plus a lookup that turns a `(project, format)`
    pair into the one parser that handles it.
    """  # noqa: E501

    classifications: tuple[DocParserClassification, ...]
    """The doc parser classifications this store holds."""

    def get_doc_parser(self, project: str, search_api_tag: str) -> DocParserProtocol:
        """
        Get the doc parser for a project and response format

        Parameters
        ----------
        project
            The project whose documents we want to parse

        search_api_tag
            The response format we want to parse, named as
            [SearchAPI.search_api_tag][esmporium.search.apis.SearchAPI.search_api_tag]

        Returns
        -------
        :
            The one doc parser that parses `project` documents in the `search_api_tag`
            format

        Raises
        ------
        NoDocParserError
            No classification pairs `project` with `search_api_tag`

        AssertionError
            More than one classification pairs `project` with `search_api_tag`, so the
            answer is ambiguous. This is a bug in whoever built the store.
        """
        matches = [
            c
            for c in self.classifications
            if project in c.projects and c.search_api_tag == search_api_tag
        ]
        if len(matches) < 1:
            raise NoDocParserError(project, search_api_tag, self.classifications)

        if len(matches) > 1:
            msg = (
                f"More than one doc parser for {project=} with {search_api_tag=}: "
                f"{matches=}"
            )
            raise AssertionError(msg)

        return matches[0].doc_parser

    @classmethod
    def initialise_with_default_doc_parsers(cls) -> DocParserStore:
        """
        Initialise with our default doc parser set

        Returns
        -------
        :
            Initialised object

            CMIP5 is only parsed on Solr; STAC serves no CMIP5 data, so there is
            deliberately no CMIP5 STAC parser to guess its (unknown) shape.
        """
        return cls(
            classifications=(
                DocParserClassification(
                    VARIABLE_BUNDLE_DOC_PARSER, ("CMIP5",), SOLR_FORMAT_TAG
                ),
                DocParserClassification(
                    SINGLE_ROW_DOC_PARSER, ("CMIP6", "CMIP7"), SOLR_FORMAT_TAG
                ),
                DocParserClassification(
                    SINGLE_ROW_DOC_PARSER, ("CMIP6", "CMIP7"), STAC_FORMAT_TAG
                ),
            )
        )


INBUILT_DOC_PARSER_STORE = DocParserStore.initialise_with_default_doc_parsers()
"""
Our in-built doc parser store.

Used by [SearchAPIFacadeStore][esmporium.search.search_api_facade.SearchAPIFacadeStore]
to give each facade the parser for its project and response format.
"""
