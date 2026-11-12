"""
Constraints: judgements about the datasets a requirement selected

A query selects datasets by matching facets, which answers "is there one?".
A constraint answers the questions a facet cannot:
e.g. whether the control a simulation branched from covers the years being analysed,
whether two variables span the same period,
whether there is enough of a run to regress against.
Those are judgements about what is *in* the data rather than about which dataset it is.

[Constraint][(m).Constraint] is the extension point.
A constraint is attached to a
[Leaf][esmporium.requirements.Leaf] or to a
[Requirement][esmporium.requirements.Requirement],
and **where it is attached decides what its failure costs**:

- on a leaf, it sees that dataset and the chain behind it,
  and a failure makes that leaf unsatisfied
- on the requirement, it sees every role in the group, and a failure fails the group

## Constraints which need more than the catalogue protocol provides

[Constraint][(m).Constraint] is generic in the catalogue it needs.
A constraint which only needs what the
[Catalogue][esmporium.requirements.Catalogue] protocol provides
is a `Constraint[Catalogue]`.
A constraint which needs more, e.g. the time range of a dataset,
defines a protocol next to itself which adds the methods it needs
and is a constraint over that protocol instead.
[Covers][esmporium.requirements.Covers] is the worked example:
it is a `Constraint[CatalogueWithTimeInfo]`.
This keeps everything local and easy to trace,
and a type checker can tell whether a catalogue can be used with a constraint.

What a type checker cannot check is a requirement:
it holds constraints which may each need a different catalogue.
So at runtime, if the catalogue passed to the solver doesn't have a method
a constraint needs, the constraint raises,
and the solver reports the part of the requirement it was attached to
as unsatisfied, with the error in the explanation.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Protocol, TypeVar, cast

from pydantic import BaseModel, PlainSerializer, PlainValidator

from esmporium.requirements.catalogue import Catalogue, CatalogueEntry
from esmporium.requirements.relations import Lineage

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


Outcome = Pass | Degraded | Fail
"""
What a constraint answers

A constraint which cannot be evaluated raises instead,
and the solver reports that as a failure
-- see [Constraint][(m).Constraint].
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

        If the constraint cannot be evaluated, raise.
        The solver reports that as a failure.

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
