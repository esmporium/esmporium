"""
The result parsers we know how to build
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from esmporium.search.apis.esgf1 import extract_one_element_list
from esmporium.search.apis.esgfng import stac_nodes
from esmporium.search.apis.protocol import (
    NoSearchResultNumberOfMatchesReturnedError,
    describe_search_api,
    read_response_path,
)
from esmporium.search.result_parsing import (
    DataNodeInfo,
    DatasetFacets,
    ParsedDocument,
)
from esmporium.search.search_api_facade.result_parsers.protocol import (
    NMatchesReader,
    get_single_value_columns_from_doc,
)

if TYPE_CHECKING:
    from esmporium.search.apis import SearchAPI
    from esmporium.search.search_api_facade.parameters import (
        FacadeParametersProtocol,
    )


class MissingResultFieldError(ValueError):
    """
    Raised when a result document does not carry a field we have to have
    """

    def __init__(self, api_field: str, doc_id: str) -> None:
        """
        Initialise the error

        Parameters
        ----------
        api_field
            The field name we read, in the API's own vocabulary

        doc_id
            An id for the document we read it from, so the offender can be looked up
        """
        self.api_field = api_field
        self.doc_id = doc_id
        super().__init__(
            f"The document with id {doc_id!r} carries no value for {api_field!r}, "
            "which we need in order to save it."
        )


def _read_required_facet(
    doc: dict[str, Any], api: SearchAPI, api_field: str, doc_id: str
) -> str:
    """
    Read a facet we cannot do without, failing loudly if it is not there

    Parameters
    ----------
    doc
        The document to read

    api
        The search API the document came from, used to read its fields

    api_field
        The field name to read, in the API's vocabulary

    doc_id
        An id for the document, used only for the error message

    Returns
    -------
    :
        The value

    Raises
    ------
    MissingResultFieldError
        `doc` carries no value for `api_field`
    """
    value = api.read_facet(doc, api_field)
    if value is None:
        raise MissingResultFieldError(api_field, doc_id)

    return value


def _single_row(
    doc: dict[str, Any],
    api: SearchAPI,
    facade_parameters: FacadeParametersProtocol,
    base: dict[str, str],
) -> DatasetFacets:
    """
    Read the one dataset row a document maps to

    Parameters
    ----------
    doc
        The document to read

    api
        The search API the document came from, used to read its fields

    facade_parameters
        The facade parameters that name which API field carries each column

    base
        The columns the parser has already worked out for itself,
        which are therefore not read again here

    Returns
    -------
    :
        The dataset row
    """
    columns = get_single_value_columns_from_doc(
        doc, api, facade_parameters, exclude=tuple(base)
    )

    return DatasetFacets.model_validate({**base, **columns})


def solr_n_matches(raw: dict[str, Any]) -> int:
    """
    Get the number of records that matched a search from a Solr-shaped response

    Note: this is not the same as the number of results in `raw`.
    Solr has the idea of 'limit', which means that the number of results returned
    can differ from the total number of records which matched a given query.

    Parameters
    ----------
    raw
        The raw search result to read

    Returns
    -------
    :
        The number of records that matched the search

    Raises
    ------
    NoSearchResultNumberOfMatchesReturnedError
        `raw` does not report the number of records that matched the search
    """
    num_found = raw.get("response", {}).get("numFound")
    if isinstance(num_found, int):
        return num_found

    elif num_found is not None:
        msg = (
            "We expected to get an integer at 'response.numFound', "
            f"but instead got {num_found!r}"
        )
        raise TypeError(msg)

    raise NoSearchResultNumberOfMatchesReturnedError(raw, "response.numFound")


def _n_matches_from(
    raw: dict[str, Any], candidates: tuple[tuple[str, Any], ...]
) -> int:
    """
    Read a match count out of the places one deployment might write it

    Parameters
    ----------
    raw
        The raw search result the values were read from

        Only used for error messages.

    candidates
        Where we looked and what we found there, in the order we prefer them

    Returns
    -------
    :
        The number of records that matched the search

    Raises
    ------
    NoSearchResultNumberOfMatchesReturnedError
        None of `candidates` carries a count

    TypeError
        A candidate carries something which is not a count
    """
    for loc, total in candidates:
        if isinstance(total, int):
            return total

        elif total is not None:
            msg = f"We expected to get an integer at {loc}, but instead got {total!r}"
            raise TypeError(msg)

    raise NoSearchResultNumberOfMatchesReturnedError(
        raw, tuple(loc for loc, _ in candidates)
    )


def stac_east_n_matches(raw: dict[str, Any]) -> int:
    """
    Get the number of records that matched a search from an ESGF-NG east response

    East writes the count as `numberMatched`, which is what STAC calls it.

    Parameters
    ----------
    raw
        The raw search result to read

    Returns
    -------
    :
        The number of records that matched the search

    Raises
    ------
    NoSearchResultNumberOfMatchesReturnedError
        `raw` does not report the number of records that matched the search
    """
    return _n_matches_from(raw, (("numberMatched", raw.get("numberMatched")),))


# TODO: run live integration tests to test for failure in west's `numMatched`
# ESGF-NG west is replacing numMatched with numberMatched, matching east.
def stac_west_n_matches(raw: dict[str, Any]) -> int:
    """
    Get the number of records that matched a search from an ESGF-NG west response

    West does not write `numberMatched` at all. It writes the count twice, as
    `numMatched` and as `context.matched`, neither of which is the STAC spelling.

    Parameters
    ----------
    raw
        The raw search result to read

    Returns
    -------
    :
        The number of records that matched the search

    Raises
    ------
    NoSearchResultNumberOfMatchesReturnedError
        `raw` does not report the number of records that matched the search
    """
    context = raw.get("context")

    return _n_matches_from(
        raw,
        (
            ("numMatched", raw.get("numMatched")),
            (
                "context.matched",
                context.get("matched") if isinstance(context, dict) else None,
            ),
        ),
    )


def solr_id_project_specific(doc: dict[str, Any], api: SearchAPI) -> str:
    """
    Read the bundle id of a Solr record

    Parameters
    ----------
    doc
        The record to read

    api
        The search API the record came from

        Only used to say who answered if the id is not there.

    Returns
    -------
    :
        The bundle's native id

    Raises
    ------
    UnreadableResponseError
        `doc` does not carry a bundle id
    """
    res: str = extract_one_element_list(
        read_response_path(
            doc, "master_id", what="the bundle id", context=describe_search_api(api)
        )
    )

    return res


def solr_parsed_document(
    doc: dict[str, Any], api: SearchAPI, datasets: tuple[DatasetFacets, ...]
) -> ParsedDocument:
    """
    Build the parsed document for a Solr record

    Parameters
    ----------
    doc
        The record to read

    api
        The search API the record came from, which names the format of `doc`

    datasets
        The dataset rows this record maps to, read by the calling parser

    Returns
    -------
    :
        The pieces of `doc` that we store

    Raises
    ------
    UnreadableResponseError
        `doc` does not carry one of the fields every record has to have
    """

    def read(path: str, what: str) -> Any:
        """Read one of the fields we cannot build a `ParsedDocument` without"""
        return extract_one_element_list(
            read_response_path(doc, path, what=what, context=describe_search_api(api))
        )

    return ParsedDocument(
        id_project_specific=solr_id_project_specific(doc, api),
        datasets=datasets,
        version=str(read("version", "the version of this record")),
        is_latest=bool(read("latest", "whether this is the latest version")),
        retracted=bool(read("retracted", "whether this record is retracted")),
        nodes=(
            DataNodeInfo(
                data_node=read("data_node", "the data node hosting this record")
            ),
        ),
        esgf_doc_id=read("id", "this record's id"),
        raw_json=json.dumps(doc),
        raw_docs_format_tag=api.raw_docs_format_tag,
    )


def solr_base_columns(
    doc: dict[str, Any], api: SearchAPI, facade_parameters: FacadeParametersProtocol
) -> dict[str, str]:
    """
    Read the dataset columns a Solr parser works out for itself

    These are the bundle id and the project:
    the rest of the columns are read generically from the facet name mapping.

    Parameters
    ----------
    doc
        The record to read

    api
        The search API the record came from, used to read its fields

    facade_parameters
        The facade parameters that name which API field carries the project

    Returns
    -------
    :
        The bundle id and the project

    Raises
    ------
    MissingResultFieldError
        `doc` does not say which project it belongs to
    """
    id_project_specific = solr_id_project_specific(doc, api)
    # Solr carries the project as a facet like any other,
    # so its name comes from the same mapping as every other column's.
    project_field = facade_parameters.get_mapping_to_api_facet_names({"project"})[
        "project"
    ]

    return {
        "id_project_specific": id_project_specific,
        "project": _read_required_facet(
            doc,
            api,
            project_field,
            # The bundle id names the offender if the project cannot be read:
            # it is what we have read already,
            # and it identifies the record well.
            doc_id=id_project_specific,
        ),
    }


@dataclass(frozen=True)
class SolrSingleRowResultParser:
    """
    Read CMIP6/CMIP7 results from a Solr-shaped API

    One record is one dataset row.
    Every facet is read as a scalar.
    A record carrying more than one value for any facet is a loud
    [MultipleFacetValuesError][esmporium.search.apis.MultipleFacetValuesError].
    """

    def get_n_matches(self, raw: dict[str, Any]) -> int:
        """
        See [ResultParserProtocol.get_n_matches][esmporium.search.search_api_facade.result_parsers.ResultParserProtocol.get_n_matches].
        """  # noqa: E501
        return solr_n_matches(raw)

    def parse_search_results(
        self,
        raw: dict[str, Any],
        *,
        api: SearchAPI,
        facade_parameters: FacadeParametersProtocol,
    ) -> tuple[ParsedDocument, ...]:
        """
        See [ResultParserProtocol.parse_search_results][esmporium.search.search_api_facade.result_parsers.ResultParserProtocol.parse_search_results].
        """  # noqa: E501
        return tuple(
            solr_parsed_document(
                doc,
                api,
                self.get_dataset_rows(
                    doc, api=api, facade_parameters=facade_parameters
                ),
            )
            for doc in api.extract_result_documents(raw)
        )

    def get_dataset_rows(
        self,
        doc: dict[str, Any],
        *,
        api: SearchAPI,
        facade_parameters: FacadeParametersProtocol,
    ) -> tuple[DatasetFacets, ...]:
        """
        See [ResultParserProtocol.get_dataset_rows][esmporium.search.search_api_facade.result_parsers.ResultParserProtocol.get_dataset_rows].
        """  # noqa: E501
        row = _single_row(
            doc,
            api,
            facade_parameters,
            base=solr_base_columns(doc, api, facade_parameters),
        )

        return (row,)


@dataclass(frozen=True)
class SolrVariableBundleResultParser:
    """
    Read results that bundle multiple variables together from a Solr-shaped API

    This supports CMIP5 for the most part.
    A CMIP5 record bundles many variables into one document. Every facet except
    `variable` is a single value shared by the rows;
    `variable` is read as a list and the record explodes into one row per value.
    """

    def get_n_matches(self, raw: dict[str, Any]) -> int:
        """
        See [ResultParserProtocol.get_n_matches][esmporium.search.search_api_facade.result_parsers.ResultParserProtocol.get_n_matches].
        """  # noqa: E501
        return solr_n_matches(raw)

    def parse_search_results(
        self,
        raw: dict[str, Any],
        *,
        api: SearchAPI,
        facade_parameters: FacadeParametersProtocol,
    ) -> tuple[ParsedDocument, ...]:
        """
        See [ResultParserProtocol.parse_search_results][esmporium.search.search_api_facade.result_parsers.ResultParserProtocol.parse_search_results].
        """  # noqa: E501
        return tuple(
            solr_parsed_document(
                doc,
                api,
                self.get_dataset_rows(
                    doc, api=api, facade_parameters=facade_parameters
                ),
            )
            for doc in api.extract_result_documents(raw)
        )

    def get_dataset_rows(
        self,
        doc: dict[str, Any],
        *,
        api: SearchAPI,
        facade_parameters: FacadeParametersProtocol,
    ) -> tuple[DatasetFacets, ...]:
        """
        See [ResultParserProtocol.get_dataset_rows][esmporium.search.search_api_facade.result_parsers.ResultParserProtocol.get_dataset_rows].
        """  # noqa: E501
        base = solr_base_columns(doc, api, facade_parameters)
        # `variable` is the one facet read as a list:
        # a bundling record carries many variables,
        # so it is excluded from the shared scalar columns
        # and read on its own to explode the record into one row per variable.
        shared = get_single_value_columns_from_doc(
            doc, api, facade_parameters, exclude={*base, "variable"}
        )
        variable_field = facade_parameters.get_mapping_to_api_facet_names({"variable"})[
            "variable"
        ]

        # A variable-bundle project (CMIP5) has no grid concept, so nothing maps
        # `grid_label` and `shared` never carries it. `DatasetFacets.grid_label` is
        # required with no default, so we state the absence explicitly as `None` (a real
        # mapping, if one ever existed, would land in `shared` and override this).
        return tuple(
            DatasetFacets.model_validate(
                {"grid_label": None, **base, **shared, "variable": variable}
            )
            for variable in api.read_facet_list(doc, variable_field)
        )


def stac_parsed_document(
    feature: dict[str, Any],
    api: SearchAPI,
    id_project_specific: str,
    datasets: tuple[DatasetFacets, ...],
) -> ParsedDocument:
    """
    Build the parsed document for a STAC feature

    Parameters
    ----------
    feature
        The feature to read

    api
        The search API the feature came from, which names the format of `feature`

    id_project_specific
        The bundle's native id, read by the calling parser

    datasets
        The dataset rows this feature maps to, read by the calling parser

    Returns
    -------
    :
        Parsed document

    Raises
    ------
    UnreadableResponseError
        `feature` does not carry one of the fields every feature has to have
    """

    def read(path: str, what: str) -> Any:
        """Read one of the fields we cannot build a `ParsedDocument` without"""
        return read_response_path(
            feature, path, what=what, context=describe_search_api(api)
        )

    return ParsedDocument(
        id_project_specific=id_project_specific,
        datasets=datasets,
        version=read("properties.version", "the version of this record"),
        is_latest=read("properties.latest", "whether this is the latest version"),
        retracted=read("properties.retracted", "whether this record is retracted"),
        nodes=stac_nodes(feature, api=api),
        esgf_doc_id=read("id", "this record's id"),
        raw_json=json.dumps(feature),
        raw_docs_format_tag=api.raw_docs_format_tag,
    )


@dataclass(frozen=True)
class ESGFNGResultParser:
    """
    Read results from the ESGF-NG STAC API
    """

    read_n_matches: NMatchesReader
    """
    Reads how many records matched a search out of one of this endpoint's responses

    Deliberately has no default: east and west should answer the same way and do not
    (see [stac_east_n_matches][(m).] and [stac_west_n_matches][(m).]), so whoever builds
    a parser has to say which deployment it is for rather than getting a reader that
    quietly tries every spelling. If the two ever agree, this can go and the count can
    move back onto the search API, where a format-level concern belongs.
    """

    def get_n_matches(self, raw: dict[str, Any]) -> int:
        """
        See [ResultParserProtocol.get_n_matches][esmporium.search.search_api_facade.result_parsers.ResultParserProtocol.get_n_matches].
        """  # noqa: E501
        return self.read_n_matches(raw)

    def parse_search_results(
        self,
        raw: dict[str, Any],
        *,
        api: SearchAPI,
        facade_parameters: FacadeParametersProtocol,
    ) -> tuple[ParsedDocument, ...]:
        """
        See [ResultParserProtocol.parse_search_results][esmporium.search.search_api_facade.result_parsers.ResultParserProtocol.parse_search_results].
        """  # noqa: E501
        return tuple(
            stac_parsed_document(
                feature,
                api,
                id_project_specific=self.get_id_project_specific(feature, api),
                datasets=self.get_dataset_rows(
                    feature, api=api, facade_parameters=facade_parameters
                ),
            )
            for feature in api.extract_result_documents(raw)
        )

    def get_dataset_rows(
        self,
        doc: dict[str, Any],
        *,
        api: SearchAPI,
        facade_parameters: FacadeParametersProtocol,
    ) -> tuple[DatasetFacets, ...]:
        """
        See [ResultParserProtocol.get_dataset_rows][esmporium.search.search_api_facade.result_parsers.ResultParserProtocol.get_dataset_rows].

        The project is read from the feature's `collection` key,
        following the advice given in
        [esgf-roadmap#203](https://github.com/ESGF/esgf-roadmap/issues/203#issuecomment-5631161411).
        """  # noqa: E501
        base = {
            "id_project_specific": self.get_id_project_specific(doc, api),
            "project": read_response_path(
                doc,
                "collection",
                what="the project this record belongs to",
                context=describe_search_api(api),
            ),
        }
        row = _single_row(doc, api, facade_parameters, base=base)

        return (row,)

    def get_id_project_specific(self, feature: dict[str, Any], api: SearchAPI) -> str:
        """
        Read the project specific id of a STAC feature

        Parameters
        ----------
        feature
            The feature to read

        api
            The search API the feature came from

            Only used to say who answered if the id is not there.

        Returns
        -------
        :
            The bundle's native id, i.e. the feature id without its version token

        Raises
        ------
        UnreadableResponseError
            `feature` does not carry a project specific id
        """
        res: str = read_response_path(
            feature,
            "properties.title",
            what="the project specific id",
            context=describe_search_api(api),
        )

        return res
