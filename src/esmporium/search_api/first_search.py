# ruff: noqa  # design-sketch pseudocode, not a real module — skip linting
# from collections.abc import Callable, Sequence
# from typing import Any, Protocol

# from esmporium.query.protocol import QueryProtocol


# class SearchClient(Protocol):
#     """
#     Search client interface
#     """

#     # define the interface here
#     # I imagine this will be one included attribute
#     search_url: str
#     # Maybe better named just url or base_url or api,
#     # up to you and claude to work through

#     retry_strategy: Any  # not sure what type this should be
#     # This would hold the retry strategy for this client.
#     # This is what would allow users to tie different retry strategies
#     # to different instances
#     # (e.g. you could have two clients for ESGF west,
#     # one that retried 20 times and one that only retried twice:
#     # I don't know if this would be useful,
#     # but I think this flexibility makes sense to keep.)

#     # If we ever needed it, authorisation handling would live here,
#     # on the client too (but I don't think we need that, fortunately)

#     # I imagine something like this wil also be needed
#     def search(self, query: QueryProtocol) -> dict[str, Any]:
#         """
#         Search for a given query
#         """
#         # Return the raw JSON (or even the raw response might make more sense,

#         # not sure - see what you and claude think)


# ClientSelector = Callable[[QueryProtocol, int], SearchClient]


# def default_client_selector(query: QueryProtocol, attempt: int) -> SearchClient | None:
#     if query.project == ("CMIP7",):
#         clients = [
#             # give back ESGF West first, then ESGF east, then fail
#         ]

#     elif query.project in (("CMIP5",), ("CMIP6",)):
#         clients = [
#             # use something else we like first, ORNL?
#             # then go through other known ESGF1 end points
#             # then ESGF east (seems to be migrating CMIP6 faster)
#             # then ESGF west
#         ]

#     else:
#         clients = [
#             # blend of project searches, start with ESGF west
#             # then ESGF east
#             # use something else we like first, ORNL?
#             # then go through other known ESGF1 end points.
#             # Other option would be to raise here...
#         ]

#     if attempt < len(clients):
#         return clients[attempt]

#     return None


# def get_list_based_client_selector(clients: Sequence[SearchClient]) -> ClientSelector:
#     def get_client(query: QueryProtocol, attempt: int) -> SearchClient:
#         if attempt < len(clients):
#             return clients[attempt]

#         return None

#     return get_client


# # we could then also offer other pre-built selectors too if we want
# # (just means we have to test and maintain them too, which is non-zero)
# # e.g. select_from_esgf1_nodes
# # and when we have our health database,
# # select_based_on_client_health,
# # that updates which node to select based on health.


# # Note: I have already started guessing about the right abstraction here.
# # The function you will probably want to start with is below.
# def search(
#     query: QueryProtocol,
#     # Name of ClientSelector could be better, maybe?
#     # Up to you
#     client_selector: ClientSelector = default_client_selector,
# ) -> dict[str, Any]:
#     # return raw JSON for now
#     pass


# # A plain 'no flexibility' function.
# # We want to work from this towards our abstracted form,
# # which might end up something like the above,
# # or might end up being completely different.
# def search_cmip5_esgf1(
#     query: QueryProtocol,
# ) -> dict[str, Any]:
#     # convert the query to canonical

#     # convert the canonical query to ESGF1 CMIP5 search parameters
#     # (I don't love that the conversion depends is not a function of the
#     # end point we're hitting (i.e. client or client 'style')
#     # but also other stuff
#     # (at the moment the project, but it could be anything in theory)).
#     # I'm not sure how best to handle this 2D conversion space.
#     # Maybe claude has a suggestion.
#     # If not, I would either put methods like the following on SearchClient:
#     #
#     # 1. `to_cmip5_style_parameters`
#     # 1. `to_cmip6_style_parameters`
#     # 1. `to_cmip7_style_parameters`
#     #
#     # or we just have to create a bunch of functions:
#     #
#     # 1. `convert_query_to_esgf1_cmip5_search_parameters`
#     # 1. `convert_query_to_esgf1_cmip6_search_parameters`
#     # 1. `convert_query_to_esgf1_cmip7_search_parameters`
#     # 1. `convert_query_to_esgfng_cmip5_search_parameters`
#     # 1. `convert_query_to_esgfng_cmip6_search_parameters`
#     # 1. `convert_query_to_esgfng_cmip7_search_parameters`

#     # hard-code our list of clients to try in here initially

#     # go over the list of clients,
#     # giving them the search parameters
#     # and trying them with their own retry strategy first,
#     # then trying each client until one works

#     # return raw JSON for now
#     pass
