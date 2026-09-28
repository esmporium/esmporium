"""
The requirement tree: what an analysis needs, and in what shape

[`catalogue`][esmporium.requirements.catalogue] answers "which datasets match this
one query?". This module is the other half of the pincer: the *shape of a need*.
Neither knows about the other, which is what lets a requirement be solved against
any catalogue. Joining them is the solver's job (a later step).

A requirement is a **tree**: one root at the top, branching downward to *leaves* at
the tips (drawn upside down, like a family tree). Two words carry the weight:

- a **leaf** is a node with nothing below it, and resolves to exactly one dataset
  per group. It carries everything about that dataset, starting with the query which
  identifies it;
- an **internal node** has children and exists to group or wrap them.

The one rule to hold onto is what each of them takes: **[Leaf][(m).Leaf] takes a
query, everything else takes nodes.**

A leaf is written out in full, `Leaf(query=..., role=...)`. The internal nodes are
built by lower-case functions ([all_of][(m).all_of]) which earn their keep by taking
any number of children.

```text
Requirement                        <- the root: also says how datasets are grouped
   └── all_of                      <- internal node: needs ALL of its children
       ├── Leaf role="field"       <- leaf: one dataset, nothing below it
       └── Leaf role="land_fraction"  <- leaf
```

A role names what the dataset is *for*, which is usually not a variable name: see
[Leaf.role][(m).Leaf.role].

`.where(**facets)` is the one way to say something about the leaves below a node, and
it does **not** get stored on the node: it is pushed straight down into each leaf's
query, because the leaf is where the work happens.

```text
   all_of                            all_of
     ├── Leaf(variable=tas)   .where(    ├── Leaf(variable=tas,  experiment=1pctCO2)
     └── Leaf(variable=rsdt) experiment= └── Leaf(variable=rsdt, experiment=1pctCO2)
                             "1pctCO2")
```

That is why a contradiction raises [ConflictingFacetsError][(m).ConflictingFacetsError]
at the moment it is written, rather than a long way away when the requirement is
solved. Set each facet once.

Two other kinds of "or" are deliberately not nodes: aliases are tuple facet values in
a query (`variable=("fLuc", "fLUC")` takes either), and fanning out over a facet's
values is `group_by` on the [Requirement][(m).Requirement].

A note for whoever adds the next node type. The node types are purely additive: a new
one is one more member of the [Node][(m).Node] union, one more entry in
[NODE_TYPES][(m).NODE_TYPES], and one more branch in each of
[apply_to_leaves][(m).apply_to_leaves], [walk_leaves][(m).walk_leaves] and
[role_paths][(m).role_paths]. Nothing already here is reshaped. Those three functions
each end in a `TypeError` rather than a silent fallthrough, so a forgotten branch
fails loudly.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Annotated, Any, Literal, TypeVar, Union, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from esmporium.formatting import readable_list
from esmporium.query import CANONICAL_FACETS, Query
from esmporium.requirements.catalogue import set_facets

NODE_MODEL_CONFIG = ConfigDict(frozen=True, extra="forbid")
"""
Model config shared by the nodes of a tree

Frozen because a requirement is stored, hashed and re-solved later, so a node which
could be edited in place would let its hash go stale.
Note that this promise is only as strong as the objects a node holds: a leaf's
[`Query`][esmporium.query.Query] is not itself frozen, so it can still be mutated
in place (see the note on [Leaf.query][(m).Leaf.query]).
"""


# A note for developers:
# This looks like a duplicate of
# [`ClashingFacetsError`][esmporium.query.ClashingFacetsError] and is not one, so it
# stays here rather than moving to `esmporium.query` the way that one did.
#
# `ClashingFacetsError` is about *one* query naming the same facet twice, once as a
# field and once in `other_terms`: there is no way to tell which value should win, so
# any double-set is refused. This is about *two* sources -- a leaf and the `where` above
# it -- and it refuses only a disagreement: setting the same facet to the same values in
# both places is fine and common, because `where` exists precisely to say something
# about leaves which may already say it themselves.
#
# It also carries what only this side knows: which leaf, by role, and which source. The
# design note names this error in its table of what `.where()` raises, so the name is
# pinned there rather than chosen here.
class ConflictingFacetsError(ValueError):
    """Raised when a facet is set to different values for the same dataset."""

    def __init__(self, role: str, facets: Iterable[str], source: str) -> None:
        """
        Initialise the error

        Parameters
        ----------
        role
            Role of the leaf whose query is being added to

        facets
            Facets which are set differently

        source
            Where the other values come from, e.g. `"where"`
        """
        self.role = role
        self.facets = tuple(sorted(facets))
        super().__init__(
            f"{source} sets {readable_list(self.facets)} "
            f"differently to leaf {role!r}. "
            f"Set each facet once: on the leaf or in {source}, not both. "
            "Setting it to the same values in both places is fine, "
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

    Deliberately *not* an error one level down:
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


def _as_query(facets: Mapping[str, tuple[str, ...]]) -> Query:
    """
    Build a query from flattened facets

    The inverse of [set_facets][esmporium.requirements.set_facets], and the reason a
    leaf's query is always in one predictable shape. A facet we model goes in its own
    field, and everything else goes in `other_terms`. Two consequences are worth
    knowing:

    - the query is built by **validating** rather than by copying, so a facet cannot
      be set on a query which has no field for it. Setting one by copying looks like
      it worked and is then silently dropped when the query is serialised, which for
      a requirement means it hashes and reloads as something other than what was
      written;
    - a facet we *do* model is moved out of `other_terms` into its field, so
      `Query(other_terms={"variable": ("tas",)})` and `Query(variable="tas")` become
      the same stored query, and hash the same.

    `source_query` is dropped, deliberately: it exists for debugging a translation,
    so keeping it would make two queries which mean the same thing hash differently.

    Parameters
    ----------
    facets
        Facet name -> values, as
        [set_facets][esmporium.requirements.set_facets] returns them

    Returns
    -------
    :
        The query
    """
    declared = {
        name: values for name, values in facets.items() if name in CANONICAL_FACETS
    }
    other = {
        name: values for name, values in facets.items() if name not in CANONICAL_FACETS
    }

    # `model_validate` rather than `Query(**declared, ...)` because expanding a
    # `dict[str, tuple[str, ...]]` into the keywords has to type-check against every
    # parameter, `source_query` included, which it cannot.
    return Query.model_validate({**declared, "other_terms": other})


def _canonical_query(query: Query) -> Query:
    """
    Put a query into the shape a requirement stores

    Parameters
    ----------
    query
        Query to canonicalise

    Returns
    -------
    :
        The same facets, in one predictable shape (see `_as_query`)
    """
    return _as_query(set_facets(query))


def _normalise(facets: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    """
    Flatten keyword facets, validating them as esmporium would

    Going through [`Query`][esmporium.query.Query] is what makes
    `.where(variable="tas")` and `.where(variable=("tas",))` the same thing, and what
    makes a misspelt facet name an error rather than a facet nothing will ever match.
    It is also how `.where(other_terms={...})` is accepted, `other_terms` being a
    field of `Query`.

    Parameters
    ----------
    facets
        Facet values, as given to `.where()`

    Returns
    -------
    :
        Facet name -> values
    """
    return set_facets(Query(**facets))


def add_facets(
    query: Query, facets: Mapping[str, tuple[str, ...]], role: str, source: str
) -> Query:
    """
    Add facets to a leaf's query

    Parameters
    ----------
    query
        The leaf's query

    facets
        Facets to add, flattened

    role
        Role of the leaf, used in error messages

    source
        Where the facets come from, used in error messages

    Returns
    -------
    :
        `query` with `facets` added

    Raises
    ------
    ConflictingFacetsError
        `facets` and `query` set the same facet to different values

    Examples
    --------
    >>> from esmporium.query import Query
    >>> from esmporium.requirements import set_facets
    >>> added = add_facets(
    ...     Query(variable="tas"), {"experiment": ("1pctCO2",)}, "field", "where"
    ... )
    >>> set_facets(added)
    {'experiment': ('1pctCO2',), 'variable': ('tas',)}

    A facet we have no field for lands in `other_terms`, and survives being stored:

    >>> added = add_facets(
    ...     Query(variable="tas"), {"product": ("output1",)}, "field", "where"
    ... )
    >>> added.other_terms
    {'product': ('output1',)}
    >>> set_facets(Query.model_validate_json(added.model_dump_json()))
    {'variable': ('tas',), 'product': ('output1',)}
    """
    already = set_facets(query)
    conflicts = {
        name for name, values in facets.items() if already.get(name, values) != values
    }
    if conflicts:
        raise ConflictingFacetsError(role, conflicts, source)

    return _as_query({**already, **facets})


class Leaf(BaseModel):
    """
    One dataset (per group) in a role, and everything about that dataset

    Written out in full, `Leaf(query=..., role=...)`, unlike the internal nodes, which
    are built by lower-case functions such as [all_of][(m).all_of]. A leaf is where
    every fact about a dataset ends up, so both of its parts are worth having on the
    page rather than inferred: what it asks for, and what it is for.

    Examples
    --------
    The role says what the dataset is *for*, which is not the same as which variable
    it happens to be. Pattern scaling scales *a* field against global mean
    temperature, and any one of nine variables will do, so it is one leaf whose query
    offers all nine and whose role is not a variable name at all:

    >>> from esmporium.query import Query
    >>> from esmporium.requirements import Leaf, set_facets
    >>> field = Leaf(
    ...     query=Query(variable=("tas", "tasmax", "tasmin", "pr", "sfcWind")),
    ...     role="field",
    ... )
    >>> field.role
    'field'
    >>> len(set_facets(field.query)["variable"])
    5

    A leaf resolves to exactly one dataset per group, so which of those five it is has
    to be settled somewhere. That somewhere is `group_by` on the
    [Requirement][(m).Requirement]: putting `variable` there runs the analysis once
    per variable, rather than picking one and discarding the other four. The two homes
    of a facet are worth keeping straight — in a leaf's query it means "any of these
    will do", in `group_by` it means "do this once for each".

    Roles which *do* read like variable names are a fact about the analysis, not a
    rule about leaves. Equilibrium climate sensitivity regresses tas against
    top-of-atmosphere radiation, so it needs four different datasets in the *same*
    run, which is four leaves — and naming each role after its variable is then simply
    the clearest thing to call it:

    >>> radiation = tuple(
    ...     Leaf(query=Query(variable=name), role=name)
    ...     for name in ("tas", "rsdt", "rlut", "rsut")
    ... )
    >>> [node.role for node in radiation]
    ['tas', 'rsdt', 'rlut', 'rsut']

    The test to apply: datasets the analysis needs *together* are separate leaves;
    a facet it repeats *over* is one leaf and a `group_by` entry.
    """

    model_config = NODE_MODEL_CONFIG

    kind: Literal["leaf"] = "leaf"

    query: Query
    """
    Query identifying the dataset

    A facet with several values is an OR, exactly as it is when searching:
    `Query(variable=("fLuc", "fLUC"))` matches either spelling. Use `group_by` on the
    [Requirement][(m).Requirement] to turn such a list into one group per value
    instead.

    The query is stored in one predictable shape: a facet we model goes in its own
    field, everything else goes in `other_terms`, and `source_query` is dropped. So
    two ways of writing the same question hash the same, and
    `Query(other_terms={"variable": ("tas",)})` is stored as `Query(variable="tas")`.

    ## When to reach for `other_terms`

    `other_terms` is esmporium's escape hatch for facets no query class names, and it
    stays an escape hatch here. It behaves differently inside a requirement than it
    does in a one-off search, in ways worth knowing before using it.

    **Two facets, two homes.** A facet a query class names is *translated* and then
    compared under its canonical name. The same facet in `other_terms` is *not*
    translated at all. So `QueryCMIP6(table_id="Amon")` asks about `processing_id`,
    whereas `Query(other_terms={"table_id": ("Amon",)})` asks about `table_id`, and
    finds nothing unless a catalogue puts `table_id` into each entry's
    [`extra`][esmporium.requirements.CatalogueEntry.extra].

    **What it costs in a requirement is not what it costs in a search.** A search is
    written once and sent. A requirement is stored, hashed and re-solved months
    later, and its facets are compared against stored entries rather than sent to an
    API. So keys here have to be facet names an *entry* can answer: a canonical name,
    or a key of that entry's `extra`. An API parameter name such as
    `cmip6:experiment_id` will simply never match anything, quietly.

    **When to use it anyway.** Two cases, both real: a facet esmporium has not
    modelled yet, and a project-specific facet no query class names. Those are why it
    is still here.

    **The better route, when there is one.** A facet which has to work in *both*
    directions — outbound to a search API and inbound against stored entries —
    belongs on a query class, annotated `QueryFacet(None)` the way CMIP5's `product`
    is. Those are translated, and arrive as `query_specific_facets`.
    """
    # TODO: `Query` is not frozen (its config is `extra="forbid"` only), so a leaf's
    # query can be mutated in place even though the leaf itself is frozen, which
    # would let `requirement_hash` go stale. Freezing `Query` is the fix, but it
    # belongs in `esmporium.query` and has its own blast radius, so it is its own PR.

    role: str
    """
    Role the dataset is resolved into

    Say what the dataset is *for*, not which variable it happens to be. A role is the
    named slot the resolved dataset is filed under, and it is what a constraint refers
    to later, so it wants to read as a job rather than as a value.

    The variable is not the leaf's identity. It has two other homes, and which one it
    is in is the whole question:

    - in this leaf's `query`, several variables mean "any one of these will do", and
      the leaf still resolves to exactly one dataset;
    - in `group_by` on the [Requirement][(m).Requirement], a variable means "run the
      whole analysis once per value".

    Pattern scaling puts nine variables in one leaf called `field` and `variable` in
    `group_by`, which is nine runs of one dataset each. Roles which do read like
    variable names, as equilibrium climate sensitivity's `tas` and `rsdt` do, are a
    fact about that analysis needing those datasets *together* -- not a rule about
    leaves.
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
        # buries the message for someone writing one by hand. `model_validate` and
        # `model_validate_json` do not call `__init__`, so loading still goes through
        # the validator.
        #
        # Only when both parts are already the right types: anything else is pydantic's
        # to complain about, in its own words.
        query = data.get("query")
        role = data.get("role")
        if isinstance(query, Query) and isinstance(role, str) and not set_facets(query):
            raise EmptyLeafQueryError(role)

        super().__init__(**data)

    @field_validator("query")
    @classmethod
    def _canonicalise_query(cls, value: Query) -> Query:
        return _canonical_query(value)

    @field_validator("role")
    @classmethod
    def _valid_role(cls, value: str) -> str:
        if not value or "." in value:
            msg = f"Roles must be non-empty and contain no '.', got {value!r}"
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
        """
        return self.model_copy(
            update={
                "query": add_facets(self.query, _normalise(facets), self.role, "where")
            }
        )


class AllOf(BaseModel):
    """
    All children are needed

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
    The root of a requirement: the tree, plus how datasets are grouped

    A requirement is solved once per **group**, and a group is one run of the
    analysis: the equilibrium climate sensitivity across forty models is forty
    groups, each resolved and reported on its own. `group_by` names the facets to
    split on; the groups themselves are not listed up front, but discovered from what
    the search found.
    """

    model_config = NODE_MODEL_CONFIG

    name: str
    """Name of the requirement, e.g. used to name its query collection"""

    tree: Node
    """The datasets needed"""

    where: Query = Field(default_factory=Query)
    """
    Facets added to every leaf's query

    A leaf which sets one of these facets differently is an error: set each facet
    once. Unlike a leaf's own query, this legitimately sets nothing at all, which is
    the default.
    """

    group_by: tuple[str, ...] = ("model", "variant_label")
    """
    Facets which define a group

    Include e.g. `experiment` or `variable` to fan out over their values, so that the
    tree is written once and solved once per value.

    Any facet name is accepted, project-specific ones included, as long as the
    catalogue puts the facet into each entry's
    [`extra`][esmporium.requirements.CatalogueEntry.extra]. What a catalogue can
    answer is the catalogue's business, so it cannot be checked when the requirement
    is built: a facet no entry knows fails when the requirement is solved, naming the
    facet and the dataset.
    """

    prefer: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    """
    Facet -> values in order of preference, used to break ties between candidates

    Project-specific facets can be used, on the same terms as `group_by`.
    """

    cardinality: Literal["one", "all"] = "one"
    """
    How many datasets each leaf resolves to per group

    `"one"` treats several remaining candidates as ambiguous.
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

    @field_validator("where")
    @classmethod
    def _canonicalise_where(cls, value: Query) -> Query:
        return _canonical_query(value)

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

        Sorted keys, so that the order `prefer`'s mapping happens to be written in
        does not change the answer, and no insignificant whitespace.

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

        Two requirements which ask for the same thing have the same hash, whichever
        way each was written, which is what makes "has this requirement become
        satisfiable?" answerable by comparing solves of the same hash over time.

        Returns
        -------
        :
            SHA-256 of [canonical_json][(m).Requirement.canonical_json]
        """
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()


def effective_query(node: Leaf, where: Query | None) -> Query:
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
    """
    if where is None:
        return node.query

    return add_facets(node.query, set_facets(where), node.role, "where")


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


def walk_leaves(node: Node, prefix: str = "") -> Iterator[tuple[str, Leaf]]:
    """
    Iterate over every leaf in a tree

    Parameters
    ----------
    node
        Node to walk

    prefix
        Role prefix the node sits under

        Always empty for now. It is here because a node which renames the leaves
        below it is what makes the same role usable twice, and that node is the point
        of this parameter.

    Yields
    ------
    :
        The prefix each leaf sits under, and the leaf

    Raises
    ------
    TypeError
        `node` is not a node
    """
    if isinstance(node, Leaf):
        yield prefix, node
    elif isinstance(node, AllOf):
        for child in node.children:
            yield from walk_leaves(child, prefix)
    else:
        raise NotANodeError(node)


def role_paths(node: Node) -> frozenset[str]:
    """
    Get the role paths a node can resolve

    Flat for now: a leaf resolves its own role and nothing else. The dotted paths a
    requirement ends up with (`control.tas`, `abrupt4x.tas`) come from a leaf's
    lineage and from nodes which rename what is below them, and this is the function
    which will build them.

    Doubling as the duplicate-role check is not incidental: working out the roles and
    checking them for repeats is the same walk.

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

    Examples
    --------
    Children are the datasets an analysis needs *together*. An energy balance wants a
    field and the surface fractions to weight it by, in the same run, so they are two
    leaves — and neither role is a variable name:

    >>> from esmporium.query import Query
    >>> from esmporium.requirements import Leaf, all_of, set_facets
    >>> node = all_of(
    ...     Leaf(query=Query(variable=("tas", "ts")), role="temperature"),
    ...     Leaf(query=Query(variable="sftlf"), role="land_fraction"),
    ... )
    >>> [child.role for child in node.children]
    ['temperature', 'land_fraction']

    `.where()` pushes down to every leaf, rather than being stored on the node:

    >>> monthly = node.where(experiment="1pctCO2", reporting_interval="mon")
    >>> [set_facets(child.query)["experiment"] for child in monthly.children]
    [('1pctCO2',), ('1pctCO2',)]

    A facet value is not a node, and saying so is the whole point of
    [NotANodeError][(m).NotANodeError]. Its message goes on to name the
    `Leaf(query=..., role=...)` which was probably meant:

    >>> all_of("tas", "rsdt")  # doctest: +ELLIPSIS
    Traceback (most recent call last):
    ...
    esmporium.requirements.tree.NotANodeError: Expected a node, i.e. one of ...
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
