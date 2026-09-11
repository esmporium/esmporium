"""
Parsing of search results into the pieces we store

A result parser is the result-reading counterpart of the facade parameters
([esmporium.search.search_api_facade.parameters][]).
The parameters know which API field carries each facet when we ask a project's question,
the result parser knows how to read the answer.
They are generally built for a specific project - search API pairing,
because the parsing can vary with project and search API.
The result parsers are used by facades and keeps this coupling in the open.
Defining specific result parsers
also leaves the search API classes speaking only for their response format
(rather than having to handle project specific quirks).
"""

from esmporium.search.search_api_facade.result_parsers.known_result_parsers import (
    ESGFNGCMIP6ResultParser,
    ESGFNGCMIP7ResultParser,
    MissingResultFieldError,
    SolrSingleRowResultParser,
    SolrVariableBundleResultParser,
    stac_east_n_matches,
    stac_west_n_matches,
)
from esmporium.search.search_api_facade.result_parsers.protocol import (
    NMatchesReader,
    ResultParserProtocol,
    get_single_value_columns_from_doc,
)

__all__ = [
    "ESGFNGCMIP6ResultParser",
    "ESGFNGCMIP7ResultParser",
    "MissingResultFieldError",
    "NMatchesReader",
    "ResultParserProtocol",
    "SolrSingleRowResultParser",
    "SolrVariableBundleResultParser",
    "get_single_value_columns_from_doc",
    "stac_east_n_matches",
    "stac_west_n_matches",
]
