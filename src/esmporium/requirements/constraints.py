"""
Constraints: judgements about the datasets a requirement selected

A query selects datasets by matching facets, which answers "is there one?". A
constraint answers the questions a facet cannot: whether the control a simulation
branched from covers the years being analysed, whether there is enough of a run to
regress against. Those are judgements about what is *in* the data rather than about
which dataset it is.

[Constraint][(m).Constraint] is the extension point. A constraint is attached to a
[Leaf][esmporium.requirements.Leaf] or to a
[Requirement][esmporium.requirements.Requirement], and **where it is attached decides
what its failure costs**:

- on a leaf, it sees that dataset and the chain behind it, and a failure makes that
  leaf unsatisfied;
- on the requirement, it sees every role in the group, and a failure fails the group.

It is generic in the catalogue it needs. One which needs no more than the
[Catalogue][esmporium.requirements.Catalogue] protocol provides is a
`Constraint[Catalogue]`; one which needs more defines a protocol beside itself and is
a constraint over that instead, as [Covers][esmporium.requirements.Covers] does with
`CatalogueWithTimeInfo`.

A constraint which cannot answer says so in one of three ways:

- [NeedsEnrichment][(m).NeedsEnrichment] -- the fact is not recorded yet. It carries
  [EnrichmentInput][(m).EnrichmentInput]s, which name what to hand the enricher.
- [MissingFeature][(m).MissingFeature] -- the question is a fair one to ask and we
  have not built it, so the message carries the issue tracker.
- [ConstraintNotEvaluableError][(m).ConstraintNotEvaluableError] -- raised rather
  than returned, and reported as unsatisfied with its message in the explanation.
  The narrow escape hatch: **anything else a constraint raises ends the solve**.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Any, Protocol, TypeVar, cast, runtime_checkable

from pydantic import BaseModel, PlainSerializer, PlainValidator

from esmporium.query import QueryCanonical
from esmporium.requirements.catalogue import Catalogue, CatalogueEntry
from esmporium.requirements.relations import Lineage

ISSUE_TRACKER_URL = "https://github.com/esmporium/esmporium/issues"
"""
Where to report a requirement we do not support yet

Named here because [MissingFeature][(m).MissingFeature] is the outcome which carries
it, and a link which is right in one message and stale in another is worse than no
link.
"""

CatalogueT_contra = TypeVar("CatalogueT_contra", bound=Catalogue, contravariant=True)
"""
The catalogue a constraint needs

Contravariant because a constraint which needs less can be used wherever
one which needs more can:
a `Constraint[Catalogue]` can be used with any catalogue,
so it is also a `Constraint[CatalogueWithTimeInfo]`.
"""

CONSTRAINT_TYPE_KEY = "type"
"""
Key under which a serialised constraint carries the class to rebuild it as

See [Constraints][(m).Constraints]. Named here because both halves of the round trip
have to agree on it, and because it is the one key of a serialised constraint which is
not one of its own fields.
"""


@dataclass(frozen=True)
class Pass:
    """
    The constraint is met
    """

    message: str = ""
    """
    Details, if there are any worth recording

    Optional, unlike the other two outcomes': something which passed usually has
    nothing to say.
    """


@dataclass(frozen=True)
class Degraded:
    """
    The constraint is met, but not as well as it could be

    The group stays satisfied, because the analysis can be run. The message is kept, as
    a note on the solution and a line in the explanation, so that "run it, and say in
    the write-up that the baseline was shorter than we wanted" is a thing the caller can
    decide.
    """

    message: str
    """What is not ideal about it"""


@dataclass(frozen=True)
class Fail:
    """
    The constraint is not met
    """

    message: str
    """Why it is not met"""


class MissingTimeFact(str, Enum):
    """
    Which piece of time information is not recorded yet

    What an [EnrichmentInput][(m).EnrichmentInput] names, because it decides how much
    work clearing it takes: a range comes off the filenames and is free, a calendar
    needs a file header fetched.
    """

    TIME_RANGE = "time_range"
    """When the data is -- its start and end. Read from the filenames."""

    BRANCH_POINT = "branch_point"
    """
    Where the run branched from its parent

    Published by the child for CMIP6 and CMIP7; a CMIP5 branch time needs a fix before
    it can be interpreted at all.
    """

    CALENDAR = "calendar"
    """
    Which CF calendar the numbers are in, and what they count from

    Only daily and sub-daily data needs this: twelve months is a year in every
    calendar, so a monthly or yearly range is answerable without it. Read from a file
    header.
    """


@dataclass(frozen=True)
class EnrichmentInput:
    """
    One dataset, and one fact about it which has to be recorded

    What [SolveResult.enrichment_inputs][esmporium.requirements.SolveResult] hands
    back, shaped to pass straight to the enricher rather than to be read and retyped.
    Frozen and hashable so that the same dataset asked for by two groups deduplicates
    to one.
    """

    dataset_id: int
    """
    The dataset, as [CatalogueEntry.id][esmporium.requirements.CatalogueEntry.id]

    A `Dataset` row's id. Which *version* of it to measure is the enricher's question,
    answered the same way the rest of the solve answers it -- through the describing
    version -- rather than something a constraint is in a position to say.
    """

    missing: MissingTimeFact
    """Which fact is not recorded"""


@dataclass(frozen=True)
class NeedsEnrichment:
    """
    The constraint could be evaluated, once somebody records the facts it needs
    """

    message: str
    """What is not recorded, and what recording it would take"""

    inputs: tuple[EnrichmentInput, ...] = ()
    """
    What to hand the enricher to clear this

    Empty is allowed and means only that this outcome could not name the datasets, not
    that there is nothing to do.
    """


@dataclass(frozen=True)
class MissingFeature:
    """
    The requirement is a fair thing to want and we have not built it

    The sibling of [NeedsEnrichment][(m).NeedsEnrichment], and the pair is worth
    keeping apart: that one says "go and fetch it", this one says "nothing you can do
    will help, please tell us you wanted it". So the message carries
    [ISSUE_TRACKER_URL][(m).ISSUE_TRACKER_URL].
    """

    message: str
    """What is not supported, and where to say you wanted it"""


Outcome = Pass | Degraded | Fail | NeedsEnrichment | MissingFeature
"""
What a constraint answers
"""


class ConstraintNotEvaluableError(Exception):
    """
    Raised by a constraint which cannot answer, for a reason which is not a bug

    The narrow escape hatch for a constraint whose "I cannot answer" is neither
    [NeedsEnrichment][(m).NeedsEnrichment] nor
    [MissingFeature][(m).MissingFeature]. The solver catches it and reports the part of
    the requirement the constraint was attached to as unsatisfied, with the message in
    the explanation.
    """


@dataclass(frozen=True)
class ConstraintView:
    """
    What a constraint can see of the datasets chosen so far

    Named for who it is for rather than for what it holds,
    because what it holds depends on where the constraint was attached:
    a leaf's check sees that leaf's roles,
    the requirement's sees every role in the group.
    "What this constraint can see" is the one description which is true in both places.

    Role names given to and returned by this view are relative to
    [prefix][(m).ConstraintView.prefix].
    """

    prefix: str
    """
    Prefix of the part of the tree being checked

    `""` when the whole group is in view. A constraint reads roles relative to this, so
    a check written as `role="control"` keeps working wherever it is attached.
    """

    roles: Mapping[str, tuple[CatalogueEntry, ...]]
    """
    Role path (relative to `prefix`) -> the datasets filling it

    Always a tuple, whatever the
    [cardinality][esmporium.requirements.Requirement.cardinality],
    so that a constraint has one shape to handle.
    """

    lineages: Mapping[str, tuple[Lineage, ...]]
    """
    Leaf role (relative to `prefix`) -> the chain behind each dataset in that role

    A leaf which asked for no lineage is absent rather than present and empty,
    so a check which needs one can say so instead of quietly passing.
    The datasets in these chains are in `roles` too, under the paths the lineage names;
    this is the same datasets in the shape a check reads them,
    where the order of the chain and which end is which are what matter.
    """


class Constraint(Protocol[CatalogueT_contra]):
    """
    A judgement about the datasets a requirement selected

    The extension point for constraints:
    implement `evaluate` and the solver will run it,
    without knowing or caring what it is.
    A constraint must also be a pydantic model
    for it to be able to be stored with its requirement in an esmporium database
    -- see [Constraints][(m).Constraints].

    Generic in the catalogue the constraint needs:
    see the module's documentation.
    """

    def evaluate(self, view: ConstraintView, catalogue: CatalogueT_contra) -> Outcome:
        """
        Evaluate whether the constraint is met

        A constraint which cannot answer **returns** why
        -- [NeedsEnrichment][(m).NeedsEnrichment] if the facts are not recorded yet,
        [MissingFeature][(m).MissingFeature] if the question is one we have not built
        -- rather than raising. Raise
        [ConstraintNotEvaluableError][(m).ConstraintNotEvaluableError] for anything
        else which is not a bug; anything else raised ends the solve.

        Parameters
        ----------
        view
            The datasets chosen so far, and the chains behind them

        catalogue
            Catalogue, which can be asked for further information if needed

        Returns
        -------
        :
            The result of evaluating the constraint
        """
        ...


@runtime_checkable
class ChecksItsLeaf(Protocol):
    """
    A constraint which can refuse the leaf it is attached to, before any data

    Optional: implement `check_leaf` and the requirement calls it as it is built; do
    not, and nothing happens.

    For a check which cannot judge some datasets at all, this is the first of the two
    places to say so. A leaf which can *only* select those datasets is a mistake in
    the requirement, so it is refused here; a leaf which selects one among others is a
    fact about the data, so that candidate fails when it is solved.

    Examples
    --------
    A check which only means anything for data on a time axis, refusing a leaf which
    asks for fixed fields and nothing else:

    >>> from esmporium.query import Query
    >>> from esmporium.requirements.catalogue import set_facets
    >>> class NeedsATimeAxis:
    ...     def check_leaf(self, role, query):
    ...         if set_facets(query).get("reporting_interval") == ("fx",):
    ...             msg = f"{role!r} selects only fixed fields, which have no time axis"
    ...             raise ValueError(msg)
    ...
    ...     def evaluate(self, view, catalogue):
    ...         return Pass()

    >>> NeedsATimeAxis().check_leaf("areas", Query(reporting_interval="fx"))
    Traceback (most recent call last):
    ...
    ValueError: 'areas' selects only fixed fields, which have no time axis

    A leaf which could select them among others is left alone, to be judged when it is
    solved:

    >>> NeedsATimeAxis().check_leaf("tas", Query(variable="tas")) is None
    True
    """

    def check_leaf(self, role: str, query: QueryCanonical) -> None:
        """
        Refuse a leaf this constraint cannot judge

        Returns nothing: this is here to raise.

        Parameters
        ----------
        role
            Role of the leaf, to name in the error

        query
            The query the leaf will be solved with, i.e. including any facets the
            requirement adds. See
            [effective_query][esmporium.requirements.effective_query].

        Raises
        ------
        Exception
            The leaf can only select datasets this constraint cannot judge. The type is
            the constraint's own to choose, and should be named for the mistake.
        """
        ...


AnyConstraint = Constraint[Any]
"""
A constraint, whatever catalogue it needs

What a requirement holds,
since the constraints in one requirement can each need a different catalogue.
"""


class NotAConstraintError(TypeError):
    """
    Something attached as a constraint cannot act as one
    """

    def __init__(self, value: object, path: str = "") -> None:
        """
        Initialise the error

        Parameters
        ----------
        value
            The value which is not a constraint

        path
            The stored `"module:QualName"` it was loaded from, when it was loaded from
            one. Named in the message, because a path which resolves to the wrong thing
            is a different mistake from a bad value handed in directly, and the path is
            what has to be corrected.
        """
        self.value = value
        self.path = path
        if path:
            found = (
                "a class" if isinstance(value, type) else f"{type(value).__name__!r}"
            )
            super().__init__(
                f"{path!r} does not name a constraint: it resolves to {found}, which "
                "has no `evaluate(view, catalogue)` method to call. A stored "
                "constraint names the class to rebuild it as, so either the path is "
                "wrong or what it points at has changed."
            )

            return

        if isinstance(value, type):
            super().__init__(
                f"{value.__qualname__} is a class, not a constraint: attach an "
                "instance of it, so that whatever it checks against is on it. Its "
                "`evaluate` would otherwise be called with the view as `self`."
            )

            return

        # The one case where the value is fine and only its wrapping is wrong. Saying
        # "this is not a constraint" about a perfectly good constraint would send
        # whoever reads it looking in the wrong place entirely.
        if _acts_as_constraint(value):
            super().__init__(
                f"{type(value).__qualname__} is a constraint, and constraints are held "
                "as a sequence of them: pass `(one,)` or `[one]` rather than `one`. "
                "`with_constraints` takes them as separate arguments instead."
            )

            return

        super().__init__(
            f"{value!r} is not a constraint: a constraint is anything with an "
            "`evaluate(view, catalogue)` method, and this has none."
        )


class NotSerialisableConstraintError(TypeError):
    """
    A constraint cannot be written down, so its requirement cannot be stored
    """

    def __init__(self, constraint: object) -> None:
        """
        Initialise the error

        Parameters
        ----------
        constraint
            The constraint which cannot be serialised
        """
        self.constraint = constraint
        super().__init__(
            f"{type(constraint).__qualname__} is not a pydantic model, so a "
            "requirement using it cannot be written down. A requirement is stored as "
            "JSON and read back, and a constraint has to be part of that: make it a "
            "`pydantic.BaseModel` whose fields are what it checks against."
        )


class UnresolvableConstraintError(TypeError):
    """
    A stored constraint names a class which cannot be found
    """

    def __init__(self, path: str, cause: Exception) -> None:
        """
        Initialise the error

        Parameters
        ----------
        path
            The stored `"module:QualName"` which could not be resolved

        cause
            What went wrong importing or looking it up
        """
        self.path = path
        self.cause = cause
        super().__init__(
            f"Cannot load the constraint stored as {path!r}: {cause}. A stored "
            "requirement names the class to rebuild each of its constraints as, so a "
            "constraint which has since been renamed, moved or removed cannot be read "
            "back. Put the class back where it was, or rebuild the requirement and "
            "store it again."
        )


def check_constraint(value: object) -> AnyConstraint:
    """
    Check that something can act as a constraint, and give it back

    Duck-typed rather than `isinstance(value, Constraint)`, because a `Protocol` is only
    checkable at runtime when it says so, and this one does not.

    Parameters
    ----------
    value
        The candidate constraint

    Returns
    -------
    :
        `value`, unchanged

    Raises
    ------
    NotAConstraintError
        `value` has no callable `evaluate`

    Examples
    --------
    >>> class AtLeastOne:
    ...     def evaluate(self, view, catalogue):
    ...         return Pass()
    >>> check_constraint(AtLeastOne()).evaluate(None, None)
    Pass(message='')

    The class rather than an instance is refused, because the thing which does the
    checking is an instance carrying what it checks against:

    >>> try:
    ...     check_constraint(object())
    ... except NotAConstraintError as exc:
    ...     print(str(exc).split(":")[0])
    <object object at ...> is not a constraint
    """
    if _acts_as_constraint(value):
        return cast("AnyConstraint", value)

    raise NotAConstraintError(value)


def _acts_as_constraint(value: object) -> bool:
    """
    Determine whether something can be asked to evaluate itself

    Parameters
    ----------
    value
        The candidate constraint

    Returns
    -------
    :
        `True` if `value` has a callable `evaluate` and is not a class

        A class is excluded even though it passes the first half: its `evaluate` is an
        unbound function, so it is callable, and calling it would pass the view as
        `self`. A class also carries none of what it is meant to be checking against,
        which lives on an instance.
    """
    return not isinstance(value, type) and callable(getattr(value, "evaluate", None))


def _load_constraint(value: object) -> AnyConstraint:
    """
    Get a constraint back from a constraint or from its serialised form

    Parameters
    ----------
    value
        A constraint, or the mapping `_serialise_constraints` wrote for one

    Returns
    -------
    :
        The constraint

    Raises
    ------
    NotAConstraintError
        `value` is neither a constraint nor a serialised one
    """
    if _acts_as_constraint(value):
        return cast("AnyConstraint", value)

    if isinstance(value, Mapping) and CONSTRAINT_TYPE_KEY in value:
        serialised = cast("Mapping[str, Any]", value)
        path = str(serialised[CONSTRAINT_TYPE_KEY])
        target = _resolve(path)
        if not isinstance(target, type) or not issubclass(target, BaseModel):
            raise NotAConstraintError(target, path)

        loaded: object = target.model_validate(
            {k: v for k, v in serialised.items() if k != CONSTRAINT_TYPE_KEY}
        )
        if not _acts_as_constraint(loaded):
            raise NotAConstraintError(loaded, path)

        return cast("AnyConstraint", loaded)

    raise NotAConstraintError(value)


def _resolve(path: str) -> object:
    """
    Get whatever a stored `"module:QualName"` names

    Parameters
    ----------
    path
        The path, as [Constraints][(m).Constraints] writes it

    Returns
    -------
    :
        What it names

    Raises
    ------
    UnresolvableConstraintError
        The module cannot be imported, or it has no such attribute
    """
    module_name, _, qualname = path.partition(":")
    try:
        target: Any = importlib.import_module(module_name)
        for attribute in qualname.split("."):
            target = getattr(target, attribute)
    except (ImportError, AttributeError) as exc:
        raise UnresolvableConstraintError(path, exc) from exc

    return target


def _validate_constraints(value: object) -> tuple[AnyConstraint, ...]:
    """
    Get a tuple of constraints from whatever was given

    Parameters
    ----------
    value
        A sequence of constraints, or of their serialised forms

    Returns
    -------
    :
        The constraints

    Raises
    ------
    NotAConstraintError
        `value` is not a sequence, or one of its entries is not a constraint
    """
    # `str` and `bytes` are sequences too, and a single constraint is not a sequence
    # at all, so both are caught here rather than iterated into a per-entry error.
    if not isinstance(value, (list, tuple)):
        raise NotAConstraintError(value)

    return tuple(_load_constraint(each) for each in value)


def _serialise_constraints(
    value: tuple[AnyConstraint, ...],
) -> list[dict[str, Any]]:
    """
    Write constraints down, each with the class to rebuild it as

    Parameters
    ----------
    value
        The constraints

    Returns
    -------
    :
        One mapping per constraint: its fields, plus
        [CONSTRAINT_TYPE_KEY][(m).CONSTRAINT_TYPE_KEY]

    Raises
    ------
    NotSerialisableConstraintError
        One of the constraints is not a pydantic model
    """
    written = []
    for constraint in value:
        if not isinstance(constraint, BaseModel):
            raise NotSerialisableConstraintError(constraint)

        cls = type(constraint)
        written.append(
            {
                CONSTRAINT_TYPE_KEY: f"{cls.__module__}:{cls.__qualname__}",
                **constraint.model_dump(mode="json"),
            }
        )

    return written


Constraints = Annotated[
    tuple[AnyConstraint, ...],
    PlainValidator(_validate_constraints),
    PlainSerializer(_serialise_constraints),
]
"""
Field type for holding constraints on a pydantic model

A requirement is stored as canonical JSON and identified by the hash of it
([requirement_hash][esmporium.requirements.Requirement.requirement_hash]),
which is what makes "has this become satisfiable since?"
a question about new data
rather than about somebody having quietly edited what was asked for.
A constraint is part of what was asked for, so it has to be in that JSON
-- hence the two halves here,
and hence a constraint having to be a pydantic model if its requirement is to be stored.
Solving never serialises a requirement,
so a constraint which is not a pydantic model can still be used there.

JSON cannot hold behaviour, so each constraint is written with the class to rebuild it
as, under [CONSTRAINT_TYPE_KEY][(m).CONSTRAINT_TYPE_KEY], as `"module:QualName"`.

Two consequences are worth knowing before relying on this.

**Loading imports the module the stored text names**, so only load requirements you
trust: a requirement read from a database you do not control is code you do not control.

**The path is part of the hash**, so moving or renaming a constraint class changes the
hash of every stored requirement which uses it, and those requirements then stop
loading ([UnresolvableConstraintError][(m).UnresolvableConstraintError]).

Both halves run inside pydantic, which wraps whatever they raise:
[NotSerialisableConstraintError][(m).NotSerialisableConstraintError] surfaces inside a
`PydanticSerializationError`, and the loading errors inside a `ValidationError`. The
message is carried through intact, so there is no pre-check.
"""
