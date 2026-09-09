"""
Parsing of search result documents into dataset rows

A doc parser is the result-reading counterpart of the facade parameters
([esmporium.search.search_api_facade.parameters][]): the parameters know which API field
carries each facet for a project, the doc parser knows how many dataset rows a document
maps to for a project and format (a CMIP5 Solr document bundles many variables; every
other combination is one row). Pairing a facade with its doc parser keeps that
document-shape decision in the open, rather than every parser behaving as though any
document might bundle variables.
"""

from esmporium.search.search_api_facade.doc_parsing.known_doc_parsers import (
    INBUILT_DOC_PARSER_STORE,
    SINGLE_ROW_DOC_PARSER,
    VARIABLE_BUNDLE_DOC_PARSER,
    DocParserClassification,
    DocParserStore,
    NoDocParserError,
    SingleRowDocParser,
    VariableBundleDocParser,
)
from esmporium.search.search_api_facade.doc_parsing.protocol import (
    DocParserProtocol,
    get_single_value_columns_from_doc,
)

__all__ = [
    "INBUILT_DOC_PARSER_STORE",
    "SINGLE_ROW_DOC_PARSER",
    "VARIABLE_BUNDLE_DOC_PARSER",
    "DocParserClassification",
    "DocParserProtocol",
    "DocParserStore",
    "NoDocParserError",
    "SingleRowDocParser",
    "VariableBundleDocParser",
    "get_single_value_columns_from_doc",
]
