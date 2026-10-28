"""
ESGF1 search API class
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from tenacity import Retrying

from esmporium.datasets.data_access import AccessService, ContainerFormat
from esmporium.search.apis.protocol import (
    LimitOutOfRangeError,
    NoFacetValuesReturnedError,
    NoSearchResultDocumentsError,
    NoSearchResultNumberOfMatchesReturnedError,
    SearchAPI,
    single_facet_value_or_none,
)
from esmporium.search.apis.request import Request
from esmporium.search.result_normalisation import SOLR_FORMAT_TAG
from esmporium.search.result_parsing import (
    DataAccessInfo,
    DataAccessPayload,
    FileInfo,
    ParsedFileDocument,
)


def solr_bool(value: bool) -> str:
    """
    Write a boolean the way the Solr-shaped APIs expect to read it

    Parameters
    ----------
    value
        The value to write

    Returns
    -------
    :
        `value`, as these APIs spell it

    Examples
    --------
    >>> solr_bool(True)
    'true'
    >>> solr_bool(False)
    'false'
    """
    return "true" if value else "false"


def solr_facet_values(raw: dict[str, Any], facets: set[str]) -> dict[str, set[str]]:
    """
    Read the available facet values out of a Solr-shaped facet values response

    We keep only the facets which were asked about.

    Parameters
    ----------
    raw
        The response to read.

    facets
        The facets we asked about.

    Returns
    -------
    :
        The values which are available, keyed by the facet name.

    Raises
    ------
    NoFacetValuesReturnedError
        `raw` enumerates nothing at all
    """
    facet_counts = raw.get("facet_counts")
    fields = (
        facet_counts.get("facet_fields") if isinstance(facet_counts, Mapping) else None
    )
    if not fields or not isinstance(fields, Mapping):
        raise NoFacetValuesReturnedError(raw, "facet_counts.facet_fields")

    res: dict[str, set[str]] = {}
    for api_name, flat in fields.items():
        if api_name in facets:
            if not isinstance(flat, list):
                raise NoFacetValuesReturnedError(
                    raw, f"facet_counts.facet_fields.{api_name}"
                )

            # Parse the API's funny list into the facet values
            res[api_name] = set(flat[0::2])

    return res


def extract_one_element_list(value: Any) -> Any:
    """
    Unwrap a one-element list, e.g. `['CMIP5']` -> `'CMIP5'`, else leave as-is

    Solr returns most facets as single-element lists. This collapses them so a facet
    reads back as the scalar it represents.
    """
    if isinstance(value, list) and len(value) == 1:
        return value[0]
    return value


def solr_extract_result_documents(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extract result documents from a raw Solr response

    Parameters
    ----------
    raw
        Raw response

    Returns
    -------
    :
        Extracted documents

    Raises
    ------
    NoSearchResultDocumentsError
        No search result documents are included in `raw`
    """
    response = raw.get("response")
    if not isinstance(response, dict) or "docs" not in response:
        raise NoSearchResultDocumentsError(raw, "response.docs")

    docs = response["docs"]
    if not isinstance(docs, list):
        raise NoSearchResultDocumentsError(raw, "response.docs")

    res: list[dict[str, Any]] = list(docs)  # ty: ignore invalid-assignment

    return res


def solr_n_matches(raw: dict[str, Any]) -> int:
    """
    Get the number of records that matched a search from a Solr-shaped response

    This is the total for the whole search, not the number of documents this response
    carries; see
    [SearchAPI.get_n_matches][esmporium.search.apis.SearchAPI.get_n_matches].

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

    TypeError
        `raw` reports the count in the place we expect
        but as something which is not an integer
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


def solr_next_page_request(request: Request, raw: dict[str, Any]) -> Request | None:
    """
    Build the request for the page after a Solr-shaped response

    Solr is offset-based, so the next page is the same request with its `offset`
    advanced by one page (`limit`). There are no more pages once the next offset
    reaches the total the response reports at `response.numFound`.

    Parameters
    ----------
    request
        The search request that was just sent, i.e. the one `raw` answers

        Its `offset` (defaulting to `0`, the first page) and `limit` are read from
        `request.params`.

    raw
        The answer to `request`

    Returns
    -------
    :
        The request for the next page,
        or `None` if `raw` was the last page

    Raises
    ------
    UnreadableResponseError
        `raw` does not report the total number of matches at `response.numFound`
    """
    params = request.params or {}

    # A missing `offset` is expected: the first request (see `build_search_request`)
    # never sets one, and page one is offset 0. From page two on, this function is what
    # adds it (via the `replace` below). So `offset` is always on a request *we* built,
    # never read from the response: a non-int is our own bug, not a bad answer from the
    # API, so we fail loudly with a plain error.
    offset = params.get("offset", 0)
    if not isinstance(offset, int):
        msg = (
            "Solr paging expects an int 'offset' on the request we built, "
            f"got {offset!r}."
        )
        raise TypeError(msg)

    # `limit` is required: every request we build carries it (from build_search_request)
    # and it is the page size we advance `offset` by. Missing or non-int here is a
    # request we built (or were handed) wrong, not a bad answer from the API, so this is
    # a plain error rather than the response-level `UnreadableResponseError`.
    limit = params.get("limit")
    if not isinstance(limit, int):
        msg = (
            "Solr paging needs an int 'limit' on the request to know the page size, "
            f"got {limit!r}. This is a request we built without one, not a bad API "
            "answer."
        )
        raise TypeError(msg)

    if limit <= 0:
        # A page size of zero or less (e.g. a facet-values request, which uses
        # `min_limit=0`) can never advance, so there is no next page to ask for.
        return None

    num_found = solr_n_matches(raw)

    next_offset = offset + limit
    if next_offset >= num_found:
        return None

    return replace(request, params={**params, "offset": next_offset})


def solr_read_facet_list_as_strings(
    doc: dict[str, Any], api_field: str
) -> tuple[str, ...]:
    """
    Read a facet from a Solr document as a tuple of strings

    Parameters
    ----------
    doc
        Document from which to read the facet

    api_field
        Facet (i.e. field in the API response) to read

    Returns
    -------
    :
        The value of `api_field`, cast to a tuple of strings

    Raises
    ------
    NotImplementedError
        The value we found is not one we can safely cast to a tuple of strings
    """
    values = doc.get(api_field)
    if values is None:
        return ()

    if not isinstance(values, list):
        values = [values]

    return tuple(solr_facet_value_as_string(value, api_field) for value in values)


def solr_facet_value_as_string(value: Any, api_field: str) -> str:
    """
    Cast a single value read from a Solr document to a string

    Parameters
    ----------
    value
        Value to cast

    api_field
        Facet (i.e. field in the API response) `value` was read from

        Only used for error messages.

    Returns
    -------
    :
        `value`, cast to a string

    Raises
    ------
    NotImplementedError
        `value` is not one we can safely cast to a string

        Casting anything else would put a value in our database
        that nobody published,
        e.g. a `dict` would come back as `"{'a': 1}"`.
    """
    if isinstance(value, (str, int, float)):
        return str(value)

    msg = (
        f"We do not know how to read {api_field!r} out of a Solr record: "
        f"expected a string, an int, a float or nothing, got {value!r}"
    )
    raise NotImplementedError(msg)


def solr_read_facet_as_string(doc: dict[str, Any], api_field: str) -> str | None:
    """
    Read a facet from a Solr document as a single string

    Parameters
    ----------
    doc
        Document from which to read the facet

    api_field
        Facet (i.e. field in the API response) to read

    Returns
    -------
    :
        The value of `api_field`, cast to a single string

    Raises
    ------
    MultipleFacetValuesError
        In the doc, `api_field` maps to more than one value
    """
    return single_facet_value_or_none(
        solr_read_facet_list_as_strings(doc, api_field), api_field
    )


DATASET_DOCUMENTS = "Dataset"
"""The `type` of a Solr document describing a dataset version"""

FILE_DOCUMENTS = "File"
"""The `type` of a Solr document describing one file of a dataset version"""

SOLR_SERVICE_TO_SERVICE: Mapping[str, AccessService] = {
    "HTTPServer": AccessService.HTTPS,
    "OPENDAP": AccessService.OPENDAP,
    "GridFTP": AccessService.GRIDFTP,
    "Globus": AccessService.GLOBUS,
}
"""How the service in a Solr `url` triple maps onto [AccessService][esmporium.datasets.AccessService]"""  # noqa: E501

SOLR_SERVICE_TO_CONTAINER_FORMAT: Mapping[str, ContainerFormat] = {
    "HTTPServer": ContainerFormat.NETCDF,
    # An OPeNDAP URL here ends `.nc.html`: a form page for a human, not the DAP
    # endpoint. Recording it as HTML is how a reader is told that.
    "OPENDAP": ContainerFormat.HTML,
    "GridFTP": ContainerFormat.NETCDF,
    "Globus": ContainerFormat.NETCDF,
}
"""
What the bytes are on a Solr route, read from its **service** not its mime type
"""

_URL_TRIPLE_PARTS = 3


class UnreadableURLTripleError(ValueError):
    """Raised when a Solr `url` entry is not the `url|mime|service` triple we expect"""

    def __init__(self, triple: str) -> None:
        self.triple = triple
        """The entry we could not read"""

        super().__init__(
            f"The Solr url entry {triple!r} does not split into the "
            f"{_URL_TRIPLE_PARTS} `<url>|<mime>|<service>` parts every such entry "
            "has, so we cannot tell what it is a route to or how to reach it."
        )


def solr_url_triple(triple: str) -> tuple[str, str, str]:
    """
    Split a Solr `url` entry into its URL, mime type and service

    ESGF-1 publishes a file's routes as a list of `<url>|<mime>|<service>` strings,
    e.g.
    `https://esgf.ceda.ac.uk/thredds/fileServer/...nc|application/netcdf|HTTPServer`.

    Parameters
    ----------
    triple
        The entry to split

    Returns
    -------
    :
        Its URL, mime type and service

    Raises
    ------
    UnreadableURLTripleError
        `triple` does not have three parts

    Examples
    --------
    >>> solr_url_triple("https://x.invalid/t.nc|application/netcdf|HTTPServer")
    ('https://x.invalid/t.nc', 'application/netcdf', 'HTTPServer')
    """
    parts = triple.split("|")
    if len(parts) != _URL_TRIPLE_PARTS:
        raise UnreadableURLTripleError(triple)

    url, mime, service = parts

    return url, mime, service


def solr_file_access(doc: dict[str, Any]) -> DataAccessPayload:
    """
    Read the one file, and every route to it, out of a Solr `type=File` document

    A Solr File document is always about exactly one file, so this never produces a
    whole-dataset-version route: every route it returns names its file.

    Reads the same way for classic ESGF-1 and for the 1.5 bridge, which publish File
    documents of the same shape.

    Parameters
    ----------
    doc
        The document to read

    Returns
    -------
    :
        The one file, and the routes to it

    Raises
    ------
    UnreadableURLTripleError
        One of the document's `url` entries is not a three-part triple
    """
    filename = extract_one_element_list(doc["title"])
    data_node = extract_one_element_list(doc["data_node"])

    found = FileInfo(
        filename=filename,
        size=doc.get("size"),
        checksum=extract_one_element_list(doc.get("checksum")),
        checksum_type=extract_one_element_list(doc.get("checksum_type")),
        tracking_id=extract_one_element_list(doc.get("tracking_id")),
    )

    accesses = []
    for triple in doc.get("url", []):
        url, _mime, service = solr_url_triple(triple)
        accesses.append(
            DataAccessInfo(
                filename=filename,
                data_node=data_node,
                service=_solr_service(service, triple),
                container_format=_solr_container_format(service, triple),
                url=url,
                # Solr describes a route only as this triple, inside a document which
                # describes the file, so the triple is the whole of what it said
                # about the route. JSON-encoded, hence a quoted string.
                raw_json=json.dumps(triple),
            )
        )

    return DataAccessPayload(files=(found,), accesses=tuple(accesses))


def solr_parsed_file_documents(
    raw: dict[str, Any], api: SearchAPI
) -> tuple[ParsedFileDocument, ...]:
    """
    Read a `type=File` response into the file documents it carries

    The file-level counterpart of a result parser's `parse_search_results`, and
    deliberately not one of those: a File document is a File document whatever
    project it is from, so there is nothing here for a per-project parser to do.
    What *is* project-specific -- which facets the request had to narrow by for these
    answers to be placeable -- is settled before the request goes out, by
    [ResultParserProtocol.file_attribution_facets][esmporium.search.ResultParserProtocol.file_attribution_facets].

    Parameters
    ----------
    raw
        The response to read

        Usually the answer to a request built with
        [SearchAPIESGF1Solr.build_file_search_request][(m).SearchAPIESGF1Solr.build_file_search_request].

    api
        The API the response came from, used to split it into documents

    Returns
    -------
    :
        One [ParsedFileDocument][esmporium.search.ParsedFileDocument] per document

    Raises
    ------
    KeyError
        A document does not name the dataset it belongs to

        Trusted the same way `solr_file_access` trusts `title`: a File document
        which does not say which dataset its file is part of is one we could not
        place anywhere, so it is better to hear about it than to drop it.

    UnreadableURLTripleError
        One of a document's `url` entries is not a three-part triple
    """
    return tuple(
        ParsedFileDocument(
            dataset_id=str(extract_one_element_list(doc["dataset_id"])),
            payload=solr_file_access(doc),
        )
        for doc in api.extract_result_documents(raw)
    )


def solr_file_search_params(limit: int) -> dict[str, Any]:
    """
    Build the parameters a `type=File` request shares across the Solr dialects

    Only the parameters whose spelling the two agree on. How several values for one
    facet are joined is **not** one of them, which is why this does not take any
    facets: classic Solr ORs on a repeated parameter and the 1.5 bridge ORs on a
    comma, and the bridge silently keeps only *one* value of a repeated parameter
    rather than rejecting it. Asking the bridge about several datasets the classic
    way therefore returns an answer about one of them, with nothing to say so. Each
    API spells its own facets, in its own `build_file_search_request`.

    Parameters
    ----------
    limit
        Page size

    Returns
    -------
    :
        The shared parameters
    """
    return {
        "format": "application/solr+json",
        "type": FILE_DOCUMENTS,
        "limit": limit,
    }


def _solr_service(service: str, triple: str) -> AccessService:
    """Read a triple's service as an [AccessService][esmporium.datasets.AccessService]"""  # noqa: E501
    found = SOLR_SERVICE_TO_SERVICE.get(service)
    if found is None:
        msg = (
            f"The Solr url entry {triple!r} names the service {service!r}, "
            f"which we have no AccessService for. "
            f"The services we know are: {sorted(SOLR_SERVICE_TO_SERVICE)}. "
            "We raise rather than skipping the route, because a route we silently "
            "dropped is a way of getting this data that we would never know we had "
            "been offered."
        )
        raise ValueError(msg)

    return found


def _solr_container_format(service: str, triple: str) -> ContainerFormat:
    """Read what a triple's service implies the bytes are"""
    found = SOLR_SERVICE_TO_CONTAINER_FORMAT.get(service)
    if found is None:
        msg = (
            f"The Solr url entry {triple!r} names the service {service!r}, "
            f"which we have no ContainerFormat for. "
            f"The services we know are: {sorted(SOLR_SERVICE_TO_CONTAINER_FORMAT)}. "
            "We raise rather than skipping the route, because a route we silently "
            "dropped is a way of getting this data that we would never know we had "
            "been offered."
        )
        raise ValueError(msg)

    return found


@dataclass(frozen=True)
class SearchAPIESGF1Solr:
    """
    ESGF1 search API that uses the SOLR format

    Instances of this class should mirror the (relevant) behaviour
    of the ESGF1 search APIs.
    """

    host: str
    """See [SearchAPI.host][esmporium.search.apis.SearchAPI.host]."""

    retrying: Retrying
    """See [SearchAPI.retrying][esmporium.search.apis.SearchAPI.retrying]."""

    raw_docs_format_tag: str = SOLR_FORMAT_TAG
    """See [SearchAPI.raw_docs_format_tag][esmporium.search.apis.SearchAPI.raw_docs_format_tag]."""  # noqa: E501

    timeout: float = 30.0
    """See [SearchAPI.timeout][esmporium.search.apis.SearchAPI.timeout]."""

    scheme: str = "https"
    """See [SearchAPI.scheme][esmporium.search.apis.SearchAPI.scheme]."""

    distrib: bool = True
    """
    Whether to perform a distributed search or not

    With this on, the search API answers for every node it knows about,
    which is what makes "search ESGF" a single request rather than one per node.
    Turning this off asks the node only about the data it holds itself,
    which is what you want when you are asking about a specific node.
    """

    min_limit: int = 0
    """
    Minimum value of limit accepted by this API
    """

    max_limit: int = 10_000
    """
    Maximum value of limit accepted by this API
    """

    def build_search_request(
        self, facet_values: Mapping[str, tuple[str, ...]], limit: int
    ) -> Request:
        """
        See [SearchAPI.build_search_request][esmporium.search.apis.SearchAPI.build_search_request].
        """  # noqa: E501
        if limit < self.min_limit or limit > self.max_limit:
            raise LimitOutOfRangeError(
                limit, min_limit=self.min_limit, max_limit=self.max_limit
            )

        params: dict[str, Any] = {
            "format": "application/solr+json",
            # `type` is a document-type discriminator inside one index, and we want
            # the dataset documents. `Dataset` is also the server's default, so
            # saying it changes nothing today -- but it is said anyway, for two
            # reasons. The failure mode if a node's default ever changed is silent
            # corruption rather than an error: a File document carries `master_id`,
            # `version` and `data_node`, i.e. every field the dataset parser reads,
            # and its `master_id` is the dataset's with the filename appended, so we
            # would quietly store dataset rows whose id ended in `.nc`. And the
            # sibling request, `solr_file_search_request`, asks for `File`; leaving
            # the one parameter that distinguishes them implicit here would hide the
            # distinction in exactly the place a reader goes looking for it.
            "type": DATASET_DOCUMENTS,
            "limit": limit,
            "distrib": solr_bool(self.distrib),
        }
        for api_name, values in facet_values.items():
            # A list becomes a repeated parameter,
            # which is how ESGF1 APIs perform OR queries
            # across multiple values for a given facet.
            params[api_name] = list(values)

        return Request("GET", "/esg-search/search", params=params)

    def build_file_search_request(
        self,
        dataset_ids: Sequence[str],
        limit: int,
        facet_values: Mapping[str, Sequence[str]] | None = None,
    ) -> Request:
        """
        Build the `type=File` request that gets per-file information for datasets

        Solr publishes `access`, `size` and `number_of_files` on a dataset document
        but **no per-file URLs**, so getting them is a second request. This is the
        whole asymmetry with ESGF-NG, where a STAC item's `assets` are its files and
        the dataset search already carries them.

        Parameters
        ----------
        dataset_ids
            The node-specific dataset ids to ask about, i.e.
            `<instance_id>|<data_node>`

            These are exactly the `id` the dataset documents published, and exactly
            what a File document names as its `dataset_id`, so the answers join back
            onto the datasets with no lookup.

        limit
            Page size

        facet_values
            Any further facets to narrow by, keyed as this API names them

            This is how `variable` is passed, and passing it matters: a CMIP5
            `dataset_id` is *table*-grained, so without narrowing, asking about one
            CMIP5 dataset returns every variable in its table. The facet is spelled
            `variable` for CMIP5 and `variable_id` for CMIP6/CMIP7, so the caller
            must read the name from the facade rather than hard-coding either.

        Returns
        -------
        :
            The request to send
        """
        params = solr_file_search_params(limit)
        params["distrib"] = solr_bool(self.distrib)
        # A list becomes a repeated parameter, which is how this API ORs.
        params["dataset_id"] = list(dataset_ids)
        for api_name, values in (facet_values or {}).items():
            params[api_name] = list(values)

        return Request("GET", "/esg-search/search", params=params)

    def next_page_request(
        self, request: Request, raw: dict[str, Any]
    ) -> Request | None:
        """
        See [SearchAPI.next_page_request][esmporium.search.apis.SearchAPI.next_page_request].
        """  # noqa: E501
        return solr_next_page_request(request, raw)

    def get_n_matches(self, raw: dict[str, Any]) -> int:
        """
        See [SearchAPI.get_n_matches][esmporium.search.apis.SearchAPI.get_n_matches].
        """
        return solr_n_matches(raw)

    def build_get_facet_values_for_project_request(
        self, facets: set[str], project: str
    ) -> Request:
        """
        See [SearchAPI.build_get_facet_values_for_project_request][esmporium.search.apis.SearchAPI.build_get_facet_values_for_project_request].
        """  # noqa: E501
        params: dict[str, Any] = {
            "format": "application/solr+json",
            # Sorted so the request we build is deterministic.
            "facets": ",".join(sorted(facets)),
            # We want the facet values, not the records,
            # so we ask for the smallest page we are allowed to ask for.
            "limit": self.min_limit,
            "distrib": solr_bool(self.distrib),
            "project": project,
        }

        return Request("GET", "/esg-search/search", params=params)

    def parse_facet_values(
        self, raw: dict[str, Any], facets: set[str]
    ) -> dict[str, set[str]]:
        """
        See [SearchAPI.parse_facet_values][esmporium.search.apis.SearchAPI.parse_facet_values].
        """  # noqa: E501
        return solr_facet_values(raw, facets)

    def parse_facet_patterns(
        self, raw: dict[str, Any], facets: set[str]
    ) -> dict[str, re.Pattern[str]]:
        """
        See [SearchAPI.parse_facet_patterns][esmporium.search.apis.SearchAPI.parse_facet_patterns].
        """  # noqa: E501
        # ESGF1 always enumerates its facet values; it never describes their form.
        return {}

    def extract_result_documents(self, raw: dict[str, Any]) -> list[dict[str, Any]]:
        """
        See [SearchAPI.extract_result_documents][esmporium.search.apis.SearchAPI.extract_result_documents].
        """  # noqa: E501
        return solr_extract_result_documents(raw)

    def read_facet(self, doc: dict[str, Any], api_field: str) -> str | None:
        """
        See [SearchAPI.read_facet][esmporium.search.apis.SearchAPI.read_facet].
        """
        return solr_read_facet_as_string(doc, api_field)

    def read_facet_list(self, doc: dict[str, Any], api_field: str) -> tuple[str, ...]:
        """
        See [SearchAPI.read_facet_list][esmporium.search.apis.SearchAPI.read_facet_list].
        """  # noqa: E501
        return solr_read_facet_list_as_strings(doc, api_field)
