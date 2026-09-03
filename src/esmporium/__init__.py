"""
Unified data retrieval from ESGF and access to datasets

## Naming

A facet (the thing you filter on, e.g. the variable of a dataset)
goes by a different name at each layer,
so we use a specific term for each:

- **canonical name**: the facet's name in
  [QueryCanonical][esmporium.query.QueryCanonical], e.g. `variable`.
  Everything translates through this.
- **query style**: a class which names facets the way one project does
  for one family of APIs, e.g.
  [ESGF1CMIP6ParametersQueryStyle][esmporium.search.ESGF1CMIP6ParametersQueryStyle].
  [QueryCMIP5][esmporium.query.QueryCMIP5] and its siblings are query styles too:
  they are the styles we expect users to write queries in.
- **query style parameter name**: what a query style calls a facet,
  e.g. `variable_id`.
  [get_mapping_to_query_style_facet_names][esmporium.search.get_mapping_to_query_style_facet_names]
  returns these.
- **API parameter name**: the name which is actually used by the search API,
  e.g. `cmip6:variable_id`.
  This is the query style parameter name plus whatever the API family adds on top
  (ESGF-NG's collection prefix, in that example).
  [FacadeParametersProtocol.get_mapping_to_api_facet_names][esmporium.search.search_api_facade.parameters.protocol.FacadeParametersProtocol.get_mapping_to_api_facet_names]
  returns these.
- **facade parameters**: an object pairing a query style with the rules for
  turning its parameter names into API parameter names, e.g.
  [ESGF1_CMIP6_FACADE_PARAMETERS][esmporium.search.ESGF1_CMIP6_FACADE_PARAMETERS].

The last two are the ones worth keeping apart.
For ESGF1 and the ESGF1.5 bridge they happen to be identical.
For ESGF-NG they are not, and treating them as one thing is how you end up
building a request which cannot match anything.

Separately, "vocabulary" always means a controlled vocabulary of facet *values*
(the set of experiment names a project allows, say),
never a set of facet names.
A set of facet names is a query style, and the names in it are parameter names.
"""

# TODO: devs - this naming section is here so there is one place
# which defines these terms while the package is being built out.
# It belongs in the docs proper (a glossary page, cross-referenced
# from the search and query docs), so move it there when we write them.

import importlib.metadata

__version__ = importlib.metadata.version("esmporium")
