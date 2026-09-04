"""
Turning a raw ESGF search document into the pieces we store

Each document (a Solr dataset record or a STAC feature) is parsed into a
[`ParsedDoc`][esmporium.db.parse.ParsedDoc]: the bundle it belongs to, the variable(s)
it covers, the common facets, the edition, and where it lives. Ingestion
([`esmporium.db.results_to_database`][]) turns that into rows.

The key CMIP5 point: a dataset-level Solr record carries **every** variable in its
`variable` list, so we read them all straight from here and never go down to the file
layer. One record therefore yields one `Dataset` per variable, all sharing one edition.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

#: In a CMIP5 DRS id (`cmip5.<product>.<institute>.<model>...`) the model is the 4th
#: token. We take the model from here, not the `model` facet, because that facet can be
#: a display name with dots/parens (e.g. `BCC-CSM1.1(m)`) that break dot-joined ids.
DRS_MODEL_INDEX = 3

_VERSION_TOKEN = re.compile(r"^v\d+$")


def one(value: Any) -> Any:
    """Unwrap a one-element list, e.g. `['CMIP5']` -> `'CMIP5'`, else leave as-is."""
    if isinstance(value, list) and len(value) == 1:
        return value[0]
    return value


@dataclass(frozen=True)
class NodeInfo:
    """Where one edition is hosted."""

    data_node: str
    index_node: str | None
    replica: bool


@dataclass(frozen=True)
class ParsedDoc:
    """One raw search document, reduced to the pieces we store."""

    id_project_specific: str
    """The bundle's native id (Solr `master_id`, or the version-free STAC id)."""

    variables: tuple[str, ...]
    """Every variable the document covers (a list for CMIP5, one for CMIP6/7)."""

    common_facets: dict[str, str | None]
    """The non-variable `Dataset` facets, shared by every variable."""

    version: str
    is_latest: bool
    retracted: bool
    nodes: tuple[NodeInfo, ...]
    esgf_doc_id: str
    raw_json: str

    def dataset_facets(self) -> list[dict[str, str | None]]:
        """Return the full `Dataset` kwargs for each variable in this document."""
        return [
            {
                **self.common_facets,
                "variable": variable,
                "id_project_specific": self.id_project_specific,
            }
            for variable in self.variables
        ]


def parse_document(raw_doc: dict[str, Any]) -> ParsedDoc:
    """Parse one raw document, dispatching on its generation.

    Parameters
    ----------
    raw_doc
        A Solr dataset record (`response.docs[i]`) or a STAC feature
        (`features[i]`), already parsed from JSON.

    Returns
    -------
    :
        The document reduced to what we store.
    """
    if "properties" in raw_doc:  # STAC features nest their facets; Solr does not
        return _parse_stac(raw_doc)
    return _parse_solr(raw_doc)


def _parse_solr(doc: dict[str, Any]) -> ParsedDoc:
    project = one(doc["project"])
    instance_id = one(doc["instance_id"])
    node = NodeInfo(
        data_node=one(doc["data_node"]),
        index_node=one(doc.get("index_node")),
        replica=bool(one(doc.get("replica", False))),
    )

    if project == "CMIP5":
        common: dict[str, str | None] = {
            "project": project,
            "model": instance_id.split(".")[DRS_MODEL_INDEX],
            "institution": one(doc["institute"]),
            "experiment": one(doc["experiment"]),
            "variant_label": one(doc["ensemble"]),
            "reporting_interval": one(doc["time_frequency"]),
            "grid_label": None,  # CMIP5 has no grid label
            "processing_id": one(doc["cmor_table"]),
        }
        variables = tuple(doc.get("variable", []))  # the whole bundle
    else:  # CMIP6-style Solr
        common = {
            "project": project,
            "model": one(doc["source_id"]),
            "institution": one(doc["institution_id"]),
            "experiment": one(doc["experiment_id"]),
            "variant_label": one(doc["variant_label"]),
            "reporting_interval": one(doc["frequency"]),
            "grid_label": one(doc["grid_label"]),
            "processing_id": one(doc["table_id"]),
        }
        variables = (one(doc["variable_id"]),)

    return ParsedDoc(
        id_project_specific=one(doc["master_id"]),
        variables=variables,
        common_facets=common,
        version=str(one(doc["version"])),
        is_latest=bool(one(doc.get("latest", False))),
        retracted=bool(one(doc.get("retracted", False))),
        nodes=(node,),
        esgf_doc_id=one(doc["id"]),
        raw_json=json.dumps(doc),
    )


def _parse_stac(feature: dict[str, Any]) -> ParsedDoc:
    props: dict[str, Any] = feature["properties"]
    prefix = "cmip7" if any(k.startswith("cmip7:") for k in props) else "cmip6"

    def facet(name: str) -> Any:
        return props.get(f"{prefix}:{name}")

    feature_id = feature["id"]
    processing = (
        facet("variable_branding_suffix") if prefix == "cmip7" else facet("table_id")
    )
    common: dict[str, str | None] = {
        "project": facet("mip_era") or prefix.upper(),
        "model": facet("source_id"),
        "institution": facet("institution_id"),
        "experiment": facet("experiment_id"),
        "variant_label": facet("variant_label"),
        "reporting_interval": facet("frequency"),
        "grid_label": facet("grid_label"),
        "processing_id": processing,
    }

    return ParsedDoc(
        id_project_specific=_strip_version(feature_id),
        variables=(facet("variable_id"),),
        common_facets=common,
        version=str(props.get("version") or _version_from_id(feature_id)),
        is_latest=bool(props.get("latest", False)),
        retracted=bool(props.get("retracted", False)),
        nodes=_stac_nodes(feature),
        esgf_doc_id=feature_id,
        raw_json=json.dumps(feature),
    )


def _strip_version(native_id: str) -> str:
    """Drop a trailing `.vYYYYMMDD` token so different editions share a bundle id."""
    parts = native_id.split(".")
    if parts and _VERSION_TOKEN.match(parts[-1]):
        return ".".join(parts[:-1])
    return native_id


def _version_from_id(native_id: str) -> str:
    """Read the version out of a `.vYYYYMMDD` token, if present."""
    parts = native_id.split(".")
    if parts and _VERSION_TOKEN.match(parts[-1]):
        return parts[-1][1:]
    return ""


def _stac_nodes(feature: dict[str, Any]) -> tuple[NodeInfo, ...]:
    """Return the distinct data nodes a STAC feature's assets are hosted on."""
    hosts: list[str] = []
    for asset in feature.get("assets", {}).values():
        href = asset.get("href")
        host = urlparse(href).hostname if href else None
        if host and host not in hosts:
            hosts.append(host)
    return tuple(NodeInfo(host, None, False) for host in hosts)


def data_node_from_esgf_doc_id(esgf_doc_id: str) -> str | None:
    """Recover the data node from a Solr `esgf_doc_id` (`<instance_id>|<data_node>`).

    Returns `None` for STAC ids, which have no `|` separator.
    """
    if "|" not in esgf_doc_id:
        return None
    return esgf_doc_id.rsplit("|", 1)[-1]
