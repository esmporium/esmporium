"""
Parameter parsing support for the search API facades
"""

from esmporium.search.search_api_facade.parameters.known_facade_parameters import (
    ESGF1_CMIP5_FACADE_PARAMETERS,
    ESGF1_CMIP6_FACADE_PARAMETERS,
    ESGF1_CMIP7_FACADE_PARAMETERS,
    ESGFNG_CMIP5_FACADE_PARAMETERS,
    ESGFNG_CMIP6_FACADE_PARAMETERS,
    ESGFNG_CMIP7_FACADE_PARAMETERS,
    DirectMappingFacadeParameters,
    ESGF1CMIP5ParametersQueryStyle,
    ESGF1CMIP6ParametersQueryStyle,
    ESGF1CMIP7ParametersQueryStyle,
    ESGFNGCMIP5ParametersQueryStyle,
    ESGFNGCMIP6ParametersQueryStyle,
    ESGFNGCMIP7ParametersQueryStyle,
    PrefixMappingFacadeParameters,
    get_mapping_to_query_style_facet_names,
)
from esmporium.search.search_api_facade.parameters.protocol import (
    FacadeParametersProtocol,
)

__all__ = [
    "ESGF1_CMIP5_FACADE_PARAMETERS",
    "ESGF1_CMIP6_FACADE_PARAMETERS",
    "ESGF1_CMIP7_FACADE_PARAMETERS",
    "ESGFNG_CMIP5_FACADE_PARAMETERS",
    "ESGFNG_CMIP6_FACADE_PARAMETERS",
    "ESGFNG_CMIP7_FACADE_PARAMETERS",
    "DirectMappingFacadeParameters",
    "ESGF1CMIP5ParametersQueryStyle",
    "ESGF1CMIP6ParametersQueryStyle",
    "ESGF1CMIP7ParametersQueryStyle",
    "ESGFNGCMIP5ParametersQueryStyle",
    "ESGFNGCMIP6ParametersQueryStyle",
    "ESGFNGCMIP7ParametersQueryStyle",
    "FacadeParametersProtocol",
    "PrefixMappingFacadeParameters",
    "get_mapping_to_query_style_facet_names",
]
