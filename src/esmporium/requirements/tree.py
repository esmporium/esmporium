"""
The requirement tree: what an analysis needs, and in what shape

A requirement is a tree: one root at the top, branching at each internal node
downward to leaves at the tips (a leaf is a node with no children).

The one rule to hold onto is what each of them takes: **[Leaf][(m).Leaf] takes a
query, everything else takes nodes.**

Two words are used throughout, and neither means quite what it might elsewhere.

A **group** is one run of the analysis. A requirement is solved once per group, and
which datasets belong to the same run is set by `group_by` on the
[Requirement][(m).Requirement]. Asking for forty models gives forty groups, each
resolved and reported on its own, so one can succeed where the next fails.

A **role** is the named slot one dataset is filed under inside a group. Each leaf
fills exactly one. A role says what the dataset is *for*, so it reads as a job rather
than as a value, and it is how a check refers to a dataset later.

```text
   Requirement            name  = "the analysis"      <- what is being calculated
        |                 group_by = ("model", ...)   <- what counts as one run
        |
        +-- all_of
             +-- Leaf     role  = "field"             <- what one dataset is FOR
             +-- Leaf     role  = "land_fraction"
```
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

*Frozen* means the object cannot be changed once it is made: the language refuses,
rather than it being a rule people are asked to follow. To "change" a node you make a
new one, which is what `.where()` does. The reason is
[requirement_hash][(m).Requirement.requirement_hash]: a requirement is fingerprinted
and stored, and anything which could be edited afterwards would leave that fingerprint
describing something which no longer exists.

The promise is only as strong as what a node holds, which is why a leaf stores a
[`QueryCanonical`][esmporium.query.QueryCanonical]. That one is frozen too.
[`Query`][esmporium.query.Query] is not, so a leaf which held one would be a locked
box with editable contents.
"""


# A note for developers:
# This looks like a duplicate of
# [`ClashingFacetsError`][esmporium.query.ClashingFacetsError] and is not one, so it
# stays here rather than moving to `esmporium.query`.
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

    Two things happen here, and both are about a requirement being *stored* rather
    than used once and forgotten.

    It is **translated**, so a leaf can be written in whichever query style suits the
    project -- [`QueryCMIP6`][esmporium.query.QueryCMIP6] and the rest all work -- and
    still be stored one way. That matters because a stored requirement has to reload
    into a known class, because two spellings of the same question must hash the same,
    and because turning a requirement back into searches (a later step) converts *from*
    canonical.

    Its `source_query` is **dropped**. That field records which query a translation
    came from, for debugging, so keeping it would make two queries which mean the same
    thing hash differently.

    Nothing else is moved. Each of the three kinds of facet stays in the home the
    writer chose for it: the ones we model under their own names as fields, the ones a
    query style names but we have no canonical name for (CMIP5's `product`) in
    `query_specific_facets`, and the ones we do not model at all in `other_terms`.

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
    Get a query's facets, kept in the three places it holds them

    Parameters
    ----------
    query
        Query to split

    Returns
    -------
    :
        The facets it declares as fields, its query-specific facets, and its
        `other_terms`, in that order
    """
    declared = {
        name: values
        for name in sorted(CANONICAL_FACETS)
        if (values := getattr(query, name))
    }

    return declared, dict(query.query_specific_facets), dict(query.other_terms)


def _normalise(facets: dict[str, Any]) -> QueryCanonical:
    """
    Turn keyword facets into a query, validating them as esmporium would

    Going through [`Query`][esmporium.query.Query] is what makes
    `.where(variable="tas")` and `.where(variable=("tas",))` the same thing, and what
    makes a misspelt facet name an error rather than a facet nothing will ever match.
    It is also how `.where(other_terms={...})` is accepted, `other_terms` being a
    field of `Query`.

    It is also the limit of this route: `Query` has no field for a facet like CMIP5's
    `product`, so `.where(product=...)` is an error. Pass a query which does name it,
    or use `other_terms`.

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
    query: QueryCanonical, adding: QueryCanonical, role: str, source: str
) -> QueryCanonical:
    """
    Add one query's facets to a leaf's query

    Each facet keeps the home its writer gave it, so a facet in `other_terms` stays in
    `other_terms` even when we do model it. `other_terms` is an escape hatch, and an
    escape hatch which quietly rewrites what you put in it is not one. The cost is
    that two spellings of the same facet are two different questions, and hash
    differently, which is the honest answer: they were written differently on purpose.

    Parameters
    ----------
    query
        The leaf's query

    adding
        Facets to add

    role
        Role of the leaf, used in error messages

    source
        Where the facets come from, used in error messages

    Returns
    -------
    :
        `query` with `adding`'s facets added

    Raises
    ------
    ConflictingFacetsError
        `adding` and `query` set the same facet to different values

    ClashingFacetsError
        Between them they set one facet in two different homes, so which value applies
        would be ambiguous

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
    incoming = set_facets(adding)

    conflicts = {
        name for name, values in incoming.items() if already.get(name, values) != values
    }
    if conflicts:
        raise ConflictingFacetsError(role, conflicts, source)

    merged = tuple(
        {**mine, **theirs} for mine, theirs in zip(_homes(query), _homes(adding))
    )
    declared, query_specific, other = merged

    # One facet, two homes: the leaf put it in `other_terms` and `source` declares it,
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

    Examples
    --------
    A role says what the dataset is *for*, which is not the same as which variable it
    happens to be. Where an analysis works on any one of several variables, and repeats
    itself for each, that is one leaf whose query offers all of them and whose role is
    not a variable name at all:

    >>> from esmporium.query import Query
    >>> from esmporium.requirements import Leaf, set_facets
    >>> field = Leaf(
    ...     query=Query(variable=("tas", "tasmax", "pr", "sfcWind")),
    ...     role="field",
    ... )
    >>> field.role
    'field'
    >>> len(set_facets(field.query)["variable"])
    4

    A leaf resolves to one dataset per group, so which of those four it is has to be
    settled somewhere, and that somewhere is `group_by` on the
    [Requirement][(m).Requirement]. Naming `variable` there runs the analysis once per
    variable rather than picking one and discarding the rest.

    Roles which do read like variable names are a fact about the analysis, not a rule
    about leaves. Where an analysis needs several *different* datasets in the same run,
    they are separate leaves, and naming each role after its variable is often the
    clearest thing to call it:

    >>> together = tuple(
    ...     Leaf(query=Query(variable=name), role=name)
    ...     for name in ("tas", "rsdt", "rlut")
    ... )
    >>> [node.role for node in together]
    ['tas', 'rsdt', 'rlut']

    The test to apply: datasets needed *together* are separate leaves; a facet the
    analysis repeats *over* is one leaf and a `group_by` entry.

    A query may be written in any style, and is stored translated:

    >>> from esmporium.query import QueryCMIP6
    >>> in_cmip6_names = Leaf(query=QueryCMIP6(variable_id="tas"), role="field")
    >>> set_facets(in_cmip6_names.query)
    {'project': ('CMIP6',), 'variable': ('tas',)}
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
    reload into a known class, two spellings of one question must hash alike, and
    turning a requirement back into searches converts *from* canonical. So what comes
    back out is not the object that went in. A leaf built from
    `Query(variable="tas")` holds a `QueryCanonical` saying the same thing, so
    comparing its `query` with that `Query` gives `False`. Compare with
    [set_facets][esmporium.requirements.set_facets] rather than with `==`.

    Whatever you put in `other_terms` stays there, including a facet we do model. It
    is an escape hatch, and one which rewrites what you put in it is not an escape
    hatch. Do note that its keys are compared against each stored dataset's
    [`extra`][esmporium.requirements.CatalogueEntry.extra] rather than sent to a search
    API, so an API parameter name such as `cmip6:experiment_id` matches nothing at all,
    quietly.
    """

    role: str
    """
    Role the dataset is resolved into

    Say what the dataset is *for*, not which variable it happens to be. A role is the
    named slot the resolved dataset is filed under, and it is what a check refers to
    later, so it wants to read as a job rather than as a value.

    You choose it, freely, subject only to three rules: it cannot be empty, it cannot
    contain a `.` (that separates the nested paths which arrive with lineage and
    scopes, as in `control.field`), and no two leaves used together may share one.

    It names one *dataset*, not the analysis. The analysis is
    [Requirement.name][(m).Requirement.name] -- "pattern-scaling", say -- and there is
    one of those per requirement and many roles beneath it. `name` answers "what am I
    calculating?"; `role` answers "what is this particular dataset for, inside that
    calculation?".

    The variable is not the leaf's identity. It has two other homes, and which one it
    is in is the whole question:

    - in this leaf's `query`, several variables mean "any one of these will do", and
      the leaf still resolves to exactly one dataset;
    - in `group_by` on the [Requirement][(m).Requirement], a variable means "run the
      whole analysis once per value".

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

    A leaf which sets one of these facets differently is an error: set each facet
    once. Unlike a leaf's own query, this legitimately sets nothing at all, which is
    the default.
    """

    group_by: tuple[str, ...] = ("model", "variant_label")
    """
    Facets which define a group, i.e. what counts as one run of the analysis

    Include e.g. `experiment` or `variable` to fan out over their values, so that the
    tree is written once and solved once per value.

    The default is one run per model per ensemble member. That is not a guess at what
    is usually wanted: it is the floor below which nothing is physical, because no
    calculation can mix one model's output with another's. Across the twenty use cases
    this design was written against, every single `group_by` begins with these two and
    only ever adds to them -- most add `experiment`, one also adds `variable`. So in
    practice this field answers "what *else*, beyond model and member, splits the
    runs?".

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
        and no insignificant whitespace. That is what lets two requirements which ask
        the same thing come out byte for byte identical, which is the whole basis of
        [requirement_hash][(m).Requirement.requirement_hash].

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

        ```text
        the whole requirement        canonical_json()          this
        (tree, name, where,     ->   one exact string    ->    64 characters
         group_by, prefer, ...)      with sorted keys          "dfc9350f9bd3...".

            same question, written differently  ->  the SAME 64 characters
            anything at all changed             ->  completely different ones
        ```

        What it is for: a requirement is solved now and again later, and the useful
        question is "has this become satisfiable since?". That only means something if
        both solves were of the *same* requirement, and the fingerprint is how that is
        known -- if it matches, an improvement is new data arriving rather than someone
        having quietly edited what was asked for. It also makes a short, stable key for
        the requirement in a database, instead of storing the whole tree again just to
        identify it.

        (SHA-256 is the particular recipe used. Which one matters far less than it
        being the same one every time.)

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
        `abrupt4x.field`

        Named in full because `prefix` means something else in
        [`esmporium.search`][esmporium.search]: the `cmipN:` and collection prefixes an
        API puts on its parameter names. This one is about role paths and nothing else.

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
