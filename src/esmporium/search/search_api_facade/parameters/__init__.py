"""
Parameter parsing support for the search API facades
"""
# Developer note:
#
# We considered just using the query classes directly for these specifications.
# We rejected this on three grounds:
#
# 1. it creates a coupling between queries and how we pass to the API.
#    We don't want such a coupling.
#    If the API changes in future, that shouldn't change how queries are formed.
# 3. ESGF-NG has this idea of a prefix, which isn't nicely expressed anywhere.
#
# This does lead to quite a lot of duplication.
# We are ok with this because of the decoupling it introduces.
# There should also be relatively little churn in this part of the code,
# making the issue even smaller.

__all__ = []
