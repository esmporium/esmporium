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
    PrefixMappingFacadeParameters,
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
    "FacadeParametersProtocol",
    "PrefixMappingFacadeParameters",
]
