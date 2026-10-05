# R3 solver review — the `# @Claude` comments in `solve.py`

Nine comments, each answered, each ending in a proposed plan. **Nothing in
`solve.py` has been changed.** Add your own notes under any plan.

Checked against commit `1fe87c7` on `solve-requirements`, by reading `solve.py`,
`tree.py`, `catalogue.py` and `db/schema.py`, and by running
`scripts/requirements_solve_demo.py`. Two claims below were tested rather than
reasoned about; they are marked where they appear.


## Decision table

Eight decisions across the nine comments (the two advice constants are one
question asked twice). Six need a code change; two do not.

| #  | Line       | Question                      | Verdict                                        | Proposed change                                               | Your call |
|----|------------|-------------------------------|------------------------------------------------|---------------------------------------------------------------|-----------|
| 1  | L135       | Is `children` the right word? | Collides with CMIP parent/child experiments    | Rename `Explanation.children` to `parts`                      |           |
| 2  | L170       | What is `subject` showing?    | The group is there, but two real defects       | Group into the leaf message; name the requirement in `explain()` |        |
| 3  | L369, L376 | Are the advice constants too smart? | Text is fine; mutating `exc.args` is not  | Move advice into `UnrecordedFacetError`, chain with `from exc` |           |
| 4  | L435       | What is `describe_query` for? | Message formatting, wrongly public             | Rename `_describe_query`, drop from `__init__.py`             |           |
| 5  | L461       | Do we need both IDs?          | Yes; neither suffices alone                    | None                                                           |           |
| 6  | L525       | Why name the ID here?         | Keep IDs, cut the guess and the unusable advice | Reword; drop "pick one by ID", which cannot be followed       |           |
| 7  | L645       | Who wrote that TODO?          | Mine, previous session; not backtracking       | Keep TODO, reword, defer to an opt-in function                |           |
| 8  | L718       | Same `prefix` as `search/`?   | No: a real clash of two meanings               | Rename `prefix` to `role_prefix` throughout                   |           |


---

## 1. L135 — is `children` the software dev name?

You are right that it collides. `children` is generic developer vocabulary for
"sub-nodes", but in this domain the word already means something specific:
CMIP's `parent_experiment_id` and the branch relationship.

`solve.py` itself already spends the word. L189 reads *"`lineages` (the chain of
parents behind each dataset)"*. So **parent** is spoken for in this module,
which means **child** is too. When R4 lands, `Explanation.children` and "the
children of an experiment" would sit in the same file meaning unrelated things.

Your own prose has the better word already. Two places call them *parts*:

- L138 — `"""Explanations of the parts, in the order they were evaluated"""`
- L108 — `"Which status a group takes when its parts fail in different ways"`

**Plan:** rename `Explanation.children` to `parts`, touching:

- the field and its docstring (L137-138)
- `render()` (L177)
- the doctest at L156-168, where the keyword appears twice
- `_eval_all_of` (L732, L737) and `solve()` (L836, L844)

A pure rename: every use is keyword or attribute access, so there is no
behaviour to change. `Explanation` is public, exported in `__init__.py`, but has
no caller anywhere else in the repo, so the cost is one search-and-replace.

> your notes: Yes, go ahead with this rename.


## 2. L170 — what is `subject` showing, and is it enough?

The model and group **are** there, on the top line of each block. What `subject`
holds at each level, from the demo output:

| Level    | What `subject` holds                | Example                                     |
|----------|-------------------------------------|---------------------------------------------|
| Group    | the `group_by` facets               | `model=ACCESS-CM2, variant_label=r1i1p1f1`  |
| `all_of` | `all_of(` + the roles below + `)`   | `all_of(tas & rsdt & rlut)`                 |
| Leaf     | the role path                       | `rlut`                                      |

Two real defects, though.

### (a) The leaf message is false when read on its own

Scene 3 of the demo prints:

    [unsatisfied] model=ACCESS-CM2, variant_label=r1i1p1f1
      [unsatisfied] all_of(tas & rsdt & rlut)
        [satisfied] tas: #3 ('CMIP6.ACCESS-CM2.1pctCO2.tas.gn')
        [satisfied] rsdt: #4 ('CMIP6.ACCESS-CM2.1pctCO2.rsdt.gn')
        [unsatisfied] rlut: no dataset matches reporting_interval=mon, variable=rlut

That last line says no dataset matches `variable=rlut`. There *is* `rlut` — for
CanESM5. The group filter is applied in `_in_group`, against the entry, so it
never appears in `describe_query(query)`. The sentence is only true if you also
read the line two levels above it, and anyone who logs, greps or pastes that one
line gets a false statement.

**Plan:** put the group into the message. `ctx.group` is already in scope, and
`solve()` at L829 builds the same string, so pull out one helper rather than
spelling it twice:

```python
def _describe_group(group: Mapping[str, str | None]) -> str: ...

# L656
f"no dataset matches {describe_query(query)} for {_describe_group(ctx.group)}"
```

Use it at L829 too, so the two spellings cannot drift apart.

### (b) Nothing names the requirement

`explain()` renders the group blocks with no header, yet `_no_groups()` at L352
*does* say `requirement {self.requirement.name!r}`. That is inconsistent on its
own terms, and it means output from two different requirements is
indistinguishable once pasted into an issue.

**Plan:** one header line in `SolveResult.explain()` before the blocks, e.g.
`Requirement 'gregory-regression', grouped by model, variant_label:`. Better
there than in each group's `subject`, which would repeat the name forty times
for forty models.

### (c) Lower value, and I would leave it

`all_of(tas & rsdt & rlut)` is a whole indent level carrying nothing today,
because the root is always an `all_of`. It earns its place the moment `any_of`
exists and the tree has real shape.

> your notes: I agree with all of the above. Keep things like all_of becuase this will be necessary in the future.


## 3. L369 and L376 — are the advice constants too smart?

Taking the question literally first: **it already is an already-defined error.**
`UnrecordedFacetError` lives in `catalogue.py` and is raised by
`CatalogueEntry.facet`. Nothing new is defined in `solve.py`. What
`_asking_about` does is append a sentence to the caught exception and re-raise
the same object.

**The text is fine; the mechanism is the smart part.** By the PR 47 standard
both constants pass — they are plain, unconditional and always true, like the
eight-line `EmptyLeafQueryError` the reviewer kept. What the reviewer cut was
*guessing what the user probably did*, and neither of these guesses. But two
things here are the kind of cleverness that costs more in review than it saves:

1. **`exc.args = (...)` is exception surgery.** It reaches into a caught object
   and rewrites its message in place. The comment at L217-222 defends it well:
   the class survives, and `facets` and `entry_id` survive. That it needs five
   lines of defence is the signal.
2. **Which advice applies is chosen by hand at each call site.** Five `with`
   blocks, two constants, paired by convention. Nothing checks that
   ``source="`prefer`"`` always arrives with `_ENTRY_FACET_ADVICE`. The
   docstring at L395-399 already concedes this.

**Plan A, recommended and minimal.** Keep both constants and keep
`_asking_about`, but stop mutating. Give `UnrecordedFacetError.__init__` two
optional parameters — `asked_by: str | None = None, advice: str = ""` — build
the sentence in the error class where the rest of that message is already built,
and have `_asking_about` do:

```python
raise UnrecordedFacetError(
    exc.facets, exc.entry_id, asked_by=..., advice=...
) from exc
```

Same class, same fields, message assembled in one place, and `from exc` keeps
the original chained. Both advice constants move to `catalogue.py`, beside the
error they belong to.

**Plan B, the plainest.** Delete the advice layer and let the catalogue's error
through untouched. You lose the "who asked" sentence — which, given those five
call sites are what make a 200-dataset failure diagnosable, I think is worth
keeping. I would take A, but that assumption is the whole argument, so say if
you disagree.

One related fact, since it is what `_QUERY_FACET_ADVICE` asserts: I tested it,
and it holds. `other_terms={'table_id': ('Amon',)}` does reach the catalogue
spelt `table_id`, and no entry knows it.

> your notes: I am still unconvinced by PLan A. COuld you describe exact situations when these two advice would be called? Right now I am leaning to Plan B, howevever I would like you to provide examples of how this could be useful for the user.


## 4. L435 — what is the role of `describe_query`?

It renders a query as one human-readable line for error messages:
`experiment=abrupt-4xCO2|abrupt4xCO2, variable=tas`. Two call sites, both inside
this module — `_no_groups()` at L348 and the unsatisfied leaf message at L656.

The oddity worth spotting is that **it is public.** No leading underscore, and
exported from `requirements/__init__.py` at L45 and L81, while `_describe_entry`
directly beneath it is private, as are `_ambiguous_message`, `_differing_facets`
and the rest. Nothing outside `solve.py` calls it.

**Plan:** rename to `_describe_query` and drop it from `__init__.py`. It is
message formatting, which is an implementation detail of the explanation, and
privacy matches the sibling sitting right below it.

The argument the other way: someone holding a `Leaf` might reasonably want to
print its effective query. But that is a public API we would be speculating into
existence, and it can be un-privated in one line the day somebody asks.

> your notes: Keep as is, no need to make private.


## 5. L461 and L525 — do we need `id_project_specific` in these messages?

Your framing — *"to us it is just another column to satisfy uniqueness"* — is
right, and it is exactly **why the ID has to stay in the ambiguity message.**
The chain:

1. `DATASET_FACET_COLUMNS` (schema L334) holds nine facets.
   `id_project_specific` is **not** one of them, and `CatalogueEntry.facet` says
   the ID columns are *"deliberately not facets"*.
2. The identity index (schema L158) is unique over `id_project_specific`
   **plus** all nine facets. So two rows identical on all nine, differing only
   in `id_project_specific`, are legal — the schema docstring says so at L350.
3. `_differing_facets` only looks at facets, so for exactly those rows it
   returns `()`. That is the branch at L525.

In that branch the IDs are **literally the only thing that differs**. Drop them
and the message says "there are 2 candidates and I can tell you nothing about
either".

So: keep them. But two things in that message need fixing.

### (a) `"which is what this usually is"` is a guess

The same species as the "write `Leaf(...)`" hint PR 47 cut. State what is true
instead: they differ only in their IDs, and `prefer` compares facets, so it has
nothing to work with.

### (b) `"pick one by ID"` is advice the library cannot honour

Tested:

    [ambiguous] tas: 2 candidates which agree on every facet, so no facet can
    choose between them: #1 ('A.tas.v1'), #2 ('A.tas.v2'). ... pick one by ID

    --- following that advice:
    UnrecordedFacetError: Cannot select datasets on id_project_specific
    (asked of entry 1): every dataset records project, model, institution, ...

There is no `prefer`-by-ID and no query-by-ID. It works only if a catalogue
happens to mirror the ID into `extra`, which nothing documents or requires.

**Plan for L526-531:** cut the speculation and the unusable advice, keep the IDs
and the one route that does work:

> 2 candidates which agree on every facet, so no facet can choose between them:
> #1 ('A.tas.v1'), #2 ('A.tas.v2'). They differ only in their project-specific
> ID, which `prefer` cannot compare. Use `cardinality='all'` to keep both and
> choose downstream, or narrow the query.

**Plan for L461, `_describe_entry`: leave it alone.** The two IDs answer
different questions and your comment at L467-469 already says why. The pairing
is load-bearing in both directions: `#id` is *"deliberately meaningless"*
(schema L179), so it tells a human nothing, and `id_project_specific` *"may not
be unique"* — CMIP5 uses one ID across several variables (schema L187) — so it
cannot stand alone either. Neither is sufficient; together they are.

> your notes: Yes go ahead with what is in green just above.


## 6. L645 — who wrote that TODO, and does it break no-backtracking?

**It is mine, from a previous session.** `git blame` puts it in
`76ff5fd "add solver"`, the commit that created the file — authored under your
git identity, but that whole file was drafted in-session, so the TODO is my
writing, not something you typed.

**It does not conflict with greedy / no-backtracking.** The distinction:

    BACKTRACKING  = un-make a choice and re-make it differently,
                    so that some OTHER part of the tree can be satisfied.
                    -> changes which dataset fills a role.  Changes the VERDICT.

    THE TODO      = the group has already failed.  Re-query with one facet
                    dropped, purely to write a better sentence.
                    -> changes no choice, no role.  Only the MESSAGE.

So the rule is untouched. Two real objections stand, though:

1. **Cost.** One extra `catalogue.find` per facet per failing leaf, on the solve
   path. Forty groups x three leaves x five facets against a database-backed
   catalogue is a lot of queries to produce prose.
2. **Altitude.** It turns `solve()` into a query *generator* that relaxes
   constraints and re-asks. That is a different job from "judge what exists",
   and belongs in its own opt-in entry point — something like
   `explain_unsatisfied(requirement, catalogue, group)` — which a user calls
   when they want to pay for the answer.

**Plan:** keep the TODO (I will not delete it) and reword it to record both of
those — that it is diagnostics only and therefore *not* backtracking, and that
the seam is a separate opt-in function rather than the solve path. No behaviour
change in this PR.

> your notes: yes, reword it to record these and potential future opt in function. Make sure solve is not a query generator.


## 7. L718 — is this the same `prefix` as in `search/`?

**No, and you are right to flag it — one word, two unrelated meanings.** In
`search/` a prefix is a **STAC collection namespace** glued onto a facet name:

    known_facade_parameters.py:449   prefix: str
                              :487   facet_name: f"{self.prefix}:{base_query_name}"
                              :598   prefix="cmip6"     ->  cmip6:variable_id

In `solve.py` it is a **role path prefix** — the `abrupt4x.` in `abrupt4x.tas`.
And `tree.py` has already made exactly this rename:
`walk_leaves(node, role_prefix: str = "")` at L829, documented at L838-841.

**Plan:** rename `prefix` to `role_prefix` throughout, matching `walk_leaves`:

- `_eval_leaf` — L634 signature, L635 `path = f"{prefix}{leaf.role}"`
- `_eval_all_of` — L721 signature, L724 recursive call
- `_eval` — L744 signature, L746 and L749 calls
- the one external call site, `solve()` L833 — `_eval(requirement.tree, ctx, "")`,
  positional, so unaffected

All three functions are private, so this is internal only. Your comment at
L718-720 then becomes the answer rather than the question, and I would leave it
in place as the record of why the name is what it is.

> your notes: yes, rename to role_prefix


---

# How solve.py fits together

## A. The two arms, and the one translation point on each

A query is translated exactly once on each arm, and `solve.py` is on neither of
those points. By the time the solver touches a query it is already canonical.

                        YOU WRITE, IN ANY STYLE
       Query(variable="tas") . QueryCMIP6(variable_id="tas") . QueryCMIP5(...)
                                    |
          +-------------------------+--------------------------+
          |                                                    |
    WRITE ARM (esmporium.search)                 READ ARM (esmporium.requirements)
          |                                                    |
          v  to_canonical()        <== ONLY translation  ==>    v  to_canonical()
    QueryCanonical                                        QueryCanonical
          |                                       stored on Leaf.query, frozen
          v  facade: from_canonical + prefix                   |  (tree.py L187,
    "cmip6:variable_id": ["tas"]  <-- THE "prefix"             |   L216)
          |                            in search/              |
          v  HTTP                                              |  ** no further
    ESGF STAC / Solr                                           |     translation **
          |                                                    |
          v  result_normalisation (strips "cmip6:")             |
    DatasetFacets --> db: Dataset rows ---------------------->  |
                            (what a Catalogue reads back)       v
                                                      catalogue.find(query)

Three facts this is making:

- **One translation per arm.** `to_canonical()`, and that is it. `solve.py` has
  no translation code at all.
- **The escape hatch is the exception.** `other_terms` keys are **never**
  translated, on either arm. Outbound they reach the API verbatim, prefix and
  all; inbound they reach `entry.facet()` verbatim. That asymmetry is the whole
  content of `_QUERY_FACET_ADVICE`.
- **`prefix` only exists on the write arm**, which is why comment 7 is a genuine
  name clash and not a shared concept.


## B. Inside solve()

    solve(requirement, catalogue)
    |
    +-- STEP 1 - DISCOVER THE GROUPS ------------ _group_values()
    |     for each leaf in the tree:
    |         query = effective_query(leaf, where)   <- tree.py: leaf query + where
    |         found = catalogue.find(query)          <- NO group filter yet
    |         collect tuple(entry.facet(f) for f in group_by)
    |     ==> {("CanESM5","r1i1p1f1"), ("ACCESS-CM2","r1i1p1f1")}  sorted, stable
    |         "40 models come out of the data rather than being listed up front"
    |
    +-- STEP 2 - JUDGE EACH GROUP, INDEPENDENTLY
          for group in groups:
            ctx = _Context(requirement, catalogue, group)
            _eval(tree, ctx, role_prefix="")   <- dispatch, else NotANodeError
            |
            +-- AllOf --> _eval_all_of
            |     evaluates EVERY child, even after one fails   <- no short-circuit,
            |     any failure -> Unresolved(_worst(...))           on purpose: the
            |     all succeed -> _merge(...)                       explanation must
            |                                                      say everything
            |                                                      that is wrong
            |
            +-- Leaf --> _eval_leaf
                  query = effective_query(leaf, where)
                  found = catalogue.find(query)        <- facet match (catalogue.py)
                  candidates = [e for e in found if _in_group(e, ctx)]
                  |                                     ^
                  |                   the group filter lives HERE, on the ENTRY,
                  |                   not in the query - which is why the
                  |                   "no dataset matches ..." message in 2(a)
                  |                   never mentions the model
                  |
                  +- 0 candidates --------------> Unresolved("unsatisfied")
                  +- >=1 -> _choose(prefer, cardinality)
                             apply prefer facet by facet, sorted(prefer)
                             |                       ^ sorted, so a requirement_hash
                             |                         always solves the same way
                             +- cardinality="one" and >1 left -> Unresolved("ambiguous")
                             +- otherwise --------------------> Resolved(roles)
          |
          v
       SolveResult        every discovered group in exactly one bucket
         +- resolved     : Mapping[GroupKey, ResolvedGroup]    <- can be run; .one(role)
         +- unsatisfied  : Mapping[GroupKey, UnresolvedGroup]  <- a dataset is missing
         +- ambiguous    : Mapping[GroupKey, UnresolvedGroup]  <- too many, nothing
                                                                  to choose between


## C. Who owns what

        tree.py                 solve.py                  catalogue.py
      WHAT IS NEEDED          DOES IT ADD UP?            WHAT EXISTS
     ---------------        ------------------        -----------------
      Requirement   ------->  the groups                 Catalogue.find
      AllOf / Leaf            _eval dispatch             CatalogueEntry
      role, role_prefix       _choose (prefer)           entry.facet()
      where                   per-group verdict          set_facets (3 homes -> 1)
      effective_query ----->  Explanation                matches
      requirement_hash
                                  |
                                  +-- raises UnrecordedFacetError
                                      (defined in catalogue.py, re-raised with
                                       "who asked" by _asking_about - see 3)

Dependency direction is one-way: `solve` imports from both `tree` and
`catalogue`; neither imports `solve`. `requirements/` must never import
`esmporium.search` (hard rule, `__init__.py` L16-29) — the shared pieces live in
`esmporium.query`, which is why a single `QueryCanonical` serves both arms.


---

# One thing you did not ask about

`catalogue.find` is called `n_leaves` times in step 1, then
`n_leaves x n_groups` times in step 2, with the *same* queries. Step 1 throws
its results away and step 2 re-asks, then filters by group in Python.

Fine for `InMemoryCatalogue`. For 40 models x 3 leaves against a database that
is 123 queries where 3 would do. The seam is `_group_values` returning
`{group: entries}` rather than only the keys.

Not this PR — flagging it because it is cheap now and awkward once the
database-backed catalogue exists. `tree.py` L757 carries a TODO in the same
spirit, about `check_where_agrees_with_leaves` discarding its combined queries.


# Open questions for you

- [ ] **Scope.** All six changes in one PR, or renames (1, 4, 7) separately from
      the message work (2, 5)?
- [ ] **Plan A or B** on the advice constants (section 3)?
- [ ] **Where the requirement's name goes** (2b): a header line in `explain()`,
      or into every group's `subject`?
- [ ] **Tests.** Do the two verified defects — the leaf message that reads false
      alone, and `"pick one by ID"` raising `UnrecordedFacetError` — get
      regression tests in this PR, or wait for the test step?
