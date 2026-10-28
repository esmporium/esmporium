"""
ESGF-NG search API class
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from tenacity import Retrying

from esmporium.datasets.data_access import AccessService, ContainerFormat
from esmporium.search.apis.protocol import (
    LimitOutOfRangeError,
    NoFacetValuesReturnedError,
    NoSearchResultDocumentsError,
    NoSearchResultNumberOfMatchesReturnedError,
    SearchAPI,
    UncompilableFacetPatternError,
    describe_search_api,
    read_response_path,
    single_facet_value_or_none,
)
from esmporium.search.apis.request import Request
from esmporium.search.checksums import decode_multihash_checksum
from esmporium.search.result_normalisation import STAC_FORMAT_TAG
from esmporium.search.result_parsing import (
    DataAccessInfo,
    DataAccessPayload,
    DataNodeInfo,
    FileInfo,
)


# In future, `aggregations` could be used to return value counts per facet value.
# Currently, only esgf-ng-east handles aggregation, not west.
# Sticking to summary for now as this is handled by east and west.
def stac_summary_values(raw: dict[str, Any], facets: set[str]) -> dict[str, set[str]]:
    """
    Read the available facet values out of a STAC-shaped facet values response

    Not every summary enumerates values.
    STAC also allows a summary to be a regular expression.
    Those facets are left out of the result:
    "we cannot list this one" and "this one has no values"
    have to stay distinguishable.

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
        `raw` summarises nothing at all,
        so this deployment cannot tell us anything about any facet.
    """
    summaries = raw.get("summaries")
    if not summaries or not isinstance(summaries, Mapping):
        raise NoFacetValuesReturnedError(raw, "summaries")

    res: dict[str, set[str]] = {}
    for api_name, summary in summaries.items():
        # This match is deliberately exact (case-sensitive). We only ever build
        # lowercase-prefixed names (`cmip7:variable_id`), and both ESGF-NG
        # deployments (east and west) key their summaries the same way today,
        # so exact matching works.
        # Do not "fix" this by case-folding the keys: these collections
        # really do treat case as significant -- east's CMIP6Plus, for one,
        # carries both `cmip6plus:Conventions` and `cmip6plus:conventions` as
        # separate keys, so lowering every key would collapse the two and
        # silently clobber one. The cost of exactness is that a node changing a
        # key's case would drop that facet silently (it reads as "no enumerable
        # values"); `test_summary_facet_keys_have_not_drifted_in_case` is what
        # turns that drift into a loud failure.
        if api_name in facets:
            if not isinstance(summary, list):
                # A range or a pattern, i.e. not a list of values.
                # `stac_summary_patterns` is where those are read.
                continue

            values = {
                value
                for value in summary
                # The `isinstance` check ensures that we don't pick up dicts.
                # The dicts probably shouldn't be there.
                # Where they are there (e.g. CMIP6 member_id),
                # that looks like a bug in the CMIP6 CVs
                # because CMIP6 uses variant_label, not member_id.
                # Hence leave this for now,
                # but we should raise an error in CMIP6 CVs at some point
                # and we should be aware that this means
                # that member_id is silently dropped from CMIP6 pattern parsing
                # at the moment, rather than loudly dropped.
                if isinstance(value, str)
            }
            if values:
                res[api_name] = values

    return res


def stac_summary_patterns(
    raw: dict[str, Any], facets: set[str]
) -> dict[str, re.Pattern[str]]:
    """
    Read the supported facet patterns out of a STAC-shaped facet values response

    Not every summary provides supported patterns.
    STAC also allows a summary to enumerate supported values.
    Those facets are left out of the result:
    "we cannot list this one" and "this one has no pattern"
    have to stay distinguishable.

    Parameters
    ----------
    raw
        The response to read.

    facets
        The facets we asked about.

    Returns
    -------
    :
        The pattern which is supported, keyed by the facet name.

    Raises
    ------
    NoFacetValuesReturnedError
        `raw` summarises nothing at all,
        so this deployment cannot tell us anything about any facet.

    UncompilableFacetPatternError
        A pattern specified for a given facet name
        is not able to be compiled as a regular expression.
    """
    summaries = raw.get("summaries")
    if not summaries or not isinstance(summaries, Mapping):
        raise NoFacetValuesReturnedError(raw, "summaries")

    res: dict[str, re.Pattern[str]] = {}
    for api_name, summary in summaries.items():
        # Exact, case-sensitive match, for the reasons in `stac_summary_values`.
        if api_name in facets:
            if not isinstance(summary, str):
                # Enumerated values or something else.
                # `stac_summary_values` is where those are read.
                continue

            try:
                res[api_name] = re.compile(summary)
            except re.error as exc:
                raise UncompilableFacetPatternError(api_name, summary) from exc

    return res


def stac_extract_result_documents(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Get the response documents from a raw STAC response

    Parameters
    ----------
    raw
        Raw STAC response to parse

    Returns
    -------
    :
        Extracted response documents

    Raises
    ------
    NoSearchResultDocumentsError
        No search result documents are included in `raw`
    """
    if "features" not in raw:
        raise NoSearchResultDocumentsError(raw, "features")

    features: list[dict[str, Any]] = raw["features"]

    return list(features)


def stac_n_matches(raw: dict[str, Any]) -> int:
    """
    Get the number of records that matched a search from a STAC-shaped response

    This is the total for the whole search, not the number of features this response
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
    loc = "numberMatched"
    total = raw.get(loc)
    if isinstance(total, int):
        return total

    elif total is not None:
        msg = f"We expected to get an integer at {loc!r}, but instead got {total!r}"
        raise TypeError(msg)

    raise NoSearchResultNumberOfMatchesReturnedError(raw, loc)


def stac_next_page_request(request: Request, raw: dict[str, Any]) -> Request | None:
    """
    Build the request for the page after a STAC-shaped response

    STAC is cursor-based, so the next page is not something we compute: the server
    hands it back in the response's `links`, as the entry with `rel: "next"`. That
    entry carries the full request to send for the next page (a continuation token
    lives in its `body`), so we resend it verbatim. There are no more pages once the
    response carries no such link, which is how the server says "that was the last
    page".

    Parameters
    ----------
    request
        The search request that was just sent, i.e. the one `raw` answers

        Not read: STAC describes the next page entirely in `raw`. It is accepted so
        this matches the shape every API's `next_page_request` takes.

    raw
        The answer to `request`

    Returns
    -------
    :
        The request for the next page,
        or `None` if `raw` carries no `next` link (it was the last page)

    Raises
    ------
    UnreadableResponseError
        `raw` carries a `next` link but not in a shape we can resend
        (it does not carry both the URL and the body to POST)
    """
    links = raw.get("links")
    if not isinstance(links, list):
        return None

    nxt = next(
        (
            link
            for link in links
            if isinstance(link, Mapping) and link.get("rel") == "next"
        ),
        None,
    )
    if nxt is None:
        return None

    href = read_response_path(
        dict(nxt), "href", what="the URL of the next page of results"
    )
    body = read_response_path(
        dict(nxt), "body", what="the body to POST for the next page of results"
    )
    # The href is a full URL, but we send it via the API's own host, so we keep only
    # its path (the host on the link matches the host we are already talking to).
    return Request(nxt.get("method", "POST"), urlsplit(href).path, json_body=body)


def stac_read_facet_list_as_strings(
    feature: dict[str, Any], api_field: str, context: str | None = None
) -> tuple[str, ...]:
    """
    Read a facet from a STAC feature (document) as a tuple of strings

    Parameters
    ----------
    feature
        Feature (i.e. document) from which to read the facet

    api_field
        Facet (i.e. field in the API response) to read

    context
        Anything which helps reproduce a feature we cannot read

        See
        [UnreadableResponseError][esmporium.search.apis.UnreadableResponseError].

    Returns
    -------
    :
        The value of `api_field`, cast to a tuple of strings

        A feature which carries no `api_field` reads as no values at all:
        an absent facet is a normal thing for a record to have.

    Raises
    ------
    UnreadableResponseError
        `feature` carries no `properties` at all

    NotImplementedError
        The value we found is not one we can safely cast to a tuple of strings
    """
    # `properties` is where a STAC feature keeps its facets,
    # so a feature without it is not one we can read at all.
    # The facet itself is read tolerantly on top of that: a record simply
    # not carrying `api_field` is normal and reads as no values.
    properties = read_response_path(
        feature,
        "properties",
        what=f"the facets of this record ({api_field!r} in particular)",
        context=context,
    )
    values = properties.get(api_field)
    if values is None:
        return ()

    if not isinstance(values, list):
        values = [values]

    return tuple(stac_facet_value_as_string(value, api_field) for value in values)


def stac_facet_value_as_string(value: Any, api_field: str) -> str:
    """
    Cast a single value read from a STAC feature (document) to a string

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
        f"We do not know how to read {api_field!r} out of a STAC record: "
        f"expected a string, an int, a float or nothing, got {value!r}"
    )
    raise NotImplementedError(msg)


def stac_read_facet_as_string(
    feature: dict[str, Any], api_field: str, context: str | None = None
) -> str | None:
    """
    Read a facet from a STAC feature (document) as a single string

    Parameters
    ----------
    feature
        Feature (i.e. document) from which to read the facet

    api_field
        Facet (i.e. field in the API response) to read

    context
        Anything which helps reproduce a feature we cannot read

        See
        [UnreadableResponseError][esmporium.search.apis.UnreadableResponseError].

    Returns
    -------
    :
        The value of `api_field`, cast to a single string

    Raises
    ------
    UnreadableResponseError
        `feature` carries no `properties` at all

    MultipleFacetValuesError
        In the doc, `api_field` maps to more than one value
    """
    return single_facet_value_or_none(
        stac_read_facet_list_as_strings(feature, api_field, context), api_field
    )


def stac_nodes(
    feature: dict[str, Any], api: SearchAPI | None = None
) -> tuple[DataNodeInfo, ...]:
    """
    Get the distinct data nodes a STAC feature's assets are hosted on

    Parameters
    ----------
    feature
        Feature (i.e. document) from which to read the data nodes

    api
        The search API the feature came from

        Only used to say who answered if an asset cannot be read.

    Returns
    -------
    :
        The distinct data nodes `feature`'s assets are hosted on

        A feature with no assets is hosted nowhere,
        so it has no data nodes.

    Raises
    ------
    UnreadableResponseError
        `feature` does not carry an id.

        One of `feature`'s assets does not say which node hosts it.

        Something is hosting it and we cannot see what,
        which means the response is not the shape we expect,
        so we fail loudly rather than quietly dropping the node.
    """
    context = describe_search_api(api) if api is not None else None
    feature_id = read_response_path(
        feature, "id", what="this record's id", context=context
    )

    hosts: list[str] = []
    # No assets at all is fine: nothing is hosting this, so there is no node to report.
    for name, asset in feature.get("assets", {}).items():
        # `alternate:name` (STAC alternate-assets extension) is the canonical data-node
        # identity, matching Solr's `data_node` (e.g. `ceda.ac.uk`).
        # Deliberately do NOT read `href` here:
        # `href` is the file-download URL (e.g. `dap.ceda.ac.uk`),
        # a different concept from the data node.
        # `href` is read by `stac_data_access` below, which is the file-access step.
        # Read from the asset, not down a path from the feature
        # because asset names are filenames,
        # so they contain dots and cannot be spelled as a path segment here.
        host = read_response_path(
            asset,
            "alternate:name",
            what="the data node",
            context=context,
            lead=(
                f"In the following, `response` refers to the ['assets'][{name!r}] "
                f"path in the API response's {feature_id!r} feature. "
                f"The information provided for the {name!r} asset of {feature_id!r} "
                "does not specify the data node."
            ),
        )
        if host not in hosts:
            hosts.append(host)

    return tuple(DataNodeInfo(host) for host in hosts)


STAC_PROTOCOL_TO_SERVICE: Mapping[str, AccessService] = {
    "https": AccessService.HTTPS,
    "http": AccessService.HTTPS,
    "opendap": AccessService.OPENDAP,
    "globus": AccessService.GLOBUS,
    "gridftp": AccessService.GRIDFTP,
    # A kerchunk sidecar is *fetched over HTTPS* -- its `href` is an https URL.
    # STAC writes the format in `protocol`, which is exactly why `service` and
    # `container_format` are two separate columns and not one.
    "kerchunk": AccessService.HTTPS,
}
"""How a STAC asset's `protocol` maps onto [AccessService][esmporium.datasets.AccessService]"""  # noqa: E501

STAC_TYPE_TO_CONTAINER_FORMAT: Mapping[str, ContainerFormat] = {
    "application/netcdf": ContainerFormat.NETCDF,
    "application/x-netcdf": ContainerFormat.NETCDF,
    "application/kerchunk": ContainerFormat.KERCHUNK,
    "application/vnd.zarr": ContainerFormat.ZARR,
    "text/html": ContainerFormat.HTML,
}
"""
How a STAC asset's `type` maps onto [ContainerFormat][esmporium.datasets.ContainerFormat]

Keyed by media type with its parameters stripped and in lower case, which is what
[media_type_without_parameters][(m).media_type_without_parameters] hands the lookup.

The zarr key is `application/vnd.zarr` -- a dot. `application/vnd+zarr` is a
long-lived typo rather than an alternative spelling (pystac shipped and then fixed
the same one), and nothing publishes it.

Icechunk is deliberately absent: it has no registered media type, so there is nothing
to key it by. An icechunk asset therefore raises
[UnknownContainerFormatError][(m).UnknownContainerFormatError], which is what we want
-- see [ContainerFormat.ICECHUNK][esmporium.datasets.ContainerFormat.ICECHUNK].
"""  # noqa: E501

DATA_ROLE = "data"
"""The asset role that means "this asset is the data", rather than a thumbnail or similar"""  # noqa: E501


class UnknownAccessServiceError(ValueError):
    """
    Raised when a STAC asset names a protocol we cannot classify

    i.e. one with no [AccessService][esmporium.datasets.AccessService].
    """

    def __init__(self, protocol: str, asset_name: str, feature_id: str) -> None:
        self.protocol = protocol
        """The protocol the asset named"""

        self.asset_name = asset_name
        """The asset that named it"""

        super().__init__(
            f"The {asset_name!r} asset of {feature_id!r} names the protocol "
            f"{protocol!r}, which we have no AccessService for. "
            f"The protocols we know are: {sorted(STAC_PROTOCOL_TO_SERVICE)}. "
            "We raise rather than skipping the asset, "
            "because a route we silently dropped is a way of getting this data "
            "that we would never know we had been offered."
        )


class UnknownContainerFormatError(ValueError):
    """
    Raised when a STAC asset is of a type we cannot classify

    i.e. one with no [ContainerFormat][esmporium.datasets.ContainerFormat].
    """

    def __init__(self, asset_type: str, asset_name: str, feature_id: str) -> None:
        self.asset_type = asset_type
        """The type the asset named"""

        self.asset_name = asset_name
        """The asset that named it"""

        super().__init__(
            f"The {asset_name!r} asset of {feature_id!r} is of type "
            f"{asset_type!r}, which we have no ContainerFormat for. "
            f"The types we know are: {sorted(STAC_TYPE_TO_CONTAINER_FORMAT)} "
            "(matched with any media type parameters, e.g. '; version=3', stripped). "
            "We raise rather than skipping the asset, "
            "because a route we silently dropped is a way of getting this data "
            "that we would never know we had been offered."
        )


def stac_asset_roles(asset: Mapping[str, Any]) -> tuple[str, ...]:
    """
    Get the roles a STAC asset claims

    Reads `roles`, falling back to `role`. The fallback is not politeness about the
    spec: ESGF-NG west publishes its kerchunk asset with `"roles": null` and a
    misspelled `"role": ["data", "virtual"]`, so reading only `roles` would see
    nothing and treat the whole virtual-access route as role-less.

    Parameters
    ----------
    asset
        The asset to read

    Returns
    -------
    :
        The roles claimed, or empty if the asset claims none
    """
    for key in ("roles", "role"):
        roles = asset.get(key)
        if roles:
            return tuple(roles)

    return ()


def stac_asset_is_one_file(name: str, asset: Mapping[str, Any]) -> bool:
    """
    Work out whether a STAC asset is one file, or a route to the whole version

    Decided structurally: an asset is one file if its name is the last segment of its
    `file:local_path`. STAC keys a data asset by its filename, and publishes that same
    filename at the end of the path, so the two agreeing is what "this asset is a
    file" looks like in the data.

    Deliberately not a hard-coded list of the non-file asset names we have seen
    (`globus` and `reference_file`). Those are west-only today and the set will grow,
    so a list would quietly start mis-classifying the next one to appear. Checking
    the shape instead means a new whole-version asset is classified correctly on the
    day it turns up:

    - a `.nc` asset is named `tas_Amon_....nc` and its path ends `/tas_Amon_....nc`,
      so it is one file
    - the `globus` asset's path is a *directory*, ending `/v20181126`, which is not
      `globus`, so it is not one file
    - the `reference_file` asset has no `file:local_path` at all, so it is not one
      file

    Parameters
    ----------
    name
        The asset's name, i.e. its key in the feature's `assets`

    asset
        The asset itself

    Returns
    -------
    :
        Whether this asset is one file of the dataset version
    """
    local_path = asset.get("file:local_path")
    if not local_path:
        return False

    return str(local_path).rsplit("/", maxsplit=1)[-1] == name


def stac_data_access(
    feature: dict[str, Any], api: SearchAPI | None = None
) -> DataAccessPayload:
    """
    Read the files and the routes to them out of a STAC feature's assets

    ESGF-NG needs no second request for this: a STAC item is one dataset version and
    its `assets` are that version's files, plus any whole-version routes the node
    offers.

    Only assets claiming the `data` role are read. That is both the filter that keeps
    a future thumbnail or metadata asset out of the route table, and the reason
    [stac_asset_roles][(m).stac_asset_roles] has to cope with the misspelled `role`
    key.

    `properties.access` is deliberately **never** read. It claims to list the
    services a dataset is available over, but it lies: items advertise
    `["HTTPServer", "OPENDAP", "GridFTP", "Globus"]` while carrying only https,
    globus and kerchunk assets, and east CMIP6 omits it entirely. Only an asset that
    is really there becomes a route.

    Parameters
    ----------
    feature
        Feature (i.e. document) to read

    api
        The search API the feature came from

        Only used to say who answered if an asset cannot be read.

    Returns
    -------
    :
        The files and routes the feature described

        A feature with no assets describes neither, which is not an error.

    Raises
    ------
    UnreadableResponseError
        `feature` does not carry an id, or one of its assets does not say which node
        hosts it or carries no `href`.

    UnknownAccessServiceError
        An asset names a protocol we have no
        [AccessService][esmporium.datasets.AccessService] for.

    UnknownContainerFormatError
        An asset is of a type we have no
        [ContainerFormat][esmporium.datasets.ContainerFormat] for.
    """
    context = describe_search_api(api) if api is not None else None
    feature_id = read_response_path(
        feature, "id", what="this record's id", context=context
    )

    files: list[FileInfo] = []
    accesses: list[DataAccessInfo] = []
    # No assets at all is fine: this record offers no way of getting the data, which
    # is a thing worth recording faithfully rather than an error.
    for name, asset in feature.get("assets", {}).items():
        if DATA_ROLE not in stac_asset_roles(asset):
            continue

        # Read from the asset, not down a path from the feature, because asset names
        # are filenames, so they contain dots and cannot be spelled as a path
        # segment -- the same reason `stac_nodes` reads this way.
        lead = (
            f"In the following, `response` refers to the ['assets'][{name!r}] "
            f"path in the API response's {feature_id!r} feature."
        )
        data_node = read_response_path(
            asset, "alternate:name", what="the data node", context=context, lead=lead
        )
        url = read_response_path(
            asset, "href", what="the route's URL", context=context, lead=lead
        )

        is_one_file = stac_asset_is_one_file(name, asset)
        if is_one_file:
            files.append(_stac_file(name, asset))

        accesses.append(
            DataAccessInfo(
                filename=name if is_one_file else None,
                data_node=data_node,
                service=_stac_service(asset, asset_name=name, feature_id=feature_id),
                container_format=_stac_container_format(
                    asset, asset_name=name, feature_id=feature_id
                ),
                url=url,
                raw_json=json.dumps(asset, sort_keys=True),
            )
        )

    return DataAccessPayload(files=tuple(files), accesses=tuple(accesses))


def _stac_service(
    asset: Mapping[str, Any], *, asset_name: str, feature_id: str
) -> AccessService:
    """Read an asset's `protocol` as an [AccessService][esmporium.datasets.AccessService]"""  # noqa: E501
    protocol = str(asset.get("protocol"))
    service = STAC_PROTOCOL_TO_SERVICE.get(protocol)
    if service is None:
        raise UnknownAccessServiceError(protocol, asset_name, feature_id)

    return service


def media_type_without_parameters(media_type: str) -> str:
    """
    Reduce a media type to the part we classify on

    A media type may carry parameters after a semicolon, and a store is where they
    actually turn up: STAC's zarr best practices write
    `application/vnd.zarr; version=3`, and `; profile=multiscales` for a pyramided
    store. Those say something about *that* store, not about what kind of thing it is,
    so they are dropped before the lookup -- matching on the whole string would miss
    every real zarr asset while matching every netCDF one, so the gap would not show
    up until the day it mattered.

    Case is dropped too, because a media type is case-insensitive.

    Parameters
    ----------
    media_type
        The media type to reduce, e.g. `application/vnd.zarr; version=3`

    Returns
    -------
    :
        Its type and subtype, lower-cased, e.g. `application/vnd.zarr`
    """
    return media_type.split(";", maxsplit=1)[0].strip().lower()


def _stac_container_format(
    asset: Mapping[str, Any], *, asset_name: str, feature_id: str
) -> ContainerFormat:
    """Read an asset's `type` as a [ContainerFormat][esmporium.datasets.ContainerFormat]"""  # noqa: E501
    asset_type = str(asset.get("type"))
    container_format = STAC_TYPE_TO_CONTAINER_FORMAT.get(
        media_type_without_parameters(asset_type)
    )
    if container_format is None:
        raise UnknownContainerFormatError(asset_type, asset_name, feature_id)

    return container_format


def _stac_file(name: str, asset: Mapping[str, Any]) -> FileInfo:
    """
    Read a file-level STAC asset as a [FileInfo][esmporium.search.FileInfo]

    `tracking_id` is matched by suffix rather than by name because STAC prefixes it
    with the project: `cmip6:tracking_id`, `cmip7:tracking_id`,
    `cmip6plus:tracking_id`, `cordex-cmip6:tracking_id`. Matching the suffix means a
    project we have not met yet works without a code change.
    """
    published_checksum = asset.get("file:checksum")
    checksum = (
        decode_multihash_checksum(published_checksum)
        if published_checksum is not None
        else None
    )

    return FileInfo(
        filename=name,
        size=asset.get("file:size"),
        checksum=checksum.value if checksum is not None else None,
        checksum_type=checksum.checksum_type if checksum is not None else None,
        tracking_id=_suffixed_value(asset, ":tracking_id"),
    )


def _suffixed_value(asset: Mapping[str, Any], suffix: str) -> Any:
    """
    Read the one key of `asset` ending in `suffix`, or `None` if there is none

    For project-prefixed STAC fields, where the prefix is the project in lower case
    and so varies with the collection.
    """
    for key, value in asset.items():
        if key.endswith(suffix):
            return value

    return None


@dataclass(frozen=True)
class SearchAPIESGFNGSTAC:
    """
    ESGF-NG search API that is based on the STAC standard

    Instances of this class should mirror the (relevant) behaviour
    of the ESGF-NG (search) APIs.
    """

    host: str
    """See [SearchAPI.host][esmporium.search.apis.SearchAPI.host]."""

    retrying: Retrying
    """See [SearchAPI.retrying][esmporium.search.apis.SearchAPI.retrying]."""

    raw_docs_format_tag: str = STAC_FORMAT_TAG
    """See [SearchAPI.raw_docs_format_tag][esmporium.search.apis.SearchAPI.raw_docs_format_tag]."""  # noqa: E501

    timeout: float = 30.0
    """See [SearchAPI.timeout][esmporium.search.apis.SearchAPI.timeout]."""

    scheme: str = "https"
    """See [SearchAPI.scheme][esmporium.search.apis.SearchAPI.scheme]."""

    min_limit: int = 1
    """
    Minimum value of limit accepted by this API
    """

    max_limit: int = 10_000
    """
    Maximum value of limit accepted by this API
    """

    def build_search_request(
        self,
        facet_values: Mapping[str, tuple[str, ...]],
        limit: int,
    ) -> Request:
        """
        See [SearchAPI.build_search_request][esmporium.search.apis.SearchAPI.build_search_request].
        """  # noqa: E501
        if limit < self.min_limit or limit > self.max_limit:
            raise LimitOutOfRangeError(
                limit, min_limit=self.min_limit, max_limit=self.max_limit
            )

        json_body: dict[str, Any] = {
            "filter-lang": "cql2-json",
            "limit": limit,
        }
        and_clauses: list[dict[str, Any]] = [
            {
                "op": "in",
                "args": [
                    # Assume that the caller puts the prefix on
                    {"property": facet},
                    list(values),
                ],
            }
            for facet, values in facet_values.items()
        ]
        if and_clauses:
            json_body["filter"] = {"op": "and", "args": and_clauses}

        return Request("POST", "/search", json_body=json_body)

    def next_page_request(
        self, request: Request, raw: dict[str, Any]
    ) -> Request | None:
        """
        See [SearchAPI.next_page_request][esmporium.search.apis.SearchAPI.next_page_request].
        """  # noqa: E501
        return stac_next_page_request(request, raw)

    def get_n_matches(self, raw: dict[str, Any]) -> int:
        """
        See [SearchAPI.get_n_matches][esmporium.search.apis.SearchAPI.get_n_matches].
        """
        return stac_n_matches(raw)

    def build_get_facet_values_for_project_request(
        self, facets: set[str], project: str
    ) -> Request:
        """
        See [SearchAPI.build_get_facet_values_for_project_request][esmporium.search.apis.SearchAPI.build_get_facet_values_for_project_request].
        """  # noqa: E501
        # Assumes project has been mapped to the intended collection style.
        #
        # The collection name is case-sensitive on east: it must be the exact
        # ESGF spelling (e.g. `CMIP6Plus`, not `CMIP6PLUS` or `cmip6plus`) or
        # east returns 404. West is case-insensitive. The response echoes the
        # name back in its `id` field, and the two deployments disagree on its
        # case (east keeps `CMIP7`, west lowercases it to `cmip7`), but we never
        # read `id`, so that difference is nothing we have to handle.
        return Request("GET", f"/collections/{project}")

    def parse_facet_values(
        self, raw: dict[str, Any], facets: set[str]
    ) -> dict[str, set[str]]:
        """
        See [SearchAPI.parse_facet_values][esmporium.search.apis.SearchAPI.parse_facet_values].
        """  # noqa: E501
        return stac_summary_values(raw, facets)

    def parse_facet_patterns(
        self, raw: dict[str, Any], facets: set[str]
    ) -> dict[str, re.Pattern[str]]:
        """
        See [SearchAPI.parse_facet_patterns][esmporium.search.apis.SearchAPI.parse_facet_patterns].
        """  # noqa: E501
        return stac_summary_patterns(raw, facets)

    def extract_result_documents(self, raw: dict[str, Any]) -> list[dict[str, Any]]:
        """
        See [SearchAPI.extract_result_documents][esmporium.search.apis.SearchAPI.extract_result_documents].
        """  # noqa: E501
        return stac_extract_result_documents(raw)

    def read_facet(self, doc: dict[str, Any], api_field: str) -> str | None:
        """
        See [SearchAPI.read_facet][esmporium.search.apis.SearchAPI.read_facet].
        """
        return stac_read_facet_as_string(doc, api_field, describe_search_api(self))

    def read_facet_list(self, doc: dict[str, Any], api_field: str) -> tuple[str, ...]:
        """
        See [SearchAPI.read_facet_list][esmporium.search.apis.SearchAPI.read_facet_list].
        """  # noqa: E501
        return stac_read_facet_list_as_strings(
            doc, api_field, describe_search_api(self)
        )
