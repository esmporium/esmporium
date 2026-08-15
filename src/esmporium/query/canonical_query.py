"""
The canonical representation of queries

All translations between specific queries
(see [esmporium.query.known_queries][])
go through this canonical representation
to keep maintenance manageable.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, TypeGuard, cast, overload

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    ValidationError,
    ValidatorFunctionWrapHandler,
    WrapValidator,
)

from esmporium.query.protocol import SourceQuery

CANONICAL_FACETS: frozenset[str] = frozenset(
    {
        "project",
        "model",
        "institution",
        "experiment",
        "variable",
        "variant_label",
        "reporting_interval",
        "processing_id",
        "activity",
        "resolution",
        "grid_label",
        "realm",
    }
)
"""
The canonical facet vocabulary

Aligns as far as possible with [Dataset][esmporium.db.schema.Dataset].

Query class-specific facets are deliberately not here.
"""


class NotACanonicalFacetError(ValueError):
    """Raised when a facet is declared as equivalent to a facet we do not have."""

    def __init__(self, canonical_equivalent: str) -> None:
        """
        Initialise the error

        Parameters
        ----------
        canonical_equivalent
            The claimed canonical facet, which is not one of
            [CANONICAL_FACETS][esmporium.query.canonical_query.CANONICAL_FACETS]
        """
        self.canonical_equivalent = canonical_equivalent
        supported = ", ".join(sorted(CANONICAL_FACETS))
        super().__init__(
            f"{canonical_equivalent!r} is not a canonical facet; we have: {supported}"
        )


class NotFacetValuesError(ValueError):
    """
    Raised when a facet's value is neither a string nor a collection of strings.

    Raised in place of pydantic's own error so that the failure is reported
    against the facet rather than against a position inside it,
    which may not make sense given the user's input.
    """

    def __init__(self) -> None:
        """Initialise the error"""
        super().__init__("Input should be a string or a collection of strings")


@dataclass(frozen=True)
class QueryFacet:
    """
    Marks a field of a query class as a facet, and says how it translates

    This is intended to be written into the field's type with `Annotated`

    ```python
    class QueryExample(BaseModel):
        # Same name in the canonical vocabulary
        model: Annotated[FacetValues, QueryFacet("model")] = ()

        # A different name in the canonical vocabulary
        ensemble: Annotated[FacetValues, QueryFacet("variant_label")] = ()

        # No canonical equivalent: i.e. this facet is specific to this query class
        product: Annotated[FacetValues, QueryFacet(None)] = ()
    ```
    """

    canonical_equivalent: str | None
    """
    This facet's name in the canonical vocabulary

    Must be one of
    [CANONICAL_FACETS][esmporium.query.canonical_query.CANONICAL_FACETS],
    or `None` if the facet is specific to this query class.
    """

    def __post_init__(self) -> None:
        """
        Check the declared canonical equivalent is a facet we actually have

        Raises
        ------
        NotACanonicalFacetError
            `canonical_equivalent` is neither `None` nor a canonical facet
        """
        if (
            self.canonical_equivalent is not None
            and self.canonical_equivalent not in CANONICAL_FACETS
        ):
            raise NotACanonicalFacetError(self.canonical_equivalent)


def is_collection_of_facet_values(value: object) -> TypeGuard[Collection[Any]]:
    """
    Determine whether a value is a collection of facet values

    Mappings, bytes and strings are all collections,
    but none of them is a collection *of facet values*.
    We have to handle this carefully.

    Parameters
    ----------
    value
        Value to check

    Returns
    -------
    :
        `True` if `value` is a collection which holds one facet value per item
    """
    return isinstance(value, Collection) and not isinstance(
        value, (Mapping, bytes, str)
    )


@overload
def normalise_facet_values(value: None) -> tuple[()]: ...


@overload
def normalise_facet_values(value: str) -> tuple[str]: ...


@overload
def normalise_facet_values(value: Collection[str]) -> tuple[str, ...]: ...


@overload
def normalise_facet_values(value: object) -> tuple[Any, ...]: ...


# Note that the implementation's return type is deliberately looser than the overloads':
# the `object` overload exists for the pydantic before-validators,
# which hand us `object` and rely on us passing anything we cannot normalise
# straight through, so that pydantic reports the type error itself.
def normalise_facet_values(value: object) -> tuple[Any, ...]:
    """
    Normalise facet values

    Parameters
    ----------
    value
        Value to normalise

    Returns
    -------
    :
        `value` converted to a tuple if the input is a string or a collection

        Otherwise, returns a one-tuple of `value`,
        on the assumption that this is used in a context where pydantic
        provides the correct type error message.
    """
    if value is None:
        return ()

    if isinstance(value, str):
        return (value,)

    if is_collection_of_facet_values(value):
        return tuple(value)

    return (value,)


def validate_facet_values(
    value: object, handler: ValidatorFunctionWrapHandler
) -> tuple[str, ...]:
    """
    Normalise and validate facet values, reporting failures where the user can see them

    Normalisation wraps a lone value in a one-tuple, which would otherwise have
    pydantic report the failure at index 0 of a tuple the user never typed.
    Here, only a value which really was a collection is reported per item;
    anything else is reported against the facet as a whole.

    Parameters
    ----------
    value
        Value to normalise and validate

    handler
        Pydantic's validator for the field's declared type

    Returns
    -------
    :
        `value`, normalised and validated

    Raises
    ------
    NotFacetValuesError
        `value` is neither a string nor a collection of strings
    """
    try:
        # `handler` is untyped, but it validates against the field's declared type
        return cast(tuple[str, ...], handler(normalise_facet_values(value)))
    except ValidationError:
        if is_collection_of_facet_values(value):
            # The user did give us a collection,
            # so the index in pydantic's error is a real position in their input.
            raise

        raise NotFacetValuesError from None


def normalise_facet_values_by_name(value: object) -> object:
    """
    Normalise a mapping of facet name -> facet values

    Only the mapping itself is handled here.
    Each facet's values are normalised (and reported on, if they are wrong)
    by [FacetValues][esmporium.query.canonical_query.FacetValues],
    which is the mapping's value type.

    Parameters
    ----------
    value
        Value to normalise

    Returns
    -------
    :
        `None` read as "no facets", i.e. an empty mapping.

        Anything else untouched, so that pydantic can handle it.
    """
    if value is None:
        return {}

    return value


FacetValues = Annotated[tuple[str, ...], WrapValidator(validate_facet_values)]
"""
The values of a single facet
"""

FacetValuesByName = Annotated[
    dict[str, FacetValues], BeforeValidator(normalise_facet_values_by_name)
]
"""
Facet name -> that facet's values

Used for the facet buckets we hold by name rather than as declared fields.
"""


class QueryCanonical(BaseModel):
    """
    An immutable query expressed in the canonical facet vocabulary
    """

    # Immutable to provide stability.
    # Extras are forbidden because the buckets below are where
    # all facet values go; anything else is a typo we should not quietly drop.
    model_config = ConfigDict(frozen=True, extra="forbid")

    project: FacetValues = ()
    """See [`Dataset.project`][esmporium.db.schema.Dataset.project]"""

    model: FacetValues = ()
    """See [`Dataset.model`][esmporium.db.schema.Dataset.model]"""

    institution: FacetValues = ()
    """See [`Dataset.institution`][esmporium.db.schema.Dataset.institution]"""

    experiment: FacetValues = ()
    """See [`Dataset.experiment`][esmporium.db.schema.Dataset.experiment]"""

    variable: FacetValues = ()
    """See [`Dataset.variable`][esmporium.db.schema.Dataset.variable]"""

    variant_label: FacetValues = ()
    """See [`Dataset.variant_label`][esmporium.db.schema.Dataset.variant_label]"""

    reporting_interval: FacetValues = ()
    """See [`Dataset.reporting_interval`][esmporium.db.schema.Dataset.reporting_interval]"""  # noqa: E501

    processing_id: FacetValues = ()
    """See [`Dataset.processing_id`][esmporium.db.schema.Dataset.processing_id]"""

    activity: FacetValues = ()
    """
    The specific model intercomparison project (MIP) to search for.

    Known as `activity_id` in CMIP6 and CMIP7 (no concept for CMIP5).

    For example: CMIP, ScenarioMIP, DAMIP, PMIP.
    """

    resolution: FacetValues = ()
    """
    Approximate horizontal grid cell sizing.

    Known as `nominal resolution` for CMIP6 and CMIP7 (no concept for CMIP5).

    For example: 1km, 250km, 500km.
    """

    grid_label: FacetValues = ()
    """See [`grid_label`][esmporium.db.schema.Dataset.grid_label]."""

    realm: FacetValues = ()
    """
    Realm most closely associated with a variable.

    For example: atmos, ocean, land.
    """

    query_specific_facets: FacetValuesByName = {}
    """
    Facets which this query names but the canonical vocabulary does not.

    These are held under their native names,
    untranslated, because there is nothing to translate them to.
    For example, CMIP5's `product`.
    """

    other_terms: FacetValuesByName = {}
    """
    Facets we do not model at all, passed through as the user gave them.
    """

    source_query: SourceQuery = None
    """
    Query from which this query was created

    Useful for debugging the results of translations.
    """
