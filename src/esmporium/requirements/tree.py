"""
The requirement tree: what an analysis needs, and in what shape

A requirement is a tree: one root at the top, branching at each internal node
downward to leaves at the tips (a leaf is a node with no children). Each requirement
tree represents one run of an analysis: solved once per group. For example,
grouped by model means one requirement tree per model.

The one rule to hold onto is what each of them takes: **[Leaf][(m).Leaf] takes a
query (in any query style), everything else takes nodes.**
"""

# TODO: A note for whoever adds the next node type. The node types are purely additive:
# a new one is one more member of the [Node][(m).Node] union, one more entry in
# [NODE_TYPES][(m).NODE_TYPES], and one more branch in each of
# [apply_to_leaves][(m).apply_to_leaves], [walk_leaves][(m).walk_leaves] and
# [role_paths][(m).role_paths]. Nothing already here is reshaped. Those three functions
# each end in a `TypeError` rather than a silent fallthrough, so a forgotten branch
# fails loudly.

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Annotated, Any, Literal, TypeVar, Union, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from esmporium.formatting import readable_list
from esmporium.query import (
    CANONICAL_FACETS,
    ClashingFacetsError,
    Query,
    QueryCanonical,
    QueryProtocol,
    to_canonical,
)
from esmporium.requirements.catalogue import set_facets

NODE_MODEL_CONFIG = ConfigDict(frozen=True, extra="forbid")
"""
Model config shared by the nodes of a tree

*Frozen* so that, in order to change a node, a user must make a new node.
The reason is [requirement_hash][(m).Requirement.requirement_hash]: a
requirement is fingerprinted and stored, and anything which could be
edited afterwards would leave that fingerprint
describing something which no longer exists.
"""

ROLE_SEPARATOR = "."
"""
Separator between the parts of a role path, e.g. the `.` in `abrupt4x.control.tas`

One constant with two users, which is the reason it is not written out at either of
them: a role may not contain it (see [Leaf.role][(m).Leaf.role]), *because* it is
what joins the parts of a path together. A lineage or a scope makes those paths (R4
and R8), and the rule and the joining have to agree or a role could be written which
silently splits into two.
"""


# A note for developers:
# This looks like a duplicate of
# [`ClashingFacetsError`][esmporium.query.ClashingFacetsError] and is not one, so it
# stays here rather than moving to `esmporium.query`.
# This error is about values, rather than facets.
class ConflictingFacetsError(ValueError):
    """
    Raised when a facet is set to different values for the same dataset

    Repeating a facet is fine as long as the values agree *and* both sides keep it in
    the same home. Agreeing on the value but disagreeing on the home -- a declared
    field on one side, `other_terms` on the other -- raises
    [`ClashingFacetsError`][esmporium.query.ClashingFacetsError] instead.
    """

    def __init__(self, role: str, facets: Iterable[str], source_name: str) -> None:
        """
        Initialise the error

        Parameters
        ----------
        role
            Role of the leaf whose query is being added to

        facets
            Facets which are set differently

        source_name
            What to call, in the message, the thing the other values came from,
            e.g. `"where"`. A label for the reader, not the values themselves.
        """
        self.role = role
        self.facets = tuple(sorted(facets))
        super().__init__(
            f"{source_name} sets {readable_list(self.facets)} "
            f"differently to leaf {role!r}. "
            f"Set each facet once: on the leaf or in {source_name}, not both. "
            "Repeating a facet with the same values is fine, "
            "it is only a disagreement which is refused."
        )


class DuplicateRoleError(ValueError):
    """Raised when two parts of a requirement which are both used share a role."""

    def __init__(self, roles: Iterable[str]) -> None:
        """
        Initialise the error

        Parameters
        ----------
        roles
            The duplicated roles
        """
        self.roles = tuple(sorted(roles))
        super().__init__(
            f"Roles {readable_list(self.roles)} are used more than once. "
            "A role names one dataset, so a repeat leaves no way to say which "
            "dataset is meant. "
            "Give the leaves distinct roles, with `Leaf(query=..., role=...)`."
        )


class NotANodeError(TypeError):
    """
    Raised when something which is not a node is used where a node is needed

    Most often a facet value written where a leaf was meant.
    [Leaf][(m).Leaf] takes a query; everything else takes nodes.
    """

    def __init__(self, value: object) -> None:
        """
        Initialise the error

        Parameters
        ----------
        value
            What was passed
        """
        self.value = value
        allowed = readable_list([node_type.__name__ for node_type in NODE_TYPES])
        hint = ""
        if isinstance(value, str):
            hint = (
                " A facet value is not a node: "
                f"write `Leaf(query=Query(variable={value!r}), role=...)`, "
                "with a role saying what the dataset is for."
            )
        super().__init__(
            f"Expected a node, i.e. one of {allowed}, "
            f"got {type(value).__name__}: {value!r}. "
            "`Leaf` takes a query, everything else takes nodes." + hint
        )


class EmptyLeafQueryError(ValueError):
    """
    Raised when a leaf's query sets no facets

    Deliberately *not* an error one level down: the catalogue's
    [matches][esmporium.requirements.matches] treats a query which sets no facets as
    "no constraint", and so matches every entry. That is the right answer to the
    question as asked there. Asking nothing and requiring nothing are different
    questions, and this is the second one.
    """

    def __init__(self, role: str) -> None:
        """
        Initialise the error

        Parameters
        ----------
        role
            Role of the leaf
        """
        self.role = role
        super().__init__(
            f"The query on leaf {role!r} sets no facets, "
            "so it identifies no particular dataset "
            "and would claim every dataset in the catalogue. "
            "Name at least one facet, e.g. the variable. "
            "This is refused here rather than left to the solver because it fails a "
            "long way from the mistake: solving an empty query reports an ambiguous "
            "group with as many candidates as the catalogue holds, "
            "rather than an error where the requirement was written."
        )


Node = Annotated[Union["Leaf", "AllOf"], Field(discriminator="kind")]
"""
Any node of a requirement tree

`discriminator="kind"` tells pydantic to pick the member of the union by reading the
literal `kind` field, rather than trying each in turn. That makes validation errors
specific, and is what lets a stored requirement load back into the right node class.
"""

LeafUpdate = Callable[["Leaf"], "Leaf"]
"""An update applied to every leaf below a node"""

NodeT = TypeVar("NodeT", bound=Union["Leaf", "AllOf"])
"""Any node type, kept as itself by the helpers which update leaves"""


def _canonical(query: QueryProtocol) -> QueryCanonical:
    """
    Put a query into the form a requirement stores

    A leaf can be written in whichever query style suits the user, but is translated
    so that it is stored in a consistent way.
    That matters because a stored requirement has to reload
    into a known class, because one question must hash the same whichever style it was
    written in, and because turning a requirement back into searches (a later step)
    converts *from* canonical.

    Parameters
    ----------
    query
        Query to canonicalise, in any style

    Returns
    -------
    :
        The query, under canonical names and with no `source_query`
    """
    canonical = query if isinstance(query, QueryCanonical) else to_canonical(query)

    if canonical.source_query is None:
        return canonical

    return canonical.model_copy(update={"source_query": None})


def _accept_any_query_style(value: Any) -> Any:
    """
    Translate whatever was passed for a query field, on the way in

    Parameters
    ----------
    value
        What was passed

    Returns
    -------
    :
        A [`QueryCanonical`][esmporium.query.QueryCanonical] if `value` was a query of
        some kind, and `value` untouched otherwise, so that pydantic reports anything
        unusable in its own words.

        A mapping is left alone too: that is a stored requirement being loaded, where
        pydantic builds the query from JSON and there is nothing to translate.
    """
    # Duck-typed rather than `isinstance(value, QueryProtocol)`, because a `Protocol`
    # is only checkable at runtime when it says so, and this one does not.
    if isinstance(value, Mapping) or not hasattr(value, "other_terms"):
        return value

    return _canonical(value)


def _homes(query: QueryCanonical) -> tuple[dict[str, tuple[str, ...]], ...]:
    """
    Get a query's facets, split by the home each sits in

    Parameters
    ----------
    query
        Query to split

    Returns
    -------
    :
        The facets it declares as fields, its query-specific facets, and its
        `other_terms`, in that order -- the three homes, always in the same order, so
        that two queries can be merged home by home
    """
    declared = {
        name: values
        for name in sorted(CANONICAL_FACETS)
        if (values := getattr(query, name))
    }

    return declared, dict(query.query_specific_facets), dict(query.other_terms)


def _normalise(facets: dict[str, Any]) -> QueryCanonical:
    """
    Turn the keyword arguments of `.where()` into a query

    Nothing is checked here. The facets are handed straight to
    [`Query`][esmporium.query.Query], so that `.where()` obeys the same rules as
    writing a query anywhere else and this module never grows a second opinion about
    what a facet is. Three things follow from that: a single value becomes a tuple, so
    `.where(variable="tas")` and `.where(variable=("tas",))` are the same thing; a
    misspelt known facet name is an error rather than a facet which silently matches
    nothing for ever; and `other_terms` is accepted, because it is a field of `Query`
    like any other.

    It is also the limit of this route. `Query` has no field for a facet like CMIP5's
    `product`, so `.where(product=...)` is an error: pass a query which does name it,
    or put it in `other_terms`.

    Parameters
    ----------
    facets
        Facet values, as given to `.where()`

    Returns
    -------
    :
        The facets, as a query
    """
    return _canonical(Query(**facets))


def add_facets(
    query: QueryCanonical,
    incoming: QueryCanonical,
    role: str,
    source_name: str,
) -> QueryCanonical:
    """
    Add one query's facets to a leaf's query

    Each facet keeps the home its writer gave it, so a facet in `other_terms` stays in
    `other_terms` even when esmporium models it. `other_terms` is an escape hatch, and
    an escape hatch which quietly rewrites what you put in it is not one.

    The cost is that one facet written into two different homes makes two different
    requirements, which hash differently. That is the honest answer: a home is chosen
    rather than guessed at, so choosing another one asks another question.

    Parameters
    ----------
    query
        The leaf's query, i.e. the one being added to

    incoming
        Query whose facets are added to `query`

    role
        Role of the leaf, used in error messages

    source_name
        What to call `incoming` in error messages, e.g. `"where"`.
        A label for the reader; `incoming` carries the facets themselves.

    Returns
    -------
    :
        `query` with `incoming`'s facets added

    Raises
    ------
    ConflictingFacetsError
        `incoming` and `query` set the same facet to different values

    ClashingFacetsError
        Between them they set one facet in two different homes, so where the facet
        belongs would be ambiguous. Only reported when the values agree, since a
        disagreement about the value is checked first and is the more useful complaint.

    Examples
    --------
    >>> from esmporium.query import Query, QueryCanonical
    >>> from esmporium.requirements import set_facets
    >>> added = add_facets(
    ...     QueryCanonical(variable=("tas",)),
    ...     QueryCanonical(experiment=("1pctCO2",)),
    ...     "field",
    ...     "where",
    ... )
    >>> set_facets(added)
    {'experiment': ('1pctCO2',), 'variable': ('tas',)}

    A facet a query style names but we have no canonical name for keeps its own home,
    rather than being pushed into the escape hatch:

    >>> from esmporium.query import QueryCMIP5, to_canonical
    >>> added = add_facets(
    ...     to_canonical(QueryCMIP5(variable="tas")),
    ...     to_canonical(QueryCMIP5(product="output1")),
    ...     "field",
    ...     "where",
    ... )
    >>> added.query_specific_facets
    {'product': ('output1',)}
    >>> added.other_terms
    {}
    """
    already = set_facets(query)
    to_add = set_facets(incoming)

    conflicts = {
        name for name, values in to_add.items() if already.get(name, values) != values
    }
    if conflicts:
        raise ConflictingFacetsError(role, conflicts, source_name)

    merged = tuple(
        {**mine, **theirs} for mine, theirs in zip(_homes(query), _homes(incoming))
    )
    declared, query_specific, other = merged

    # One facet, two homes: the leaf put it in `other_terms` and `incoming` declares
    # it,
    # say. Nothing can decide which wins, and it is the same ambiguity a single query
    # naming a facet twice would raise, so it raises the same error.
    seen = [name for home in merged for name in home]
    doubled = {name for name in seen if seen.count(name) > 1}
    if doubled:
        raise ClashingFacetsError(doubled)

    # `model_validate` rather than `QueryCanonical(**declared, ...)` because expanding
    # a `dict[str, tuple[str, ...]]` into the keywords has to type-check against every
    # parameter, `source_query` included, which it cannot.
    return QueryCanonical.model_validate(
        {
            **declared,
            "query_specific_facets": query_specific,
            "other_terms": other,
        }
    )


class Leaf(BaseModel):
    """
    One dataset (per group) in a role, and everything about that dataset

    Written out in full, `Leaf(query=..., role=...)`, unlike the internal nodes, which
    are built by lower-case functions such as [all_of][(m).all_of]. A leaf is where
    every fact about a dataset ends up, so both of its parts are worth having on the
    page rather than inferred: what it asks for, and what it is for.
    """

    model_config = NODE_MODEL_CONFIG

    kind: Literal["leaf"] = "leaf"

    query: QueryCanonical
    """
    Query identifying the dataset

    A facet with several values is an OR, exactly as it is when searching:
    `Query(variable=("fLuc", "fLUC"))` matches either spelling. Use `group_by` on the
    [Requirement][(m).Requirement] to turn such a list into one group per value
    instead.

    Write it in whichever style suits the project --
    [`Query`][esmporium.query.Query], [`QueryCMIP5`][esmporium.query.QueryCMIP5] and
    the rest all work. It is translated on the way in and stored as a
    [`QueryCanonical`][esmporium.query.QueryCanonical]: a stored requirement has to
    reload into a known class, one question must hash the same whichever style it was
    written in, and turning a requirement back into searches converts *from* canonical.
    """

    role: str
    """
    Role the dataset is resolved into

    Say what the dataset is *for*, not which variable it happens to be. A role is the
    named slot the resolved dataset is filed under, and it is what a check refers to
    later, so it wants to read as a job rather than as a value.

    You choose it, freely, subject only to three rules: it cannot be empty, it cannot
    contain a [ROLE_SEPARATOR][(m).ROLE_SEPARATOR] (that separates the nested paths
    which arrive with lineage and scopes, as in `control.field`), and no two leaves
    used together may share one.
    """

    def __init__(self, **data: Any) -> None:
        """
        Initialise

        Parameters
        ----------
        **data
            The fields

        Raises
        ------
        EmptyLeafQueryError
            `query` sets no facets
        """
        # Checked here as well as in `_query_sets_a_facet` so that the error arrives as
        # itself. Raised from a validator, pydantic wraps it in a `ValidationError`,
        # which is the right answer for a requirement being *loaded* -- one bad leaf
        # should be reported alongside everything else wrong with the document -- but
        # buries the message for someone writing one by hand.
        #
        # Note that defining `__init__` at all sets pydantic's
        # `__pydantic_custom_init__`, which routes *every* path through it:
        # `model_validate` and `model_validate_json` call this too, and pass every
        # field, `kind` included. So `__init__` cannot be the thing which separates
        # writing from loading, and the guard below is.
        #
        # It raises only when there is a query object to ask and a role to name it by.
        # On the loading path `query` is a mapping, so `asks_nothing` is False however
        # empty it is, and `_query_sets_a_facet` refuses it a moment later -- wrapped,
        # which is what loading wants. Anything else is pydantic's to complain about,
        # in its own words.
        query = data.get("query")
        role = data.get("role")
        asks_nothing = (
            query is not None
            and not isinstance(query, Mapping)
            and hasattr(query, "other_terms")
            and not set_facets(query)
        )
        if asks_nothing and isinstance(role, str):
            raise EmptyLeafQueryError(role)

        super().__init__(**data)

    _accept_any_style = field_validator("query", mode="before")(_accept_any_query_style)

    @field_validator("role")
    @classmethod
    def _valid_role(cls, value: str) -> str:
        # `strip()` because a role of only whitespace is indistinguishable from an
        # empty one in any message which quotes it, and is never what was meant.
        # Capitals, spaces within, and length are all left alone: a role is a label
        # someone chose, and this is not the place to have opinions about their naming.
        if not value.strip() or ROLE_SEPARATOR in value:
            msg = (
                "Roles must have some non-whitespace content "
                f"and contain no {ROLE_SEPARATOR!r}, got {value!r}"
            )
            raise ValueError(msg)

        return value

    @model_validator(mode="after")
    def _query_sets_a_facet(self) -> Leaf:
        # After the field validators, so `query` is already canonical and `role` is
        # available to name in the error.
        if not set_facets(self.query):
            raise EmptyLeafQueryError(self.role)

        return self

    def where(self, **facets: Any) -> Leaf:
        """
        Add facets to this leaf's query

        Parameters
        ----------
        **facets
            Facet values

        Returns
        -------
        :
            Updated leaf

        Raises
        ------
        ConflictingFacetsError
            A facet is already set to a different value

        ClashingFacetsError
            A facet is already set to the same value, but in a different home
        """
        return self.model_copy(
            update={
                "query": add_facets(self.query, _normalise(facets), self.role, "where")
            }
        )


class AllOf(BaseModel):
    """
    All children (nodes) are needed

    Build one with [all_of][(m).all_of].
    """

    model_config = NODE_MODEL_CONFIG

    kind: Literal["all_of"] = "all_of"

    children: tuple[Node, ...] = Field(min_length=1)
    """
    What is needed, all of it

    At least one: a node with no children needs nothing, which is as useless as a
    leaf whose query sets no facets
    (see [EmptyLeafQueryError][(m).EmptyLeafQueryError]).
    """

    @model_validator(mode="after")
    def _distinct_roles(self) -> AllOf:
        # `role_paths` is what raises, because working out the roles is the same walk
        # as checking them for repeats.
        role_paths(self)

        return self

    def where(self, **facets: Any) -> AllOf:
        """
        Add facets to every leaf below this node

        Parameters
        ----------
        **facets
            Facet values

        Returns
        -------
        :
            Updated node

        Raises
        ------
        ConflictingFacetsError
            A leaf already sets one of these facets to a different value

        ClashingFacetsError
            A leaf already sets one of these facets to the same value, but in a
            different home
        """
        return apply_to_leaves(self, lambda each_leaf: each_leaf.where(**facets))


NODE_TYPES: tuple[type, ...] = (Leaf, AllOf)
"""
The concrete node types, i.e. the members of [Node][(m).Node]

Used to tell a node from something which is merely node-shaped.
A new node type is added here as well as to the union.
"""


class Requirement(BaseModel):
    """
    The root of a requirement: the tree, plus how solving splits it into groups

    A requirement is solved once per **group**, and a group is one run of the
    analysis.

    For example, the equilibrium climate sensitivity across forty models is forty
    groups, each resolved and reported on its own. `group_by` names the facets to
    split on; the groups themselves are not listed up front, but discovered from what
    the search found.
    """

    model_config = NODE_MODEL_CONFIG

    name: str
    """
    Name of the analysis, e.g. used to name its query collection

    Yours to choose, and the counterpart to [Leaf.role][(m).Leaf.role]: this names the
    whole calculation ("pattern-scaling", "gregory-method"), a role names one dataset's
    job inside it. One `name` per requirement, many roles beneath it.
    """

    tree: Node
    """The datasets needed"""

    where: QueryCanonical = Field(default_factory=QueryCanonical)
    """
    Facets added to every leaf's query

    Written and stored exactly as [Leaf.query][(m).Leaf.query] is: any query style is
    accepted and translated on the way in, and each facet keeps its home. A leaf which
    sets one of these facets to a different value is an error, and so is a leaf which
    agrees on the value but keeps the facet in a different home: set each facet once,
    in one home.

    Unlike a leaf's own query, this legitimately sets nothing at all, which is the
    default.
    """

    group_by: tuple[str, ...] = ("model", "variant_label")
    """
    Facets which define a group, i.e. what counts as one run of the analysis
    """

    prefer: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    """
    Facet -> values in order of preference, used to break ties between candidates

    The earliest value listed wins, and this is not special to `variable`:
    `prefer={"grid_label": ("gn", "gr")}` breaks the same kind of tie. A tie which
    survives `prefer` is settled by [cardinality][(m).Requirement.cardinality].

    Project-specific facets can be used, on the same terms as `group_by`.
    """
    # A note on why this is a mapping rather than a callable, which would let a user
    # inject their own ranking. A requirement is fingerprinted by
    # `requirement_hash`, via `canonical_json`, and a function has no canonical form:
    # `model_dump(mode="json")` raises `PydanticSerializationError` on one. Losing the
    # fingerprint would lose the only question it exists to answer, which is whether
    # two solves were of the same requirement.
    #
    # So pluggable ranking is possible, but it has to arrive the way `Constraint`
    # does: a pydantic model stored with its import path. That pattern lands in R5,
    # and nothing reads `prefer` until R3, so this waits for it rather than guessing
    # at it now.

    cardinality: Literal["one", "all"] = "one"
    """
    How many datasets each leaf resolves to per group

    `"one"`, the default, is the usual case: a leaf fills one slot, so several
    surviving candidates are a question nobody has answered rather than a result.
    The group is reported `ambiguous` and nothing is picked, which is the point --
    guessing would make the analysis depend on which dataset happened to be listed
    first. `prefer` is how a tie is settled on purpose.

    `"all"` is for an analysis whose subject *is* the spread across candidates.
    Ensemble spread is the worked example: the variance across a model's variants is
    one number computed from every variant together, so the leaf wants all of them.
    Written with `group_by=("model",)` and `cardinality="all"`, that is one run per
    model, each holding however many variants that model published.

    It is worth contrasting with the other way to write something variant-shaped,
    because they are easy to confuse and answer different questions:

    ```text
      group_by = ("model", "variant_label")   |  group_by = ("model",)
      cardinality = "one"                     |  cardinality = "all"
      ----------------------------------------+---------------------------------
      one run PER variant                     |  one run per MODEL
      each run sees a single variant          |  each run sees every variant
      "do this for r1, then for r2, ..."      |  "compare r1, r2, ... to each
                                              |   other"
    ```

    So the test to apply is the one the tree itself uses: a facet the analysis
    repeats *over* belongs in `group_by`, and candidates it needs *together* are what
    `"all"` is for.
    """

    def __init__(self, tree: Node | None = None, /, **data: Any) -> None:
        """
        Initialise

        Parameters
        ----------
        tree
            The tree, which reads better positionally than as a keyword.
            Can also be passed by keyword.

        **data
            The other fields
        """
        if tree is not None:
            data["tree"] = tree

        super().__init__(**data)

    _accept_any_style = field_validator("where", mode="before")(_accept_any_query_style)

    @model_validator(mode="after")
    def _check_tree(self) -> Requirement:
        role_paths(self.tree)
        # Fail here, where the requirement is written, rather than when the facets
        # are actually used.
        for _, node in walk_leaves(self.tree):
            effective_query(node, self.where)

        return self

    def canonical_json(self) -> str:
        """
        Get a canonical JSON representation

        "Canonical" here means one exact string per requirement: sorted keys, so the
        order `prefer`'s mapping happens to be written in does not change the answer,
        and no insignificant whitespace. Together with queries being translated as they
        are stored, that is what lets two requirements which ask the same thing come out
        byte for byte identical whatever style each was written in, which is the whole
        basis of [requirement_hash][(m).Requirement.requirement_hash].

        Returns
        -------
        :
            The JSON
        """
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )

    def requirement_hash(self) -> str:
        """
        Get a hash which identifies this requirement

        A *hash* is a fingerprint: a short, fixed-length string computed from content,
        where the same content always gives the same string and a change anywhere gives
        a completely different one. This one is 64 characters whatever the size of the
        requirement.

        What it is for: a requirement is solved now and again later, and the useful
        question is "has this become satisfiable since?". That only means something if
        both solves were of the *same* requirement, and the fingerprint is how that is
        known -- if it matches, an improvement is new data arriving rather than someone
        having quietly edited what was asked for. It also makes a short, stable key for
        the requirement in a database, instead of storing the whole tree again just to
        identify it.

        Returns
        -------
        :
            SHA-256 of [canonical_json][(m).Requirement.canonical_json]
        """
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()


def effective_query(node: Leaf, where: QueryCanonical | None) -> QueryCanonical:
    """
    Get the query which identifies a leaf's datasets

    Parameters
    ----------
    node
        The leaf

    where
        Facets to add, from the requirement

    Returns
    -------
    :
        The combined query

    Raises
    ------
    ConflictingFacetsError
        `where` and the leaf set the same facet to different values

    ClashingFacetsError
        `where` and the leaf agree on a facet's value but keep it in different homes
    """
    if where is None:
        return node.query

    return add_facets(node.query, where, node.role, "where")


def apply_to_leaves(node: NodeT, update: LeafUpdate) -> NodeT:
    """
    Apply an update to every leaf below a node

    Parameters
    ----------
    node
        Node to update

    update
        What to do to each leaf

    Returns
    -------
    :
        Updated node, of the same kind as `node`

    Raises
    ------
    TypeError
        `node` is not a node
    """
    if isinstance(node, Leaf):
        return cast(NodeT, update(node))

    if isinstance(node, AllOf):
        children = tuple(apply_to_leaves(child, update) for child in node.children)

        return cast(NodeT, node.model_copy(update={"children": children}))

    raise NotANodeError(node)


def walk_leaves(node: Node, role_prefix: str = "") -> Iterator[tuple[str, Leaf]]:
    """
    Iterate over every leaf in a tree

    Parameters
    ----------
    node
        Node to walk

    role_prefix
        Role path prefix the node sits under, e.g. `"abrupt4x."` giving
        `abrupt4x.field`, joined with [ROLE_SEPARATOR][(m).ROLE_SEPARATOR]

        Always empty for now. It is here because a node which renames the leaves below
        it is what makes the same role usable twice, and that node is the point of this
        parameter.

    Yields
    ------
    :
        The role path prefix each leaf sits under, and the leaf

    Raises
    ------
    TypeError
        `node` is not a node
    """
    if isinstance(node, Leaf):
        yield role_prefix, node
    elif isinstance(node, AllOf):
        for child in node.children:
            yield from walk_leaves(child, role_prefix)
    else:
        raise NotANodeError(node)


def role_paths(node: Node) -> frozenset[str]:
    """
    Get the role paths a node can resolve

    Parameters
    ----------
    node
        Node to inspect

    Returns
    -------
    :
        Role paths

    Raises
    ------
    DuplicateRoleError
        Roles are duplicated in a way which could not be resolved

    TypeError
        `node` is not a node
    """
    if isinstance(node, Leaf):
        return frozenset({node.role})

    if isinstance(node, AllOf):
        return distinct_role_paths(node.children)

    raise NotANodeError(node)


def distinct_role_paths(nodes: Iterable[Node]) -> frozenset[str]:
    """
    Get the role paths a set of sibling nodes resolves, refusing any repeat

    Separate from [role_paths][(m).role_paths] so that [all_of][(m).all_of] can check
    its arguments *before* handing them to pydantic, which would otherwise wrap the
    error in a `ValidationError` and bury the message.

    Parameters
    ----------
    nodes
        Sibling nodes, all of which are used

    Returns
    -------
    :
        Role paths

    Raises
    ------
    DuplicateRoleError
        Two of the nodes resolve the same role
    """
    seen: set[str] = set()
    duplicated: set[str] = set()
    for node in nodes:
        node_roles = role_paths(node)
        duplicated |= seen & node_roles
        seen |= node_roles

    if duplicated:
        raise DuplicateRoleError(duplicated)

    return frozenset(seen)


def all_of(*nodes: Node) -> AllOf:
    """
    Require all of the given nodes

    Parameters
    ----------
    *nodes
        Nodes

    Returns
    -------
    :
        Node

    Raises
    ------
    NotANodeError
        Something which is not a node was given

    DuplicateRoleError
        Two of the nodes resolve the same role
    """
    for node in nodes:
        if not isinstance(node, NODE_TYPES):
            raise NotANodeError(node)

    # As with `Leaf`, checked here so the error arrives as itself rather than wrapped
    # in a `ValidationError` by the model validator which backs it up.
    distinct_role_paths(nodes)

    return AllOf(children=nodes)


for _model in (Leaf, AllOf, Requirement):
    _model.model_rebuild()
