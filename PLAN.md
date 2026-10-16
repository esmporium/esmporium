# esmporium plan

What we are building, in the order we mean to build it.

Last updated: 2026-10-15

## What changed about this document

Until now there were two plans:

- esmporium's own search-and-database track, numbered `PR1` … `PR9` (this file)
- the requirements track, numbered `R0` … `R13`, which lived in
  [esmporium-processing/PLAN-REQUIREMENTS-TO-ESMPORIUM.md][plan-req]

[plan-req]: https://github.com/esmporium/esmporium-processing/blob/plan/PLAN-REQUIREMENTS-TO-ESMPORIUM.md

The two tracks have now met. A `Requirement` is
what you search for (`search(requirement)`), what you compile into a search plan
(`to_search_plan`) and what you solve against the
database (`DatabaseCatalogue` plus
`solve`). Keeping two lists stopped making sense the
moment the order of work started interleaving them, so everything that is left is one
numbered sequence, grouped into four phases.

Every step says which old number it was (`was PR4`, `was R6`), because changelog
entries, code comments and PR descriptions refer to those. Nothing has been thrown
away: the completed steps and the original framing of each track are preserved
verbatim in the appendices at the bottom.

## Note while we are still in the development phase

We are in the developement phase and over the course of this plan we will be making changes
and updates to the database and schema. You do not need to worry at all about breaking the
API or database caused by migrations, because there are no users of this package. You have
full freedom because the plans to build up the package into a working prototype will require
breaking the current database logic. Please keep this in mind for all future prompts.

## The pipeline we are filling in

```text
        what an analysis needs                        what ESGF has
        ──────────────────────                        ─────────────

             Requirement                        ┌────────────────────────┐
                  │                             │  ESGF search APIs      │
                  │  to_search_plan             │  (Solr, STAC east,     │
                  ▼                             │   STAC west, bridge)   │
             SearchPlan ───────── search ──────►└───────────┬────────────┘
                  │                                         │
                  │                                 parsed documents
                  │                                         ▼
                  │                             ┌────────────────────────┐
                  │                             │  esmporium database    │
                  │                             │  Dataset, versions,    │
                  │                             │  raw documents, API    │
                  │                             │  health                │
                  │                             └───────────┬────────────┘
                  │                                         │
                  │            DatabaseCatalogue            │
                  └──────────────► solve ◄──────────────────┘
                                     │
                                     ▼
                       SolveResult: satisfied / unsatisfied
                                   / ambiguous
```

Everything still to do either **widens that loop** (more tables below `Dataset`: file
access, ancestry, downloads) or **sharpens the left-hand side** (more node types and
checks above `Leaf`), or **runs the loop for real** (a watched search on the server,
and then downloading what it finds).

```text
  Phase 1 ─────────► Phase 2 ─────────► Phase 3 ─────────► Phase 4
  watch what         get the data       say what we        load it, and
  changes                               need precisely     tidy up

  steps 1–5          steps 6–10         steps 11–16        steps 17–22
```

## Roadmap at a glance

| # | Step | Was | Phase |
|---|---|---|---|
| 1 | CORDEX integration test: `other_terms` from search to solve | new | 1 |
| 2 | Make `find` ~6x cheaper (`needs_facets`) | new | 1 |
| 3 | Track changes in search results | PR4 | 1 |
| 4 | Nice summaries of results and of changes | PR5 | 1 |
| 5 | A live pattern-effect watch on the server, emailing us | PR4 live tests | 1 |
| 6 | Data access tracking (`DataAccess`, `File`, `FileAccess`) | PR7 | 2 |
| 7 | Dataset ancestry | PR6 | 2 |
| 8 | `Ancestors` lineage in requirements | R5 | 2 |
| 9 | Downloads | PR8 | 2 |
| 10 | Deploy downloading off the watched searches | new | 2 |
| 11 | The `Constraint` protocol, nothing shipped | R6 | 3 |
| 12 | `Covers` | R7 | 3 |
| 13 | `any_of`, `optional` | R8 | 3 |
| 14 | `scope` and `SameTimeRange` | R9 (`namespace`) | 3 |
| 15 | `Aux` | R11 | 3 |
| 16 | Rework `prefer` | new | 3 |
| 17 | Data loading | PR9 | 4 |
| 18 | `Sibling` | R10 | 4 |
| 19 | Dataset clash handling | PR5.5 | 4 |
| 20 | Ingesting from a local source | new | 4 |
| 21 | Daily CMIP5/CMIP6 downloads, using what is already local | new | 4 |
| 22 | Clean up, including retiring the prototype | R13 + new | 4 |

Two orderings changed on purpose, and both are worth knowing about:

- **Data access tracking (step 6) now comes before ancestry (step 7).** It used to be
  the other way around, with ancestry throwing file information away and `File`
  arriving next. Doing it this way means ancestry can link a fact read out of a file
  header to the file it came from as it goes, instead of us coming back to add that
  link afterwards.
- **`Sibling` (step 18) is late, and `Ancestors` (step 8) is early.** Ancestors is
  what the live searches need (parents back to `historical`); siblings are not needed
  until the ERF use cases.

## Done so far

The search-and-database track:

| Was | What landed | PR |
|---|---|---|
| PR1 | Search, against all our query classes and APIs | [#14](https://github.com/esmporium/esmporium/pull/14) |
| PR1.5 | Search API health recording | [#16](https://github.com/esmporium/esmporium/pull/16) |
| PR1.7 | Picking an API using recorded health | [#18](https://github.com/esmporium/esmporium/pull/18) |
| PR2 | Parsing results into `Dataset`s | [#25](https://github.com/esmporium/esmporium/pull/25) |
| PR2.2 | Tests that `other_terms` reaches the request | [#37](https://github.com/esmporium/esmporium/pull/37) |
| PR2.5 | Pagination | [#40](https://github.com/esmporium/esmporium/pull/40) |
| PR3 | The `search` entry point, and parallelisation | [#41](https://github.com/esmporium/esmporium/pull/41) |
| PR3.3 | Splitting `search.py` into a package | [#42](https://github.com/esmporium/esmporium/pull/42) |
| PR3.5 | Concurrent saving, dataset clashes under races | [#43](https://github.com/esmporium/esmporium/pull/43) |
| — | Facade fixes, `get_n_matches` moved onto the APIs | [#49](https://github.com/esmporium/esmporium/pull/49) |
| — | Keep both ESGF-NG mirrors' documents (`search_host`) | [#54](https://github.com/esmporium/esmporium/pull/54) |

The requirements track:

| Was | What landed | PR |
|---|---|---|
| R0 | The design note, [docs/further-background/data-requirements.md](docs/further-background/data-requirements.md) | [#44](https://github.com/esmporium/esmporium/pull/44) |
| R1 | Entries, facets and matching (`CatalogueEntry`, `Catalogue`, `matches`) | [#46](https://github.com/esmporium/esmporium/pull/46) |
| R2 | `Leaf`, `all_of`, `Requirement`, `.where()` | [#47](https://github.com/esmporium/esmporium/pull/47) |
| R3 | `solve` — the skeleton closed | [#48](https://github.com/esmporium/esmporium/pull/48) |
| R4 + R12 | `search` takes a `Requirement`, `to_search_plan`, `DatabaseCatalogue` | [#51](https://github.com/esmporium/esmporium/pull/51) |

`PR3.6` and `PR3.7` are gone from the list of work: `PR3.6` was "go and do the
requirements plan", which is what `R0`–`R4` were, and `PR3.7` was "use `Requirement`
in search", which is what `R4` did. `PR3.7`'s one remaining idea — auto-generating a
requirement's name with a timestamp — is no longer wanted:
`Requirement.name` is required and chosen by
the user, so there is nothing to generate. What is left of `PR3.7` is the name-clash
handling, which is part of step 3.

---

# Phase 1 — watch what changes

The loop in the diagram above closes today, but nothing yet *remembers* an answer, so
nothing can tell us what changed. This phase makes a search's results comparable over
time, makes the comparison readable, and then points it at real data on the server.
Steps 1 and 2 come first because they are small and because step 1 covers a path
(`other_terms`) that steps 3–5 will lean on.

## 1. CORDEX integration test: `other_terms` from search to solve

*New step.*

`other_terms` is the one path where a facet travels from the user to the search API
with no translation and no checking, and it is now also a path through grouping
(`group_by_other_terms`),
tie-breaking (`prefer_other_terms`)
and matching (both catalogues). [#37](https://github.com/esmporium/esmporium/pull/37)
checked only the first hop — that the same request gets built — and unit tests cover
each later hop separately. Nothing yet runs the whole length of it.

CORDEX is the honest test case, because `cordex-cmip6:driving_source_id` is a real
facet that no `Dataset` column holds and that no query
class names, which is exactly the situation `other_terms` exists for.

- [ ] add an integration test that searches for CORDEX data with a leaf constrained
      only through `other_terms`, saves the results, and then solves against
      `DatabaseCatalogue`, grouping on
      `cordex-cmip6:driving_source_id`
    - the assertion that matters is that the driving model reaches the solver's group
      key, having been read back out of a stored search document rather than a column
    - mark it opt-in, like our other network tests
- [ ] while we are here: check the value of `project` CORDEX data actually comes back
      under on each facade, and that our facade selection sends a CORDEX query
      somewhere that answers

This step is also a demonstration of the expensive shape named in step 2: a leaf
constrained only through `other_terms` cannot become SQL, so it pulls every row the
versions filter allows, and every stored document with it.

## 2. Make `find` ~6x cheaper (`needs_facets`)

*New step.* The note this comes from is in
`src/esmporium/db/database_catalogue.py`, above `_documents_by_dataset`:

> The cost is one fetch plus one JSON parse per stored document per `find`, and it is
> the bulk of a `find`: measured over 20,000 datasets with one 57-key Solr document
> each, the fetch-and-parse is ~59% of the time and flattening ~25%, so skipping the
> documents entirely would be roughly a 6x saving. We cannot skip them from here,
> because `Catalogue.find` is given a query and not the `group_by`/`prefer` which
> decide what else will be read. Giving it that — e.g.
> `find(query, *, needs_facets=...)` — is the optimisation to reach for, and it is a
> change to the protocol, so it is not done here. Note also that `solve` calls `find`
> once per leaf and then once per leaf per group, so the repetition matters more than
> the parse does.

A 6x speed-up is worth having now rather than later, because every step from here on
runs `solve` more often and over more rows: step 3 solves the same requirement
repeatedly to diff the answers, step 5 does it on a schedule on the server, and
phase 3 adds node types that each solve more leaves.

- [ ] add `needs_facets` to the `Catalogue` protocol
      and to both implementations. `InMemoryCatalogue` can ignore it; the point is
      that `DatabaseCatalogue` can skip the document fetch and parse entirely when no
      document-only facet is wanted
- [ ] derive what is needed from the requirement (the leaf's query, `group_by`,
      `group_by_other_terms`, `prefer`, `prefer_other_terms`, and whatever
      `versions` needs) and pass it down
      from `solve`
- [ ] deal with the repetition too, since the note says it matters more than the parse
      does: `solve` asking the same `find` once per leaf and then once per leaf per
      group is work we should be able to do once

Tests:

Unit tests:

- [ ] a catalogue spy asserting that no documents are read for a requirement that
      needs no document-only facet, and that they are read when one is wanted
- [ ] the same requirement gives the same `SolveResult` whether or not the documents
      were skipped — this is the test that stops the optimisation changing answers

Not a test:

- [ ] a demo script we can run against a populated database to confirm the saving is
      real, in the style of `scripts/requirements_solve_demo.py`. Numbers in the PR
      description, not in an assertion

## 3. Track changes in search results

*Was PR4.*

Tracking changes in search results over time by tracking `Requirement`'s in the database

In the database, if a user tries to perform two searches which are the same `Requirement`, but they only differ by name,
the user should be able to choose what happens:
a) the name of the search is overwritten in the database
b) an error is raised
c) the search goes through but the old name is kept.
Make raising an error the default: we don't want people losing the ability to find the old name of their search by accident
and we don't want to support multiple names for a given search. Use an Enum (maybe best `StrEnum`) to represent the different options.

Check that we can track searches (i.e. `Requirement`'s) in our database
Check that we can repeat a search, given its `Requirement`'s ID or name (a user set thing) alone
Check that we can identify when, for a given search (i.e. `Requirement`),
metadata associated with a Dataset changes e.g. a Dataset goes from being not retracted to retracted

`canonical_json` and
`requirement_hash` (from R2) are the
primitives this is built on: the hash is the identity a stored requirement is found
by, and the name is the label a human uses. "This requirement became satisfiable" is
the difference between `solve` at t1 and `solve` at t2, so the snapshot of a solve is
the other thing worth storing.

This is also the step to consider the ramifications of search results returning different facet values for the same facet (i.e. the reason we include search_host)
Need to think about this though... perhaps that is more for different hosts than different search on the same host...

Tests:

Unit tests:

- [ ] handling of clashes in names (unit test because this logic test should be able to be done without a database connection if we get the abstraction right)
- [ ] passing of clash handling parameters from `search` down to low level functions (use mocking so this test is only checking parsing, not other behaviour)

(maybe we'll move some of the integration tests below in here if we can make them fast enough)

Integration tests:

- [ ] update existing tests to handle `search`'s new API
- [ ] searching with two requirements that only differ by name raises by default (parsing and other behaviour handled above)
- [ ] `search` tracks the search (i.e. `Requirement`) in the database
- [ ] we have a `rerun_search_esgf`, which works based on the requirement's name or ID alone (not sure if we need a specific function for this or we just make `search` handle requirement names, let's think about it). Re-running records the information required to see how dataset entries have changed over time
    - there's probably quite a few cases to consider here e.g. new dataset version, version goes from not retracted to retracted, dataset is no longer available (should never happen, because ESGF returns retracted and not retracted results by default, but it might look like data disappeared if a user set retracted=false in their search)
    - all of this tracking behaviour should also work if we just call `search` again with the same requirement (i.e. there should be some common path between the two, if we keep `rerun_search_esgf` and `search` separate)

## 4. Nice summaries of results and of changes

*Was PR5.*

Helpers for making nice summaries of search results and changes in search results over time.

Let's put these helpers in esmporium, they're cheap and useful to others.
Let's drive their development based on what we see in the 'live' watch above (step 5).
I expect we'll just want to be able to convert search results to pandas' dataframes
because then we can just use tools like https://pypi.org/project/pandas-diff/
(although maybe there is a smarter, database native way to do this rather than going through pandas...).
Then we'll also have nice formatters, which initially we'll just put in our 'live test' repo
but we can move out into `esmporium` core when we're happy with them.

Tests:

Unit tests:

- [ ] Start from whatever intermediate changes object our low-level functions return (which I assume we'll need to have) and then run the formatter and check the formatted output
    - I would do this with pytest regressions by just saving the formatted output to a file, that is the easiest way to check output and changes over time in my experience (better than hard-coding strings to compare to in tests)
- [ ] parsing of formatting and other options through the higher-level API

Integration tests:

- [ ] Pre-load a database, then get the summaries
    - this is the kind of function we would use in our 'live tests' repository
    - this test is testing the actual function we care about: given I have a database, get me the summary
    - here we only test one path through (e.g. our default)
    - the unit tests then cover all the formatting options and behaviour and parameter parsing so we end up being confident that all the functionality works, without having to run lots of expensive integration tests

## 5. A live pattern-effect watch on the server, emailing us

*Was PR4's live tests, narrowed to one search.*

Set up a search for pattern effect on the server and start emailing ourselves and see
what happens. One search, running for real, is what will tell us whether steps 3 and 4
produced something useful — and it is cheap to extend once it works.

- [ ] set up a new repo and, on the CMIP6 server, set up the pattern effect search as
      a 'watched' search. Once a day, re-run it. If there are changes, email you and me
- [ ] see what happens: the formatters in step 4 should be driven by what these emails
      make us wish for

Then: we will add other searches as our requirement specification functionality
improves. The queue, in the order the requirement language will make them expressible:

- [ ] Pattern scaling search: monthly tas for historical plus any of the `scen7-` experiments
    - email you, me and Spencer
- [ ] Malte search: CMIP7 daily and monthly for any experiment for the following variables: clt-evspsbl-hurs-huss-mrso-pr-psl-rlut-rsds-rsdt-rsut-rtmt-sfcWind-tas-tasmax-tasmin-ts-uas-vas
    - email you, me and Malte
- [ ] Gregory search: (you know this one)
    - email you and me
- [ ] Gang's search: (need to ask him)
    - email you, me and Gang

---

# Phase 2 — get the data

Everything in phase 1 is about metadata we already have. This phase adds the rows
below `Dataset` that say where the bytes are, how datasets are related to each other,
and what we have actually pulled down — and then starts pulling it down.

## 6. Data access tracking

*Was PR7.* Note the swap: this now comes **before** ancestry (step 7), so that
ancestry can record which file a fact came from while it is reading it, rather than us
adding that link afterwards.

Adding File i.e. data access tracking

Note that I think we should call this data access rather than file in preparation for the day when some data is made available by e.g. zarr archives, which aren't really 'files' in the way we normally think about it.
This probably means we'll need a DataAccess table with a type column, and only 'file' types have links to a `File` table (which then has further links to a `FileAccess` table). Other types would have different access patterns and information.

Use the old mermaid diagrams to help us think through the objects and tables we want (https://mermaid.live/edit#pako:eNqVV2- [ ]2zYQ_iuEvsTBbCOxncRxsAFBg67F2ixogg0YAgg0dbLZSKRKUmlcJ_99d5TkyBHtrf4kkvf63KvXkdAJRLMIzJXkC8Pze8XwdwvciOWXUrHn58FArxl-foZ8DsYuZcFmzIDQJrE7qK-44xbcuyVXC0BqofOidFCT169viP9C2VIrpL6Pltyyx-ps76MtroZsm_mTFtw13BbMIyQMTwqd-w8B72UGG50pHvbSP4esLbgB5eLa4PgBVo0IL7yl6FIIsNYzJfq7yjRP-BxJUqPzLk9Q2RJ4AoYVyKEdeuk0Mb6Nw7q6oJ9UjsmE3fzxemWdkWrBbAEi_mpR8H2ktMl5Ji1K_FaCWV2wRKYpQyglWNZyqcUPKik0io9Lk23rU2Uep7pUSffaOm0g6driuCvt63XCHTiZAxMG8DOJuevwOL5Ay3VBgecZEzzLEJmMzyFjPQUYD0zTb6VEfYeN-S_bKbjuCM25dWBijxhKr4M6YJfXVz6dBh-vEyjQdQx5ABOMy1cQXVulsk66kkxF2V33dWkEhF7gqcAY5JRggddHbiTHJ-90-BXzK8Tpdj0sjEx2yEsJTlBi1XlROpcYg9iA1Zn3MgR3k8frIDxceQQq3H9tBeKXphcE8E4qyVR07D0xDn5rtA03EgJ8tUQqREyv-vYCEcskXSQMi9qCL06nGff52GepNhgq45C2LXOudUYpbhE37HJuO-tJqfwBnUusBuynsU7jqu10UqnTVt56WOM5bKPXu3VQsMkFahHZsPHzMJSqlfytBCM8QGT4krCq0wy0ylYsB8cJatajVsXuPny8bUQfVG1zj4b9SVoT7a6AmoALJx-lW4VI5gbdX8bUMGKp4oqjQ1U5FJMHHnIPKY4LnTqGoFLjHcrkgmr4ERQBynT62mkbDMKtZDOB1rtyzavzyY1h3BvEHXkeU_8JNXKwizRuKsEH0S2h6lbU42UqBY2AXkvFM70eBpLYQJFJwcM6CGAUkRc09TA3vEGvtwG7Df_ejJgvl3-zDVNtLQ4YGlks0YL1HgAKy8h0eMJooym2zNzb1u3n47pTTU3jsKUxeoFleMEUjhSDg4Fw77WC0PcJq3geTNqfq7eAgEY4skqaEohmxrgwGid_ja5tswV7RC1LLEE82DLvtm_DxQN-hKqhznPunLEN-IQqGYbVusj0nCzCZzmnrYz1DkK1fhBCp11D3G8zgSqq1hysJXTblTiWV-g438JqM99rgfS-mfGtSNcb0_-IdwDSTZE3caxLfF99dUczbpNSUDQ_3N3d3NJuadgz-_MGrq8ub_Drd8SztAGZW2tRkxzWb134hAKrw0DjOlGtgbiE_WxJeh99zW_V-_5SxM_XUiQJrTrclFvUj2gXiGbOlNCPcsANkY6RD4ZPqRxR98ssNw_ERzwFV_9onTdsRpeLZTRLeWbxVBYU-PqvxoaEtinzDvdFF83OpudeRjRbR0_RbDA6Hw8no9Px6Gg8PR-NJuN-tMLr8dl0OB6fjKfjk-Oj0dHR5KUf_fBqj4fj6en5-PTkaHI8GZ-Npmf9CBKJa-fn6q-O_8fTj3jp9O1KicqKl38BECNB5A).

We want to avoid lots of project-specific fields if we can, but some might just make sense to keep (more tables, more problems, but maybe necessary).

Tests:

Unit tests:

- [ ] we can parse ESGF1 and ESGF-NG's raw JSON information about files into DataAccess, File, FileAccess etc. objects

Integration tests:

- [ ] Do a search query that gets file information from an ESGF API, assert that the expected data access information and links appear in the database
    - do this on all combinations of ESGF-1 ESGF-NG and CMIP5/CMIP6/CMIP7. Yes, that's more expensive, but it's also the key test so we are happy to pay that price

## 7. Dataset ancestry

*Was PR6.* The "Not included: the File class, we will add it next" note that used to end
this step no longer applies — step 6 landed first, so file information is already
there and a fact read out of a header should be linked to the file it came from as it
is read.

Finding dataset ancestry

Note that this is currently huge.
Before we start implementing, let's think about how to split this up, perhaps:

1. functionality to find a file that belongs to a dataset
1. functionality to retrieve the header of a file (also includes tracking of node health for this retrieval and the same 'selector' idea as we have for search)
1. addition of information only accessible from file headers to dataset entries in the database (with the link between that information and the file it came from, which step 6 has made possible)
1. generation of a 'parent query' from a dataset in the database
1. (not needed, but noting): executing a parent query search and parsing results
1. linking parent and child datasets in the database
1. recursion i.e. adding ancestry back to a given level/experiment
1. caching/fast-path i.e. ensuring that we don't query again if we have information already or can get information from another dataset in the database (e.g. if we know parent for tas, we can use that to generate the parent query for rsdt, we don't need an rsdt file)

Also keep in mind that the above is very ESGF1 specific, the flow will likely be quite different for ESGF-NG so we'll need to think about that as we go too.

Will require adding the parent information to our dataset version table
Will require adding the file header query logic and parallelisation and node selection and health monitoring too
Will require adding some concept of fixes (initially do it in this package e.g. `src/esmporium/fixes`, can split out later) so we can inject corrected metadata on the fly

Tests:

Unit tests:

- [ ] we can navigate the ancestry in different cases
    - this is the key one because we should be able to cover lots of cases because the tests will be very fast as we will mock out all the calls to search and fetch file header etc.
- [ ] tests of file header retrieval that can use mocking
- [ ] tests of node health monitoring that can use mocking
- [ ] tests of node selection that can use mocking
- [ ] tests of node selection passing from high level down to low-level functions

Integration tests:

- [ ] start with a dataset, get its ancestry up to a given point, assert that all the expected parents and links end up in the database
    - [ ] key cases to cover
        - [ ] CMIP5 abrupt4xCO2
        - [ ] CMIP5 scenario
        - [ ] CMIP6 abrupt4xCO2 (pick a simulation with broken parent information here to test the fixes path)
        - [ ] CMIP6 scenario (or maybe even better, g6solar)
        - [ ] CMIP7 abrupt4xCO2
        - [ ] CMIP7 scenario
        - [ ] CMIP7 scenario extension when available
    - [ ] this will be very slow, because it will need to do lots of live API calls (ok, just make sure it is marked)
    - [ ] use parallelisation approaches and nodes that seem to be stable, so this test is as stable as possible
    - [ ] also assert that node health stats that we expect end up in the database
    - [ ] also assert that dataset information is updated as we expect
    - [ ] also assert that information which came out of a file header is linked to the file it came from
- [ ] 'fast path' testing i.e. pre-load a database with a dataset that already has all the parent information, then
    - [ ] test that if we ask for parent information to be added to this dataset again, no queries are actually done because all the information is already there
        - [ ] I don't think we need a `--force` flag that forces the queries to be re-run because the files attached to a dataset cannot be changed on ESGF without making a new version (and all of this parent stuff is version specific). This is one to check. A force flag isn't that expensive so maybe we just include it anyway, although it then means we have to track changes in parent information over time, which is tricky, so on second thoughts let's not include force for now, and we just note that this could be an issue in our docstring and wait and see if it is an issue before doing anything else.
    - [ ] test that if we ask for parent information of a dataset that should have the same parent information, no queries are done because we just use the parent information from the 'other' dataset instead
        - e.g. if we know parent information for tas, then we can get parent information for tos by just looking at the tas info, we don't need to re-query ESGF and read a file header
        - this tests the key optimisation that we need for large-scale parent attachment
        - we should note in our docstring clearly that this all relies on the assumption that we can do this 'cross-dataset' parent information jump. If we can't, the logic of this 'fast path' falls over (and then we'll need to introduce a `do_not_use_cross_dataset` flag or something, but again let's wait before doing that)

## 8. `Ancestors` lineage in requirements

*Was R5.* Step 7 puts parent links in the database; this is how a requirement asks for
them.

- `relations.py` (new): `RELATION_MODEL_CONFIG`, `Ancestors`, and `Lineage` with `index_of`.
  `Lineage` lives here rather than in `constraints.py` (which does not exist yet) because it is
  the resolved form of `Ancestors`/`Sibling` and solving needs it before any constraint exists;
  `constraints.py` imports it from here at step 11.
- `catalogue.py`: `Catalogue.parent_of`, `InMemoryCatalogue.parents`, reference checking in
  `__post_init__`, and the `DatabaseCatalogue` implementation reading the links step 7 stored.
- `tree.py`: `LineageRelation` (one member for now), `Leaf.lineage`, `.with_lineage()`,
  `RESERVED_ROLES`, the leaf-role-versus-lineage-role validator, lineage entries in
  `role_paths`.
- `solve.py`: `_lineage`, `_ancestor_lineage`, `Resolved.lineages`, lineage role paths in
  `_eval_leaf`.

Role paths gain their second level here: `control.tas`, `chain.0.tas`.

Tests: `test_branch_chain_back_to_picontrol`, `test_bell_is_rooted_in_esm_picontrol`,
`test_lineage_role_names_where_it_stopped`, `test_catalog_rejects_unknown_references`,
`test_leaf_role_cannot_be_its_own_lineage_role`,
`test_role_names_are_free_unless_a_lineage_uses_them`,
`test_machinery_role_names_are_always_reserved`, no parent recorded, `max_depth` exhausted.

Use cases: `15-pattern-scaling`, `17-etccdi` — both have a lineage and no constraints.

## 9. Downloads

*Was PR8.*

Downloads
Tracking node health
Auto-preferencing nodes (but overridable by users)

Maybe too big and should be split, perhaps:

1. downloading a single file in isolation
1. tracking node health
1. parallelisation and (re-)queueing (will also require a basic selector function)
1. auto-selecting nodes based on health (fancy 'selector' function)

Tests:

Unit tests:

- [ ] parsing of parallelisation and node preferencing options from high- to low-level functions
- [ ] automated node preferencing logic
    - e.g. when a node is marked as dead, what controls whether one node is picked over another
    - we should be able to do this with a mock node health table/object so it is only a test of the preferencing logic, free from actual database calls
- [ ] preparation of download calls
    - my assumption is that we'll a flow something like: 1) specify the data of interest (maybe using a query object, maybe something else) 2) find all the datasets in the database that match 3) identify datasets that aren't available locally 4) put those datasets in a queue 5) do the downloading 6) update the database with the local download information
    - we can test the flow up by mocking out step 5, so we should be able to make this test very fast and cheap but still check all the key pieces of logic along the way
- [ ] handling of node failures and other responses within the download call
    - mock out the actual node responses with specific behaviour (e.g. a node that fails after a given amount of time, a node that returns a 404)
    - make sure that the behaviour is handled correctly
    - should be able to keep this as an integration test, as we can just adjust the failure tresholds and 'slow response' times together so the test only waits e.g. 1 second before declaring a node as a failure but we have confidence about this 'identifying failing node' and config (so have at least some confidence that a failing node can be handled)

Integration tests:

- [ ] actual downloading in parallel
    - [ ] populate a database with say 4 entries, then call download in a way that should only download two of them
    - [ ] pick files/datasets we know are small (e.g. areacella files or setting this up in a way that means we only need to download two years of EC Earth data, which is in one year files that are tiny)
    - [ ] actually download them
    - [ ] check that the data is there, the database state is as expected etc.

Not included:

- integration tests that hit a node we are confident is failing. I hope we can get the confidence we want from unit tests and can avoid having a live 'node failure wait' test. Let's make a note that we may want to revisit this decision in the test file

## 10. Deploy downloading off the watched searches

*New step.* Step 5 watches; step 9 can download; this is the two wired together on the
server, triggered off what the watch finds.

We're going to be a bit stupid about this and just do a clean archive. The reason is that the logic for ingesting data that is already available locally will be tricky and I don't want us to get delayed worrying about that headache: downloading is not that expensive and we have to do it for CMIP7 anyway. We just need to be a bit careful to not re-download anything really expensive i.e. let's not start downloading any daily CMIP6 data until we have the 'ingest from local' functionality working (steps 20 and 21).

As soon as this and downloading lands, I would like to spin up and start doing tracking and downloading for scenario tas
(but only following parents back up to historical, not piControl):
I'm going to need it for a paper and it's a nice simple use case.

- [ ] download data for the live searches in step 5 (except for Malte's daily data,
      let's check with him before we start downloading anything as we might actually
      run out of space if we do that)

---

# Phase 3 — say what we need precisely

This is the rest of the requirement language. Three things make these steps cheap, and
they are why the phase can run as a straight line:

- **The node types are purely additive.** The `Node` union has two members today
  (`Leaf`, `AllOf`). Each new node type is one more entry in that union plus one more
  branch in four small dispatch functions (`apply_to_leaves`, `walk_leaves`,
  `role_paths`, `_eval`). Nothing already written gets reshaped.
- **The design note is already in the docs** ([data-requirements.md](docs/further-background/data-requirements.md)),
  so each step can be read against a stated target.
- **The use cases grow step by step.** `tests/unit/requirements/use_cases.py` has one
  case today and each step adds the ones it unlocks, so every step ends with "and
  these real use cases now build and solve".

One thing has changed since this was written, and it affects every step below:
`to_search_plan` already exists (it landed with R4). So a new node type now lands in
**three** places, not two — the tree, the solver, and the search plan — and each step
needs the `to_search_plan` branch and test that the old R12 would have added at the
end. The old R12's `test_gcmagicc_includes_every_alternative_and_optional`,
`test_erf_includes_sibling_query` and `test_carbon_closure` are the shape of those
tests; they belong to steps 13, 18 and 14 respectively.

## 11. The `Constraint` protocol, with nothing shipped

*Was R6.*

- `constraints.py` (new): `Pass`/`Degraded`/`Fail`/`Outcome`, `GroupView`, the `Constraint`
  protocol, `NotAConstraintError`, `NotSerialisableConstraintError`, `_load_constraint`,
  `_serialise_constraints`, and the `Constraints` annotated field type.
- `catalogue.py`: `MetadataUnavailableError`, `Catalogue.metadata`,
  `InMemoryCatalogue.metadata_by_id`, and the `DatabaseCatalogue` implementation (which is
  what phase 2's file information unlocks).
- `tree.py`: `Leaf.constraints`, `.with_constraints()`, `Requirement.constraints`.
- `solve.py`: `_evaluate_constraints`, the `undetermined` status.

**Ship no constraint implementation in this PR.** The design note's central claim — constraints
are a pluggable protocol and the solver never special-cases `Covers` — is *provable* here,
because `Covers` does not exist yet and the only constraint under test is the user-defined
`AtLeastNDatasets`. This is the strongest single argument for doing any of this bottom-up, and
the PR description should say so.

Tests: `test_user_defined_constraint`, `test_missing_metadata`,
`test_missing_metadata_is_undetermined_not_passed`, `test_not_a_constraint`,
`test_constraint_which_is_not_a_pydantic_model`, `test_round_trip_user_defined_constraint`,
`test_with_lineage_and_with_constraints_reach_every_leaf`.

## 12. `Covers`

*Was R7.*

`constraints.py`: `TIME_RANGE`, `BRANCH_TIME_IN_PARENT`, `_as_year`, `_as_time_range`,
`Covers`, `_covers`. Carry the prototype's in-code note that branch-time handling needs to
become clearer, and easy for users to override, since recorded branch times often have bugs.

Tests: `test_covers_calendar`, `test_covers_branch_needs_ancestor`, `test_covers_needs_a_lineage`,
`test_covers_rejects_malformed_metadata`, `test_degraded_when_ideal_padding_not_met`,
`test_branch_chain_control_must_cover_child_and_parents`,
`test_end_and_the_lineage_role_name_mean_the_same_thing`.

Use cases: `01-tcr`, `03-tcre-flat10`, `04-tcre-1pct`, `05-zec-flat10`,
`11-tas-scenario-anomalies`, plus `TO_CONTROL`, `CONTROL_COVERS` and `CONTROL_IDEALLY_COVERS`.

## 13. `any_of`, `optional`

*Was R8.*

- `tree.py`: `AnyOf` (`min_length=2`), `OptionalNode`, `any_of`, `optional`, and the new
  branches in `apply_to_leaves`, `walk_leaves` and `role_paths`.
- `solve.py`: `_eval_any_of`, `_eval_optional`, `choices`, `notes`.
- `planning.py`: the `to_search_plan` branches, and
  `test_gcmagicc_includes_every_alternative_and_optional` — every alternative and every
  optional has to be searched for, because we cannot know which one exists until we look.

Two things become visible here and belong in the PR description. First, the third kind of "or"
— alias, fan-out, alternative — is complete. Second, **ambiguous and undetermined results are
never skipped**: `any_of` stops at them rather than trying the next alternative, and `optional`
passes them on rather than treating them as absent, so a question we cannot answer is never
silently read as "no".

Tests: `test_alternative_chosen_in_order`, `test_alternative_applies_to_whole_lineage`,
`test_ambiguity_is_not_skipped_by_any_of`, `test_optional_run_with_too_short_control_is_absent`,
`test_optional_run_with_long_enough_control_is_used`,
`test_duplicate_roles_allowed_across_alternatives`.

Use cases: `06-zec-1pct`, `07-zec-bell`, `08-flat10-cdr`, `12-gcmagicc`, `18-amoc`.

## 14. `scope` and `SameTimeRange`

*Was R9, where it was called `namespace`. Let's settle on `scope` unless the code reads
better the other way when we get there.*

- `tree.py`: `Scope`, `scope()`, the name validator, role prefixing.
- `solve.py`: `_eval_scope` and the scope-scoped `GroupView`.
- `constraints.py`: `SameTimeRange`.
- `planning.py`: `test_carbon_closure` — a scoped requirement compiles to the same
  searches, with roles prefixed.

The third attachment level lands, completing the design note's table of where a check goes and
what its failure costs. Worth restating that there is deliberately no "one experiment" node:
that was four separable things in a trench coat — shared facets, a lineage, scoped checks and a
role prefix — and each now has one home.

Tests: `test_scopes_prefix_roles_and_can_nest`,
`test_scope_carries_cross_leaf_constraints`,
`test_cross_leaf_constraint_degrades_when_periods_differ`,
`test_cross_leaf_constraint_fails_when_periods_are_disjoint`,
`test_cross_leaf_constraint_is_scoped_to_its_scope`, and `test_round_trip_and_hash`
parametrised over `ECS` and `CARBON_CLOSURE` — the first requirements rich enough to be worth
round-tripping whole.

Use cases: `02-ecs`, `16-carbon-closure`, `16a-carbon-calibration`.

## 15. `Aux`

*Was R11.*

- `relations.py`: `MatchLevels`, `STRICT_MATCH`, `Aux` (`role_name`, `required`, `match`,
  `via`, `also_for_lineage`) and its validators.
- `catalogue.py`: `Catalogue.linked`, `InMemoryCatalogue.links`.
- `tree.py`: `Leaf.aux`, auxiliary role paths, duplicate-auxiliary-role detection.
- `solve.py`: `_aux`, and auxiliary resolution in `_eval_leaf` including `also_for_lineage`,
  required-versus-optional, and the fallback-level note.

Switch the `atmos`/`land`/`ocean` test helpers on to their real `Aux` lists; the
twenty use cases are untouched. Record in the PR description that `via="link"` is where all of
this should end up — esmporium links once at ingestion from each file's `cell_measures`
attribute, so every analysis reads the same answer instead of redoing the matching — and that
`via="match"` is the fallback for whatever is not linked yet.

Tests: `test_fx_not_matched_by_default`, `test_fx_matched_with_opt_in_fallback`,
`test_fallback_still_requires_same_grid`, `test_aux_via_link`, `test_fractions_are_not_linked`,
`test_land_and_ocean_fractions`, `test_missing_required_sea_fraction`,
`test_aux_only_for_the_anchor_dataset`, `test_aux_defaults_to_strict_match`,
`test_aux_needs_role_for_several_variables`, `test_aux_match_accepts_project_specific_facets`,
`test_duplicate_aux_roles`, `test_aux_needs_at_least_one_match_level`.

Use cases: `13-energy-balance`, `19-sea-ice`. All twenty now build and solve.

If this runs long, split at the `via` boundary: first `via="match"` with match levels and
required-versus-optional, then `via="link"` and `also_for_lineage`.

## 16. Rework `prefer`

*New step.*

Fix up how we handle prefer. This needs to change from only being prefer with facets, to being more sophisticated. At minimum, prefer needs to be able to express the idea of preferring specific versions of datasets. Ideally, we should be able to pass an arbitrary function to prefer to so we can decide how to prefer, given the list of candidates. Giving this much flexibility will make the database handling trickier. For now, my instinct is to handle it how we handle normalisation. Write a string that identifies the prefer function in the database. Force the user to provide a `prefer_registry` so we can get the right function back at run time.

Concretely, that means:

- [ ] `prefer` can rank a dataset's **versions**, not just its facets. Today
      `Requirement.versions` says which
      versions *count* (a filter) and `prefer` says which candidate *wins* (a ranking);
      preferring a specific version falls between the two and neither can express it
- [ ] `prefer` can be an arbitrary callable, given the candidates and returning the
      ranking, with the facet-mapping form kept as the easy case
- [ ] stored the way normalisation is stored: the database holds a string naming the
      function (see `DEFAULT_NORMALISERS` and `NormaliseFunc` in
      `esmporium.search.result_normalisation` for the pattern we are copying), and the
      user supplies a `prefer_registry` at run time to turn that name back into a
      function
- [ ] decide what this does to `canonical_json`
      and `requirement_hash`: the hash is a
      requirement's identity for change tracking (step 3), so what the hash covers when
      `prefer` is a named function needs stating, not assuming
- [ ] `prefer_other_terms` keeps working on the same terms as now (unchecked,
      applied after the modelled facets)

This lands after `Aux` because by then we know what the candidate lists actually look
like in every node type, which is what an arbitrary `prefer` function has to be handed.

---

# Phase 4 — load it, and tidy up

## 17. Data loading

*Was PR9.*

Data loading

This needs to be thought through more carefully before we get started.

- overall loading flow
- ability to override intermediate steps
- linking with `esmporium-fixes` package
    - make this package in this repository initially (just make it in a path like `src/esmporium/fixes`)
    - we will split out into a separate repository later
- live test is setting up our gregory and other calculations on the CMIP6 server
    - this will be what really drives our testing strategy, as only once we try and do this will we see the convenience functions that we actually want
- the step of then passing our results out to other places e.g. sharing them via a web site is a separate concern. We'll go and speak to Jared about REF integration (or not) before making decisions there. However, I would like our archive of processed CMIP7 (and CMIP6 and CMIP5) output to start building up on the server while we think about this 'public facing' option as it will take a while to download and crunch all this data.
- probably implement using the same type annotation trick or something similar so the user has a way to specify to esmporium what groups it wants and how it wants its data loaded. Might need to express that in a class too/instead (that matches some interface/protcol). We should write up some pseudocode before we start implementing.

Tests:

Unit tests:

- [ ]

Integration tests:

- [ ]

## 18. `Sibling`

*Was R10.*

`relations.py`: `Sibling`, `sibling_query`. `tree.py`: `LineageRelation` becomes
a real discriminated union on `kind`. `solve.py`: `_sibling_lineage`.
`planning.py`: `to_search_plan` learns to emit a sibling's query —
`test_erf_includes_sibling_query`. (The old plan had `sibling_query` living in
`relations.py` so that `compile.py` could import it when it arrived; the search plan
arrived first, so the import goes the same way, just sooner.)

Tests: `test_erf_control_found_as_sibling`, `test_lineage_kinds_round_trip`.

Use cases: `09-erf-transient`, `10-erf-timeslice`.

## 19. Dataset clash handling

*Was PR5.5.*

Look in `schema.py` at the comment that starts with "TODO: work out how we handle clashes".
Then think about how we handle these cases.
Probably we will just write tests for each to lock down the behaviour,
but we might have to make some behaviour changes/add high-level helpers to get the behaviour we want.
The tests will probably all be integration tests because setting up the database for the test could be a bit involved/slow.

This is the deliberate, thought-through version of what
[#43](https://github.com/esmporium/esmporium/pull/43) had to fix under races:

"Concurrent saves of the same dataset reliably crash — 8/8 trials raised UnhandledDatasetClashError, misdiagnosing "another worker already saved this exact dataset" as a genuine unmodelled-facet clash.

So testing it now does mean fixing it now. And the fix is bigger than it first looks: the race isn't just the Dataset row. When I fixed that, the probe failed one step later on datasetversion. Every get-or-create in the ingest chain — dataset → version → data node → version–node link → raw doc → raw-doc link (~6 of them) — is select-then-insert, so each races. Making it safe is a concurrency-correctness pass over results_to_database, which is a different beast from "parallelise the query loop" — hence the deferral was legitimate, even if my comment undersold why."

See note in `test_health`

## 20. Ingesting from a local source

*New step.* This is the headache step 10 deliberately deferred.

Adding datasets to the database because we found them on disk, rather than because a
search returned them. The column for this already exists —
`dataset_addition_source`, added in PR2 for exactly this — so the work is the path, not
the schema.

- [ ] walk a local archive, read what we need out of each file (the same header-reading
      step 7 built), and produce the same objects the search path produces
- [ ] the hard part, named up front: deciding when a local file **is** a dataset ESGF
      would have given us. Same dataset, same version, same enough to count as already
      downloaded? Get this wrong in one direction and we re-download; wrong in the
      other and we think we have data we do not have
- [ ] make it clear in the database which answer came from where, so a disagreement
      between a local file and a search document is debuggable

## 21. Daily CMIP5/CMIP6 downloads, using what is already local

*New step.* Depends on step 20.

Downloading daily CMIP6/CMIP5 data, cogniscent of the data that is already available
locally. This is the expensive data that step 10's note said to leave alone until
'ingest from local' works — so this is where that guard comes off.

## 22. Clean up

*Was R13, plus the loose ends from everything above.*

In esmporium-processing: delete `src/esmporium_processing/requirements/` and
`tests/unit/requirements/`, bump the esmporium dependency, and import `esmporium.requirements`
wherever analyses need it. `PLAN-REQUIREMENTS.md` and
`PLAN-REQUIREMENTS-TO-ESMPORIUM.md` can go, since their content now lives in
esmporium's docs and in this file.

This can happen as soon as nothing in esmporium-processing imports the prototype,
which is after step 15 — it sits here because it is tidy-up, not because it is blocked.

Also sweep, at this point:

- [ ] the import-boundary test question (see *Deviations still outstanding* below)
- [ ] the branch-time handling note carried into step 12
- [ ] whatever "wait and see if this bites us" notes we wrote along the way and now
      have an answer for

---

# Decisions, conventions and loose ends

## Decisions taken up front (requirements track)

| Decision | Choice |
|---|---|
| Read-side dataset type | `CatalogueEntry`, kept separate from `DatasetFacets` and pinned by a drift test |
| Database-backed catalogue | **Done** at R4 ([#51](https://github.com/esmporium/esmporium/pull/51)); `InMemoryCatalogue` stays, for testing, likely forever |
| Use cases | Grown PR by PR inside esmporium's tests |
| Spelling | British English: `Catalogue`, `CatalogueEntry`, `InMemoryCatalogue`, `catalogue.py` |

The reasoning behind `CatalogueEntry` is preserved in Appendix B; it is still the
argument to point at when someone asks why there are two classes mirroring the same
facets.

## Per-PR conventions

- One changelog fragment per PR, `changelog/<issue>.<type>.md`, in the existing style: a
  sentence naming the new objects with `[...][]` cross-references.
- `esmporium/requirements/__init__.py` grows its `__all__` each PR, matching
  `esmporium/query/__init__.py`'s style.
- API docs are auto-generated from `docs/api/`, so new modules need no manual page.
- `make checks` and `make test` pass on every PR.
- Fast tests by default: anything slow or network-bound is marked and skipped unless
  opted in.
- Numpydoc docstrings on everything public, at the prototype's density.
- Each requirements PR's `USE_CASES` dict is strictly larger than the previous one's, and
  `test_use_case` round-trips, hashes, solves and (now that it exists) compiles every entry.

## Deviations still outstanding

The requirements prototype and what we are landing differ in a few places. Most are
already done; these two are not:

- **The import-boundary test inverts.** The prototype asserts that
  `esmporium_processing.requirements` imports only esmporium, pydantic and the standard
  library. In esmporium the mirror-image assertion is the useful one: `esmporium.requirements`
  may import `esmporium.query` and `DATASET_FACET_COLUMNS` from `esmporium.datasets`, and
  nothing else from `esmporium.search` or `esmporium.db`.
  Unclear why we need this, maybe remove (at least chek).
- **The `atmos`/`land`/`ocean` test helpers start auxiliary-free** and gain their `Aux` lists
  at step 15. Six lines change in one helper module; the twenty use cases themselves never change.

## Loose ends we have noticed but not scheduled

Not steps of their own, but written down so they are not lost:

- **`merge_on_variable`.** `to_search_plan` deduplicates identical queries but does not
  yet merge two queries that differ only by variable into one search. It was on the old
  R12 list. It is a search-cost saving, so the natural time is alongside step 2 or
  whenever the live searches in step 5 make the cost visible.
- **Caching search results.** Still deliberately not done: if the user says 'search',
  we search (even if we already ran the same search 2 seconds previously) because the
  state of the ESGF database might have changed since we last looked (i.e. there is no
  sensible way to cache).
- **Preferencing search APIs by past performance beyond speed.** PR1.7 ranks by speed
  only; ranking by results-per-project was left for later.

## Out of scope, named as follow-ups

- **Storing requirements and solve snapshots.** `canonical_json` and `requirement_hash` (R2)
  are the primitives; the tables are ordinary esmporium tables now that requirements live here.
  "Requirement became satisfiable" is the difference between `solve` at t1 and at t2. (This is
  now step 3, rather than out of scope.)
- **Linking from `cell_measures`** at ingestion, which is what makes `Aux(via="link")` the
  default rather than the fallback. Lands between steps 6 and 15.
- **Backtracking.** Solving stays greedy: a choice made for one leaf is never revisited to
  satisfy another. [TODO: clarify what this means]
- **Narrowing auxiliary searches.** Auxiliary queries inherit nothing, so they find fx data for
  every model. If that proves too broad, narrow first and widen only when nothing is found, the
  way `ancestry_until` is handled. [I'm not convinced the logic of this is right, let's see when we get to auxiliary]
- **Calendars.** `time_range` and `branch_time_in_parent` stay plain fractional years. [Fine and probably simplest]
- **Trust when loading.** Loading a serialised requirement imports constraint classes by name,
  so only load requirements you trust. Worth a docstring note, not a mechanism, for now.
  [We might be able to do something a bit safer with a specific loading function in the DB, let's see]
- **The `From(...)` / `GlobalMean` annotation layer** from `PLAN-LOAD-CLAUDE.md`, which is
  esmporium-processing's side of the boundary.

---

# Appendix A — the search track as originally written (`PR1` … `PR3.7`)

Kept verbatim, including the review replies, because the reasoning in the completed
steps is the record of why things are the way they are. `PR3.6` and `PR3.7` are here
because they were superseded rather than completed, as described at the top.

### PR1 Done, see https://github.com/esmporium/esmporium/pull/14

Add search

Check that we can execute searches using all our query classes.
Check that we can hit both the ESGF-1 and ESGF-NG APIs.
Check that we can track search API performance in a database.

Tests:

Unit tests:

- [x] conversion of query classes into ESGF-1 and ESGF-NG query parameters
    - no actual hit of the API, we just want to check the generated query parameters for each API
    - default is to always search so that retracted results are included too so we get tracking of when datasets go from not retracted to retracted (this means the retracted parameter will need to be passed every time I think, because ESGF's default is to not return retracted results)
- [x] check of retry logic i.e. make sure that retry wishes from the user are respected and passed through correctly
    - both retrying a given query with a given API and working our way through a list of APIs if the first one fails to give results (after retries)
- [x] *deferred to PR- [ ]5* check of search API performance database handling

Integration tests:

- [x] hit the APIs using all query classes and check that we get the results we expect. Run this on all combinations, even e.g. CMIP5 with ESGF-NG and CMIP7 with ESGF-1 We shouldn't get an error, we should just get no results.
    [x] check that all query - API combinations can be handled. Parse the returned JSON very roughly (proper parsing comes in the next PR) to check results are as expected.
        - only do 'plain' queries here, test the AND/OR logic of the ESGF APIs in a separate test below
        - the simplest will be e.g. CMIP5 with ESGF1
        - the most complicated will be a query using ESGFQuery that queries CMIP5, CMIP6 and CMIP7. This will require the queries to be split in parallel and retries of different end points for CMIP5 vs. CMIP6 vs. CMIP7. I guess the logic should be, if an end point gives no results, try the next one? For this PR, please just have some function like `end_point_selector` which is given the full query and the attempt number and returns the end-point to search. Give this sensible defaults but make sure that this function is injectable so a user can override the logic as they wish/need. This is what will also allow a user to effectively specify a queue of APIs to try as needed. For now, just combine the results in a basic dict with keys for each search e.g. `{"cmip5": cmip5_raw_json, "cmip6": cmip6_raw_json, "cmip7": cmip7_raw_json}` - we will clean this up in a follow up PR.
        - the form of this test will change in a follow up PR (and be easier to read and write) where we return Dataset objects rather than raw JSON (we will stop caring about the raw JSON format as it is only an intermediate). Please mark this test with this note
    [x] check the AND/OR logic of the ESGF APIs. Parse the returned JSON very roughly (proper parsing comes in the next PR) to check results are as expected.
        - the simplest test should not have any test of AND/OR logic, because the search is so simple. However, the test should somehow be parameterised/duplicated so that there are also tests that check the ESGF APIs' AND/OR logic too.
        - this is testing ESGF behaviour, rather than ours, but it's still key to test in case ESGF makes a change
        - the form of this test will change in a follow up PR where we return Dataset objects rather than raw JSON (we will stop caring about the raw JSON format as it is only an intermediate). Please mark this test with this note
        - do this only with our ESGFQuery class (don't worry about the specific ones, they're tested above), but this will need to be done on ESGF-1 ESGF-NG east and ESGF-NG west
    [x] deferred to PR1.5 check that the API health stats appear in the expected database
        - do this both for ESGF1 and ESGF-NG (don't worry about testing across all query classes though)
    [x] mark these tests so that they are opt-in. By default they should be skipped so that our 'main' test suite doesn't require slow ESGF queries to run
        - this pattern of keeping only 'fast' tests on the default testing branch is one we want to use throughout
        - we'll need different marks for different kinds of tests, that is ok
        - we'll probably want a 'run all' mark too for CI etc. Don't add it now, but just keep it in mind

Not included:

- converting results into Datasets
- preferencing API's based on past performance
    - we can add in such a helper later, if we want it. We're going to wait until we want it before we do this though
- tracking queries in our database, that comes later


### PR1.5 Done, see https://github.com/esmporium/esmporium/pull/16

Search API health
If this PR is small enough, consider also adding a selector function that picks the API to use based on health information (e.g. pick the API which has the fastest response with results for the given project)
search.py logging see comment to return failure reason rather than None? (see line 137 in search.py)

Tests:

Unit tests:

- [x] capturing of various paths e.g. successful search (make sure we store request plus time taken for response plus whether there were any results/number of results (so we can distinguish APIs with results for projects vs. those without)?), failure search (store request plus failure status code plus failure status message plus ?), any other paths (not sure what...). Mock out the search API so we control the response

Integration tests:

- [x] check that the API health stats appear in the expected database
    - do this both for ESGF-1 ESGF15-bridge and ESGF-NG (don't worry about testing across all query classes though). This can just be part of the existing integration tests, no need to add new tests.
        - only done on select queries: no point duplicating the tests across code paths that won't vary by search API (we think)

### PR1.7 Done, see https://github.com/esmporium/esmporium/pull/18

Use Search API health information from the database to select/rank APIs for the next request

This is an optional selector function to use if the SearchAPICallRecord table has data (any data, or only opt-in selector if the same request is being sent out? - maybe the latter?) This selector could take a few different formats: it could be based on no. results per request per host, or time per request per host, or results/time. Should this be a user decision, or we make that decision now? Our selector will rank the hosts based on database information, then return hosts based on that ranking (the search functions handle the logic around how many hosts are used, the selector functions don't need to worry about that logic at all).
ZN reply: it can only do something sensible if there is data. You have to decide what to do if the table has no data. I'd probably have a fallback option (e.g. the default selector) so that it doesn't blow up the first time it is used (and then kicks in once there is data)
ZN reply: I wouldn't do it only if the same request is being sent, but I can also see why that might be helpful. It doesn't really matter: we're just writing this as a convenience - users can always inject something else if they want so there is no real penalty to pay from getting this 'wrong'. Just use this PR as a chance to practice adding something and making decisions yourself with a lot of freedom. Maybe use a demo script that you can run repeatedly over different searches to help you figure out what is actually useful in practice.
ZN reply: it's all user decision. The point of the selector function being injectable is that the user can always choose to do something different.
ZN reply: all we want to demonstrate here is how such a selector could work, and to then put a couple that we think are most useful in the codebase so that a) others can get this without having to figure it out themselves and b) users have a template if they want to copy-paste then edit to get behaviour that is exactly tuned to their use case.

Where would a selector live? search_api.py?
ZN reply: ideally it should end up in search_api.py, next to the other in built selectors. However, given that it requires database knowledge, we might have to put it in `search_health` or somewhere else instead.

Update: Only rank by speed for this PR, leave project and more intelligent ranking (by results and speed according to project requests) for future PR.

Tests:

Unit tests:

- [x] I feel a little lost with tests still, but I will have a crack and you can provide feedback.
    - Test that a mock health table with various hosts with different results and request times (and attempt numbers) rank the way we want them to. Creates output of ranked hosts that can be injected back to search.
      ZN reply: yep this is what we want

Integration tests:

- [x] Are there any integration tests here? Feels like maybe just unit tests if we are only performing live tests on a single host?
    - ZN: yep no integration tests for this. The functionality is nice to have, not something we need to make bullet proof. Also setting up an integration test that was more than just running a bunch of searches then using this selector would be hard, and this simple integration test is very expensive to run and of very little value.

### PR2 Done, see https://github.com/esmporium/esmporium/pull/25

Converting search results into Datasets

Check that we can parse search results into Datasets
Check whether we can create a mermaid diagram that, by definition, stays in line with our code that we can put in our docs to show the links between things
Include pagination in this step (currently we know the number of results found for each search, but are only recording the first 10,000 in the raw_json). STAC and SOLR will be handled differently. ~SOLR can be paralelised and STAC (with east and west having slightly different naming conventions), have to be handled consecutively.~ defer parallelisation to PR3.5
Defer parallelisation to PR3.5 ~This is the first place where we will be parallelising (handling multiple pages of results via SOLR). There are notes in tests/integration/test_health.py about defering parallelisation testing, primarily regarding how we handle recording results to a dataset in paralel (must be handled specifically).~

Tests:

Unit tests:

- [ ] parse a response from ESGF1, ESGF15-bridge and ESGF-NG into Dataset objects
    - use the pre-saved responses from PR1
    - Open up pre-saved responses to tas and rlut (multiple variables, not just tas)
    - don't save the Datasets into the database - that is an integration test
    - this will require adding the dataset version and dataset location objects (and I guess then also tables). The dataset location link may need to be nullable, because it doesn't exist for ESGF-NG?
    - I think this will also require adding in the project-specific dataset tables so we can parse CMIP5, CMIP6 and CMIP7 results properly
    - please also include a `dataset_addition_source` or similar column (I'm not sure what the best name is). In future, we will also want to add datasets e.g. from local files and I think we'll want to be able to tell whether a file was added because it was found in a query vs. because it was found in local files.
    - Defer to PR2.5 ~Pagination: we want a unit test for the pagination behaviour~

Integration tests:

- [ ] Update the integration tests from PR1 so that they now check the created Dataset objects, rather than the parsed raw JSON
- Defer to PR2.5 ~Pagination: an explicit integration test for a search that returns around 30 000 results (so we need 3 queries to get everything)~
- Defer to PR2.5 ~We can get the integration test with SOLR. With STAC, we'll have to rely on mocking for now to test this and a proper integration test can wait until there are 10 000 records available. -> or perhaps not...~
- Defer to PR3.5 ~SOLR paralelisation and saving multiple pages of results to dataset concurrently.~

Not included:

- parent stuff, that comes later

### PR2.2 Done, see https://github.com/esmporium/esmporium/pull/37

Add tests that the same request is built if a user uses `other_terms` instead of our supported names.

They can be unit tests (I think).

This is how we check that `other_terms` will actually end up in requests to search APIs.

### PR2.5 Done, see https://github.com/esmporium/esmporium/pull/40

Pagination

Unit tests:

- test pagination against a mock end point

Integration tests:

- Pagination: an explicit integration test for a search that returns around >10 queries when we set limit to 3 (so we need multiple queries to get everything)
    - by fiddling with limits, we should be able to set up an integration test for both SOLR and STAC

### PR3 Done, see https://github.com/esmporium/esmporium/pull/41

Alter our search entrypoint

By this stage, we'll have two key functions (at least conceptually, maybe not in practice):

- [ ] `search`
- [ ] `parse_raw_json`
    - this will only parse into Datasets here. However, in future, this could also return DataAccess/File objects, so we need to be careful that the function signature and build of `parse_raw_json` accomodates for this (if not already, it should at least be clear how it would be modified to support DataAccess/File in future)

We want to add higher-level interfaces 'above' these.
For `search`, that means renaming the existing `search` to `search_single` (because it works with a single query) so we can then create our 'real' `search` function.
The new `search` function should handle splitting queries into project-specific queries, paralelise (although implementation is deferred until PR3.5), search ESGF, parse the raw JSON into datasets and save those datasets to the database.
We want it like this so that every search result goes in the database: that coupling is deliberate and, in many ways the point of esmporium. Users can use the low-level functions if they really want.

The new `search` function should take one or more queries.
In PR3.5, we will add handling of parallelisation of calls to `search_single`
(this is now simplified: we are going to set this up so that `search`
only ever passes queries that specify a single project to `search_single`
(we are never going to be fancy and put CMIP6 and CMIP6Plus searches together to save one query, because it makes implementation and error handling so much harder).

Add parallelisation in this PR. Parallelisation of search queries and saving (noting that we will not parallelise pagination). Only Solr (not STAC) could parallelised, and the benefits (time saved) would be small. Instead we will focus our attention on optimising other areas of the workflow to parallelise where we can really save.

My instinct is to do it this way. Check this plan with claude first.

Unit tests:

- [ ] When a number of searches are done in parallel using `search`, if the process is killed in the middle of the parallel work, the results from the searches that did succeed are still in the database
    - this is the key check that the saving is done as part of `search_single`, rather than only happening after all search results have been collected in `search`
    - I have no idea how to implement this, ask claude (I think it should be possible to somehow kill a function halfway through, maybe just by raising an otherwise unhandled error...)
    - not a perfect unit test, but should be fast to run (by using a mock search endpoint) so can stay here

Integration tests:

- [ ] Update the integration tests from PR2 so that they use `search` or `search_single` (as is appropriate for the test)
- [ ] Add test of executing multiple queries in parallel using `search`
    - in the docstring of `search`, note that performing multiple queries with this function will be (once we do PR4) the only way to get certain types of logic e.g. if you want to look for `tas` monthly and `ts` daily but exclude `tas` daily and `ts` monthly, you need to do two queries because the ESGF API will give you `tas` and `ts` monthly and daily if you put in a search like `variable=["tas", "ts"], frequency=["mon", "day"]` (this is definitely true for SOLR, for NG it's probably not quite like that as the CQL2 syntax probably allows better AND/OR control - let's defer really deep support that right now, we can come back to it in future if we ever decide that we really need that feature/control)

Not included:

- there is no deliberately no attempt to cache in anyway here. If the user says 'search', we search (even if we already ran the same search 2 seconds previously) because the state of the ESGF database might have changed since we last looked (i.e. there is no sensible way to cache).
-

### PR 3.3 Done, see https://github.com/esmporium/esmporium/pull/42

Split search.py into subfolders for clarity.

### PR 3.5 see https://github.com/esmporium/esmporium/pull/43

Concurrent saving to database from multiple parallel workers, dataset clash handling:

"Concurrent saves of the same dataset reliably crash — 8/8 trials raised UnhandledDatasetClashError, misdiagnosing "another worker already saved this exact dataset" as a genuine unmodelled-facet clash.

So testing it now does mean fixing it now. And the fix is bigger than it first looks: the race isn't just the Dataset row. When I fixed that, the probe failed one step later on datasetversion. Every get-or-create in the ingest chain — dataset → version → data node → version–node link → raw doc → raw-doc link (~6 of them) — is select-then-insert, so each races. Making it safe is a concurrency-correctness pass over results_to_database, which is a different beast from "parallelise the query loop" — hence the deferral was legitimate, even if my comment undersold why."

See note in `test_health`

### PR3.6 and friends

See https://github.com/esmporium/esmporium-processing/blob/plan/PLAN-REQUIREMENTS-TO-ESMPORIUM.md

@Anna let's discuss how to split this up.
It might make sense to execute the entire plan, or we might want to break it up somehow and do a couple of things in parallel.

### PR3.7

Use `Requirement` (from PR3.6) in search.
This is what we actually need to support all different ways of creating searches
and hence what we should link to when we think about 'tracking changes in search results over time'.
Include an optional name parameter in `Requirement`.

Update `search` so it only takes `Requirement` objects.

Unit tests:

- [ ] auto-generation of name for `Requirement` objects. Include a timestamp so collision of names is very unlikely
- [ ] AND/OR logic with `Requirement` and search (should be covered in PR3.6 mostly, we just have to make sure this applies to what we return from `search` or explicitly decide not to do this filtering on search results i.e. make the filtering database specific so we don't need both an in memory catalogue and a database catalogue)

Integration tess:

- [ ] update our `search` tests to use the new interface
- [ ] add `search` tests that use `Requirement`'s AND/OR logic (i.e. have multiple `Query`'s in the search that is generated by `Requirement`)


---

# Appendix B — the requirements track as originally written

From `esmporium-processing/PLAN-REQUIREMENTS-TO-ESMPORIUM.md`, written 2026-09-16.
Kept verbatim: the framing, the `CatalogueEntry` reasoning, and the steps which are
done (`R0`–`R4`). The steps which are still to come (`R5`–`R13`) are not repeated here
— they are steps 8, 11–15, 18 and 22 above, carrying their original text.

One wrinkle to know when reading it: the table in that document drifted out of step
with its own sections. The table said `R4` was `to_search_plan`, while the section
bodies had `R4` as "`search` takes a `requirement`" and `R12` as `to_search_plan`. Both
landed together in [#51](https://github.com/esmporium/esmporium/pull/51), so it does
not matter any more, but the `R` numbers used above are the **section** numbers, not
the table's.

### Context

`PLAN-REQUIREMENTS.md` says where this is going: **requirements move into esmporium**, which
then owns the database, requirement-driven searches, solving, and storing requirements and
their results. esmporium-processing keeps only the analyses, which declare requirements. The
prototype was written to make that move mechanical — it imports only esmporium, pydantic and
the standard library, and a test enforces it.

What is left is the sizing problem: 3 305 src lines, 1 802 test lines and 20 use cases, to be
landed in 100–1000 line PRs.

Two obvious orderings both have a real cost. Starting from the high-level interface — the one
we actually care about — and building complexity outwards churns every PR and re-teaches the
mental model each time. Starting from the lowest-level pieces churns almost nothing, but
leaves each piece unmotivated: it is hard to see why `Aux.match` is a tuple of tuples, or why
a lineage's `role` is required, without the high-level context.

### The ordering: a walking skeleton, then thicken

Three small PRs build a thin end-to-end slice — entries and matching, `Leaf`/`all_of`/
`Requirement`, then `solve` — so that a requirement can actually be solved by **R3**, roughly
1 300 lines in. From there every PR adds exactly one concept.

Three things make the rest cheap:

- **The node types are purely additive.** After R2 the `Node` union has two members. Each
  later node type is one more entry in that union plus one more branch in four small dispatch
  functions (`apply_to_leaves`, `walk_leaves`, `role_paths`, `_eval`). Nothing already written
  is reshaped.
- **The design note lands first (R0),** so every low-level PR can be read against a stated
  target. This is the direct answer to "it is hard to see why the low-level pieces are how
  they are".
- **The use cases grow PR by PR.** `use_cases.py` starts at R3 with one case and each later PR
  adds the cases it unlocks, so every PR ends with "and these real use cases now build and
  solve". Every low-level PR gets a high-level anchor without anyone having to invent one.

Numbered `R0`–`R12` rather than continuing esmporium's `PR1…PR9`, because **nothing here
depends on esmporium's PR4–PR9**; this can run in parallel with them. Only the follow-up
database-backed catalogue is gated on PR6 (parent links) and PR7 (file information).

| PR | Concept added | src | tests | Use cases unlocked |
|---|---|---|---|---|
| R0 | The design note, as docs including ascii or other plain text diagram(s) | ~300 | — | — |
| R1 | Entries, facets and matching | ~310 | ~160 | — |
| R2 | `Leaf`, `all_of`, `Requirement`, `.where()` | ~565 | ~200 | — |
| R3 | **`solve` — end to end works here** | ~450 | ~475 | 14 |
| R4 | `to_search_plan` | ~150 | ~135 | —
| R5 | `Ancestors` lineage | ~325 | ~260 | 15, 17 |
| R6 | The `Constraint` protocol (no shipped checks) | ~425 | ~230 | — |
| R7 | `Covers` | ~190 | ~280 | 01, 03, 04, 05, 11 |
| R8 | `any_of`, `optional` | ~235 | ~300 | 06, 07, 08, 12, 18 |
| R9 | `namespace`, `SameTimeRange` | ~215 | ~270 | 02, 16, 16a |
| R10 | `Sibling` | ~110 | ~170 | 09, 10 |
| R11 | `Aux` | ~280 | ~400 | 13, 19 |
| R12 | `to_search_plan` |
| R13 | Retire the prototype | — | — | — |

Totals: ~3 255 src lines against the prototype's 3 305 — a useful check that the slices cover
the whole thing.

### Why `CatalogueEntry` and not `DatasetFacets`

`DatasetFacets` (`esmporium/search/result_parsing.py`) already mirrors `Dataset`'s facet
columns, so it is the obvious candidate. It is the wrong one, because it is the **write-side**
DTO: parsers produce it and `save_dataset` turns it into a row. It has no surrogate `id`
because the row does not exist yet, and it is `extra="forbid"` precisely so that a parser
which invents a facet fails loudly.

The solver needs the **read-side** projection of a row that does exist: a stable `id` — every
parent link, aux link, metadata lookup and error message keys off it — and a deliberately
*open* `extra`, which is the documented home for project-specific facets used by `group_by`,
`prefer` and auxiliary matching. Merging the two gives one class whose `id` is `None` half the
time and whose `extra` must be forbidden on one path and open on the other.
(Let's think about whether we call this `extra` or something else and how we handle it exactly.)

Inheriting is wrong for a smaller reason: `DatasetFacets` is `DATASET_FACET_COLUMNS` *plus*
`id_project_specific`, whereas `CatalogueEntry.facet()` answers only for
`DATASET_FACET_COLUMNS` + `extra`. A subclass would carry a required `id_project_specific`
field that `.facet("id_project_specific")` then refuses to answer for.

esmporium already has a convention for this exact shape of problem — two classes mirroring the
same facets, kept separate and pinned by a single assertion whose docstring says the
hand-written list is deliberate, because "changing our Dataset model is a big deal"
(`tests/unit/test_schema.py`, `test_dataset_facets_mirror_dataset_columns`). R1 follows it.

The name is `CatalogueEntry` rather than `DatasetRecord` or `DatasetCandidate` because, of its
33 use sites, only three are candidate-shaped (`_choose`'s parameter and `_eval_leaf`'s local).
The rest hold datasets already decided on — `ResolvedGroup.roles`, `Lineage.members`,
`GroupView.roles`, the parent from `parent_of`, the auxiliary data from `linked`, the subject
of `metadata(...)` — plus `InMemoryCatalogue.records`, which is simply the catalogue's
contents, with no requirement in sight. Naming it for the layer that owns it reads correctly
everywhere.

### The PRs

#### R0 — The design note, as docs

No code. Port `PLAN-REQUIREMENTS.md` into esmporium as
`docs/further-background/data-requirements.md`, and list it in `docs/NAVIGATION.md` under
*Further background*, beside *Dependency pinning and testing*. Add a `## Requirements (R0–R12)`
section to esmporium's `PLAN.md` holding the table above.

This is what makes the bottom-up half of the ordering tolerable. It lands the mental model —
the three kinds of "or", the three levels a check can be attached at and what each means when
it fails, what a role path looks like — before any of the pieces arrive, so every later PR can
be reviewed against a stated target.

Keep *Three kinds of "or"*, *The tree*, *Relations*, *Constraints*, *Solving*, *Flow and
storage*, *Queries and facets* and *Findings worth remembering*. Drop the prototype-specific
*Where this is going* and *Known limits*, folding the latter into this document's
*Out of scope* below.

#### R1 — Entries, facets and matching

New package `src/esmporium/requirements/`, with `catalogue.py` and `__init__.py`.

- `CatalogueEntry`: `id`, the nine `DATASET_FACET_COLUMNS`, `extra`, and `.facet(name)`.
- `set_facets(query)`, which flattens a `Query` including `other_terms`; `ClashingFacetError`.
- `matches(query, entry)`; `UnsupportedFacetError`.
- `Catalogue` protocol with `find` only. `InMemoryCatalogue` with `records` and `find`, plus
  the `__post_init__` uniqueness check.

Tests: port `test_matches`, `test_matching_unrecorded_facets_raises`,
`test_other_terms_are_facets_too`, `test_facet_set_twice`, `test_catalog_rejects_duplicate_ids`
and the `record()` helper. Add `test_catalogue_entry_mirrors_dataset_columns` directly below
the existing `test_dataset_facets_mirror_dataset_columns` in `tests/unit/test_schema.py`.

#### R2 — `Leaf`, `all_of`, `Requirement`, `.where()`

`tree.py`: `Leaf` (query, role, `Leaf.of`, role validator), `AllOf`, `all_of`, `as_node`,
`Requirement` (name, tree, where, group_by, prefer, cardinality), `canonical_json`,
`requirement_hash`, `add_facets`, `_normalise`, `effective_query`, `.where()`,
`apply_to_leaves`, `walk_leaves`, `role_paths`, `DuplicateRoleError`, `ConflictingFacetsError`.

The `Node` union is declared here with two members. Say in the PR description that every later
node type is an additive entry in that union plus a branch in four dispatch functions — that
property is what the whole ordering rests on.

Tests: `test_leaf_shorthand`, `test_leaf_needs_role_for_several_variables`,
`test_duplicate_roles_in_all_of`, `test_where_adds_facets_to_every_leaf`,
`test_where_contradicting_a_leaf_is_an_error`,
`test_requirement_where_contradicting_a_leaf_is_an_error`, `test_role_paths`,
`test_project_specific_facets_are_allowed_when_building`, `test_hash_changes_with_content`,
and a tree round-trip.

#### R3 — `solve`: the skeleton closes

`solve.py`: `Explanation`, `Resolved`/`Unresolved`/`NodeResult`, `ResolvedGroup`/
`UnresolvedGroup`/`SolveResult`, `_Context`, `_group_values`, `_eval_leaf` (no lineage,
auxiliary data or constraints), `_eval_all_of`, `_choose` (i.e. `prefer` and `cardinality`),
`_merge`, `_worst`, `_label`, `describe_query`, `solve`.

`solve(PATTERN_EFFECT, catalogue)` works. So do grouping and fan-out — `group_by` including
`variable` or `experiment` — which is the least obvious third of the design and is now
demonstrable rather than described.

Introduce `tests/unit/requirements/use_cases.py`, seeded with `14-pattern-effect`, the
auxiliary-free `atmos`/`land`/`ocean` helpers, the alias tuples (`CONTROL`, `SCENARIOS`,
`ABRUPT`, …), and `test_use_cases.py::test_use_case` parametrised over the `USE_CASES` dict —
round-trip, hash, and solve against `satisfying_catalogue`. Port `satisfying_catalogue` in its
`find`-only form; it grows a branch per PR alongside the feature it serves.
[We'll think about all these helpers. Lots of them either obscure what is actually going on,
or are things we can just put in the package so there'll be some thinking here.]

Tests: `test_all_satisfied`, `test_required_variable_missing`, `test_ambiguous_grids_and_prefer`,
`test_ambiguous_despite_prefer_says_what_was_preferred`, `test_explanation_is_readable`,
`test_project_specific_facets_come_from_the_catalogue`, `test_facet_no_dataset_knows_is_an_error`.

If this runs long, move the `use_cases.py` seed to R4; the skeleton itself is ~450 src and
~250 test lines.

#### R4 — `search` takes a `requirement`

Here is the first step to combining search and requirement logic. Update `search` to take a requirement, instead of a `QueryProtocol`. This will allow us to start performing live ESGF searches for simple use cases on CMIP7 data, to track what is currently available, what will be available in future (what was not satisfied becomes satisfied on another search). This will also mean we need to add a catalogue that is backed by our database, so we can test this live, rather than than only using the InMemoryCatalogue (we will keep the InMemoryCatalogue for testing, likely forever). We may need to update the catalogue protocol class too to make this work. If we need to make such a change, that is ok.

This PR will alter search test logic and may alter catalogue test logic. It will require us to add a database-backed catalogue and associated tests (potentially we should split this in three: 1) alter search to take a requirement 2) add a database-backed catalogue 3) add integration tests of specifying a requirement, performing a search (which saves everything it gets in a dumb way, rather than trying to be smart and only saving what is actually required) and then getting the trees which match the requirement using the database-backed catalogue).
