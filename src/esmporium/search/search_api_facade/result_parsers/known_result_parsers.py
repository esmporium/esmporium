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
)
from esmporium.search.result_parsing import (
    DataNodeInfo,
    DatasetFacets,
    ParsedDocument,
)
from esmporium.search.search_api_facade.result_parsers.protocol import (
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


def stac_n_matches(raw: dict[str, Any]) -> int:
    """
    Get the number of records that matched a search from a STAC-shaped response

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
    # The two ESGF-NG deployments disagree on where the total lives:
    # east reports `numberMatched` (the STAC spelling),
    # west reports `numMatched` and `context.matched`.
    # We try everything, starting with the correct (STAC) spelling.
    #
    # This is shared by both ESGF-NG parsers rather than written out in each,
    # because the disagreement is between east and west
    # while the parsers are split by project:
    # each parser answers for both deployments,
    # so each would have to try every spelling anyway.
    # Writing it out per parser would duplicate it
    # without saying anything about which deployment does what.
    context = raw.get("context")
    candidates = (
        ("numberMatched", raw.get("numberMatched")),
        ("numMatched", raw.get("numMatched")),
        (
            "context.matched",
            context.get("matched") if isinstance(context, dict) else None,
        ),
    )
    for loc, total in candidates:
        if isinstance(total, int):
            return total

        elif total is not None:
            msg = f"We expected to get an integer at {loc}, but instead got {total!r}"
            raise TypeError(msg)

    raise NoSearchResultNumberOfMatchesReturnedError(
        raw, tuple(loc for loc, _ in candidates)
    )


def solr_id_project_specific(doc: dict[str, Any]) -> str:
    """
    Read the bundle id of a Solr record

    Parameters
    ----------
    doc
        The record to read

    Returns
    -------
    :
        The bundle's native id
    """
    res: str = extract_one_element_list(doc["master_id"])

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
    """
    return ParsedDocument(
        id_project_specific=solr_id_project_specific(doc),
        datasets=datasets,
        version=str(extract_one_element_list(doc["version"])),
        is_latest=bool(extract_one_element_list(doc["latest"])),
        retracted=bool(extract_one_element_list(doc["retracted"])),
        nodes=(DataNodeInfo(data_node=extract_one_element_list(doc["data_node"])),),
        esgf_doc_id=extract_one_element_list(doc["id"]),
        raw_json=json.dumps(doc),
        search_api_tag=api.search_api_tag,
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
    id_project_specific = solr_id_project_specific(doc)
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

        return tuple(
            DatasetFacets.model_validate({**base, **shared, "variable": variable})
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
    """
    props: dict[str, Any] = feature["properties"]

    return ParsedDocument(
        id_project_specific=id_project_specific,
        datasets=datasets,
        version=props["version"],
        is_latest=props["latest"],
        retracted=props["retracted"],
        nodes=stac_nodes(feature),
        esgf_doc_id=feature["id"],
        raw_json=json.dumps(feature),
        search_api_tag=api.search_api_tag,
    )


@dataclass(frozen=True)
class ESGFNGCMIP6ResultParser:
    """
    Read CMIP6 results from the ESGF-NG STAC API

    Two things here are CMIP6's rather than STAC's:

    - the bundle id is the feature's `base_id` property,
      which is the same value Solr writes as `master_id`.
      Reading it means we do not have to assume anything
      about the shape of the feature id itself.
    - the project is not written as a `project` property (CMIP6 features have none),
      so it is read from `cmip6:mip_era`
      (this may be a temporary workaround, let's see if the APIs are updated).
    """

    def get_n_matches(self, raw: dict[str, Any]) -> int:
        """
        See [ResultParserProtocol.get_n_matches][esmporium.search.search_api_facade.result_parsers.ResultParserProtocol.get_n_matches].
        """  # noqa: E501
        return stac_n_matches(raw)

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
                id_project_specific=self.get_id_project_specific(feature),
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
        """  # noqa: E501
        base = {
            "id_project_specific": self.get_id_project_specific(doc),
            "project": _read_required_facet(
                doc, api, "cmip6:mip_era", doc_id=doc["id"]
            ),
        }
        row = _single_row(doc, api, facade_parameters, base=base)

        return (row,)

    def get_id_project_specific(self, feature: dict[str, Any]) -> str:
        """
        Read the bundle id of a CMIP6 STAC feature

        Parameters
        ----------
        feature
            The feature to read

        Returns
        -------
        :
            The bundle's native id
        """
        res: str = feature["properties"]["base_id"]

        return res


@dataclass(frozen=True)
class ESGFNGCMIP7ResultParser:
    """
    Read CMIP7 results from the ESGF-NG STAC API

    The two differences from [ESGFNGCMIP6ResultParser][(m).] are exactly why the parsers
    are split by project:

    - a CMIP7 feature has no `base_id`,
      so the bundle id has to be recovered
      by dropping the version token off the end of the feature id
      (see [strip_version][(m).]).
      (This may be a temporary workaround, let's see if the APIs are updated.)
    - the project is written as a plain `project` property
    """

    def get_n_matches(self, raw: dict[str, Any]) -> int:
        """
        See [ResultParserProtocol.get_n_matches][esmporium.search.search_api_facade.result_parsers.ResultParserProtocol.get_n_matches].
        """  # noqa: E501
        return stac_n_matches(raw)

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
                id_project_specific=self.get_id_project_specific(feature),
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
        """  # noqa: E501
        base = {
            "id_project_specific": self.get_id_project_specific(doc),
            "project": _read_required_facet(doc, api, "project", doc_id=doc["id"]),
        }
        row = _single_row(doc, api, facade_parameters, base=base)

        return (row,)

    def get_id_project_specific(self, feature: dict[str, Any]) -> str:
        """
        Read the bundle id of a CMIP7 STAC feature

        Parameters
        ----------
        feature
            The feature to read

        Returns
        -------
        :
            The bundle's native id, i.e. the feature id without its version token
        """
        feature_id: str = feature["id"]

        return strip_version(feature_id)


def is_version_token(token: str) -> bool:
    """
    Check whether a `.`-separated segment of an id is a version token

    A version token is written as `v` followed by digits, e.g. `v20200623`.
    `isdigit()` on the tail after the `v` requires at least one digit,
    so a bare `v` is not mistaken for a version.

    Parameters
    ----------
    token
        The segment to check

    Returns
    -------
    :
        `True` if `token` is a version token, `False` otherwise

    Examples
    --------
    >>> is_version_token("v20200623")
    True
    >>> is_version_token("v")
    False
    """
    return token.startswith("v") and token[1:].isdigit()


def strip_version(native_id: str) -> str:
    """
    Drop a trailing `.vYYYYMMDD` token so different versions can share a dataset id

    Parameters
    ----------
    native_id
        The id to strip

    Returns
    -------
    :
        `native_id` without its version token, or unchanged if it has none

    Examples
    --------
    >>> strip_version("CMIP7.CMIP.MIROC.tas.v20200623")
    'CMIP7.CMIP.MIROC.tas'
    >>> strip_version("CMIP7.CMIP.MIROC.tas")
    'CMIP7.CMIP.MIROC.tas'
    """
    parts = native_id.split(".")
    if parts and is_version_token(parts[-1]):
        return ".".join(parts[:-1])

    return native_id
