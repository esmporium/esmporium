"""
Query protocol class
"""

from __future__ import annotations

from typing import Annotated, Protocol

from pydantic import PlainValidator


class QueryProtocol(Protocol):
    """
    A query we support
    """

    other_terms: dict[str, tuple[str, ...]]
    """Facets that are not modelled and should be passed through untranslated"""

    source_query: SourceQuery
    """
    Source from which this query was created

    Useful for debugging.
    """

    def __init__(self, **facets: object) -> None:
        """
        Initialise, given facet values keyed by the name used with queries of this kind

        `other_terms` and `source_query` are also passed the same way.
        """
        ...

    # @Claude, given the implementation of this is always the same,
    # can we just get rid of this and replace with facet_values_from_attributes
    # everywhere? Backwards compatibility isn't an issue
    def facet_values(self) -> dict[str, tuple[str, ...]]:
        """
        Get the facets which are set, keyed by this query's name

        Facets which are not set are left out,
        so that "not asked for" and "asked for nothing" cannot be confused.
        `other_terms` is not a facet, so it is not included.
        """
        ...


def accept_without_validation(value: object) -> object:
    """
    Hand a value straight back, unvalidated

    Parameters
    ----------
    value
        Value to pass through

    Returns
    -------
    :
        `value`, untouched
    """
    return value


SourceQuery = Annotated[QueryProtocol | None, PlainValidator(accept_without_validation)]
"""
The type of a query's `source_query` field

The annotation says what we expect and is what a type checker enforces.
The validator switches pydantic's checking off for this one field:
a `Protocol` is not something pydantic can validate against,
and the alternatives
(`arbitrary_types_allowed` plus a `runtime_checkable` protocol)
would buy only a shallow shape check
at the cost of loosening validation across the whole model.

`source_query` exists for debugging, so being strict about it at runtime
earns little.
"""
