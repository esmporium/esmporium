# Enrichment: the time information ESGF does not publish

Design note for the `enrich` step, and for making
[`Covers`](data-requirements.md#constraints) work against a real catalogue rather
than a hand-written one. Like
[the requirements design note](data-requirements.md), it lands ahead of the
implementation, so that every later PR can be read against a stated target.

Written: 2026-11-12.

Cross-references are written as plain code spans rather than `mkdocstrings`
links, because most of what they name does not exist yet and a strict docs
build would fail on them. They become links as each piece lands.

## Context

PR [#69](https://github.com/esmporium/esmporium/pull/69) shipped `Constraint` and
`Covers`. `Covers` works — but only against `InMemoryCatalogueWithTimeInfo`, a
35-line test-only catalogue (`tests/unit/requirements/catalogues.py`) answering from
two hand-written dicts keyed by entry id. Against `DatabaseCatalogue` it cannot run
at all: it needs `CatalogueWithTimeInfo` (`get_time_range`,
`get_branch_time_in_parent`) and no column anywhere holds a dataset's start and end,
its calendar, or an interpreted branch point. `PLAN.md` step 13 says so outright:
*"This step needs a source of `time_range`, and we have none."*

The failure is also silent in the wrong direction. The `Constraint` protocol says
"raise if you cannot evaluate", `solve._evaluate_constraints` catches **any**
exception and returns `Fail`, and the group lands in `SolveResult.unsatisfied` — the
same bucket as "no such dataset was ever published". *"Nobody has got this information/enriched the database
yet"* is being reported as *"there is no data"*.

ESGF's search APIs do not guarantee publication of start and end times or branch points, so esmporium
has to fill that gap itself. The decision taken is to **store** the information
rather than derive it during `solve`, because (a) lookups then cost nothing, (b) a
value set by a fix is recorded rather than recomputed every run, and (c) it is where
the information belongs. That needs a new pipeline step between search and solve:
**`enrich`**.

```
Requirement ──► search ──► database ──► ENRICH ──► solve ──► outcome
                                        ^^^^^^
                                     the new step
```

Intended outcome: `Covers` runs against a real migrated database in an integration
test, and a solve that lacks the information says so *distinctly* and hands back a
list that can be passed straight back to the enricher.

## Settled decisions

| # | Decision |
|---|---|
| 1 | The step is **`enrich`** / enrichment. `src/esmporium/enrich/`, `enrich_datasets(...)`, PLAN step "Enrichment". |
| 2 | The new outcome is **`NeedsEnrichment`**, not `Undetermined` — "undetermined" implies we might look and still not know, when in fact we just have to go and do the work so that the answer is available. |
| 3 | **Store both** natural and rich time values. Natural values are ones in the dataset's own frequency and come from the filename, which is basically free to parse and is free (natural values match the dataset's frequency e.g. for yearly datasets you get values like 2024, 1840, for monthly datasets values like 1850-01, 2023-12); rich comes from a file header and is fetched only where needed (rich means information that allows you to create a full datetime object without guessing i.e. values like 2023-01-01 00:00:00, you don't get this from the filenames alone). |
| 4 | **Never store fractional years, and never convert to them.** Natural units are the data's own frequency; rich values are datetimes with a calendar. No year conversion at solve time, and none at enrich time. |
| 5 | **Store start and end, not extents.** An extent is `end - start` (or `end - start + 1` sometimes because of how the indexing/conversion works e.g. the extent of a dataset that goes from 1850 to 1860 when its natural units are years is 11 years, not 10, because the actual bounds can be assumed to be 1850-01-01 to 1860-12-31, this has tripped over many people, hopefully not us), computed outside the database when wanted. Storing both would be the same fact twice, with a consistency burden (that is impossible to get right given that the correct answer depends on assumptions/context that we don't have at enrichment time). |
| 6 | **One pair of value columns plus a `units` column**, not a natural column group and a rich column group. No half-empty rows. |
| 7 | A corrected branch time is a **`FIX`-sourced row**, not a Python-level catalogue wrapper. |
| 8 | A version whose files have a gap gets start and end recorded **plus a contiguity flag**, and `Covers` refuses a gapped range rather than treating it as continuous. |
| 9 | **`Covers` defaults nothing.** Both modes are first-class and the user states which one they mean, in full, every time. This follows the rule `Requirement` already sets for `group_by` — *"a grouping decides what one group is, so it is stated rather than defaulted"* — and it is forced anyway, because containment cannot be written as a number: nobody knows a dataset's extent until we have measured it. |
| 9b | **Time ranges are stored half-open, `[start, end_exclusive)`, and the field is named for it.** See "The half-open convention" below. |
| 10a | **A requirement may be stated in years or months only, for now.** `min_overlap` and `pad` in days or hours get `MISSING_FEATURE_PLEASE_RAISE_ISSUE`. This is a decision about the *API surface*, and says nothing about which data we support — see "Frequency, units, and what needs a header". |
| 10b | **Data at any frequency is supported, including daily and sub-daily** — but anything finer than monthly needs a calendar to unambiguously calculate the extent, so it needs a header read, so it reports `NEEDS_ENRICHMENT` until enriched. Monthly and yearly data never need a file opened to calculate their extent in natural units (they will need files opened to calculate extent in other units). |
| 11 | A **sibling** pair whose axes do not align is a **loud `Fail`** naming the constraint, not a raise: it is a question we can answer, and the answer is no. |
| 12 | An **uninterpretable raw CMIP5 branch time** is `NeedsEnrichment` pointing at a fix. |
| 13 | **`SameTimeRange` is deferred** (PLAN step 15), with a comment in the code recording this decision so it is not lost when step 15 lands. |
| 14 | **The branch point lives on the edge, not on either dataset** — a deliberate departure from ESGF, whose data model has no edges. The *facts* come from the child (which is where they are published and where `DatasetVersionParent` already stores them); the *place they belong* is the child→parent link. |
| 15 | **`Covers` does not apply to `fx` data at all, and says so explicitly** rather than relying on nobody ever attaching it to an `fx` leaf. |
| 16 | **Keep the raw text beside the parsed value** (`start_raw`, `end_raw`). The string is the observation; the number is our parse of it. Different facts, and keeping the string lets a wrong parser be corrected without re-reading anything. |
| 17 | An unsupported requirement unit is **reported as a group that failed to solve**, with a status along the lines of `MISSING_FEATURE_PLEASE_RAISE_ISSUE` pointing at the issue tracker — *not* an exception that aborts the solve. |
| 18 | The actionable list is **`enrichment_inputs`** — it names what you pass to the enricher. |
| 19 | `SolveResult` stays at **four mappings**: `satisfied`, `unsatisfied`, `ambiguous`, `couldnt_answer`. The fourth holds every group we could not answer, **with the reasoning** — each group carries which status applied and why. (`couldn't_answer` cannot be a Python identifier, so the apostrophe is dropped.) |
| 20 | **Calendar arithmetic uses `cftime`, from a new optional extra. We do not roll our own.** Six CF calendars with six sets of leap rules is exactly the job that looks small and is not. It is an *extra* rather than a dependency, following `h5py`: monthly and yearly requirements never need it, because twelve months is a year in every calendar, so only the sub-monthly path pays. See "Where `cftime` can live" below for the one architectural consequence. |

## The `Covers` question, settled

Two modes over the same stored values, and the user names which one they mean —
nothing is defaulted (settled decision 9):

```
Covers(align="branch", mode="containment", pad=(10, 10))
Covers(align="branch", mode="overlap", min_overlap=100, pad=(10, 10))
```

One class with a required `mode` rather than two classes, because `align` and the
lineage mapping are common to both and two classes would duplicate that surface.
`mode` is required rather than inferred from whether `min_overlap` was given, because
inferring it is exactly the kind of default decision 9 rules out — and because
containment cannot be written as a number anyway.

### The one piece of arithmetic

The simplification that makes this readable: **after mapping, the child's start *is*
the branch point.** `_needed_span` computes `start' = branch + (start - step_start)`,
so the mapped child start equals `branch` by construction. Everything below is in the
control's axis, after that mapping.

```
child_extent = child_end_exclusive - child_start

PASS iff  child_extent >= min_overlap
     AND  control_start          <= branch_point - pad_before
     AND  control_end_exclusive  >= branch_point + min_overlap + pad_after
```

Three independent comparisons, and the analysis window is **anchored at the branch
point**: it is the child's first `min_overlap` of data, which is the window an anomaly
actually uses. The pad is control data either side of that window, and whether the
child also has data in the pad region is irrelevant.

Anchoring is the part worth not losing. An earlier draft of this note let the window
slide anywhere inside the overlap, which passes cases where the control does not reach
the branch point at all:

```
            0        100       150        250            400
control                        |===========================|
child mapped      |=============================|
branch            ^

sliding window   finds 90 yr of overlap further right   -> PASS
anchored         control_start 150 <= 100-10 = 90 false -> FAIL   <- the right answer
```

There is no control data at the branch, so there is nothing to take an anomaly
against. The anchored form is both stricter and easier to explain.

Verified against two worked examples — control `[0, 200)`, child mapped `[100, 250)`,
`branch_point = 100`, pad 10 either side, so `child_extent = 150`:

| Requirement | extent ≥ overlap | control reaches back | control reaches forward | Verdict |
|---|---|---|---|---|
| `min_overlap=100` | 150 ≥ 100 ✓ | 0 ≤ 90 ✓ | 200 ≥ 210 ✗ | **Fail** — short on the far side |
| `min_overlap=10` | 150 ≥ 10 ✓ | 0 ≤ 90 ✓ | 200 ≥ 120 ✓ | **Pass** |

Containment is the same arithmetic with `min_overlap` set to the covered run's own
extent: conditions two and three become `control_start <= child_start - pad_before`
and `control_end_exclusive >= child_end_exclusive + pad_after`, which is `_covers()`
as #69 already implements it. So one private function serves both modes.

### The half-open convention

Settled decision 5 notes that an extent is sometimes `end - start` and sometimes
`end - start + 1`, and that this has tripped over many people. **Storing half-open is
how we stop it being "sometimes".**

A range is `[start, end_exclusive)`, and the field is *named* `end_exclusive` rather
than `end`, so the convention travels to every call site, query and log line instead
of living in a docstring somebody read once. That is the same move this repo already
made with `reporting_interval` over `frequency`, and with
`branch_time_in_parent` / `branch_time_in_child`.

Decision 5's own example, stored this way:

```
yearly data published as 1850 to 1860

  end_raw          "1860"      what the filename said
  start            1850
  end_exclusive    1861        the first year NOT in the data
  extent           1861 - 1850 = 11      <- no correction term, ever
```

Three things follow, and the third is why the convention is chosen rather than merely
documented:

1. `extent = end_exclusive - start`, with no `+1` anywhere.
2. `min_overlap` and `pad` are *lengths, not positions*, so the convention does not
   touch them: `min_overlap=100` against monthly data means one hundred months.
3. Two files are adjacent exactly when `previous.end_exclusive == next.start`. That
   *is* gap detection (settled decision 8). With inclusive ends it would be
   `previous.end + 1 == next.start`, and that `+1` has to know the unit.

The off-by-one is confined to one named conversion, so there is exactly one place to
get it right and one place to look when it is wrong:

```
filename  "…_320001-325012.nc"
   │
   ├─ PublishedRange(start_raw="320001", end_raw="325012", unit="mon")   inclusive, as published
   │
   └─ to_half_open(...)        <- the ONLY place the +1 lives
         │
         └─ TimeRange(start=38400, end_exclusive=39012, unit="mon")      half-open
```

The value type owns the arithmetic — `extent`, `contains`, `is_adjacent_to` — so no
caller ever subtracts by hand. The docstring explains the convention; the type is what
enforces it.

Drafted wording for the stored field:

> **`end_exclusive`** — the first step *not* in the data, counted in `units`.
>
> Half-open, so that `end_exclusive - start` is the extent with no correction term,
> and two files are adjacent exactly when
> `previous.end_exclusive == next.start`. That second property is what gap detection
> is, so the convention is chosen for it rather than merely documented.
>
> Note that this is **not** what the filename says. A monthly file named
> `…_320001-325012.nc` contains December 325012, so `end_exclusive` is January
> 325101 — one step past it. `end_raw` keeps `"325012"` as published, which is the
> value to quote back to a user, and the value to re-parse from if this conversion
> ever turns out to be wrong.

and for `Covers`:

> `min_overlap` and `pad` are lengths, not positions, so the half-open convention
> does not apply to them: `min_overlap=100` against monthly data means one hundred
> months.

The test that catches a wrong conversion is the **single-step dataset**: a monthly
file named `…_320001-320001.nc` holds one month, so its extent must be 1. Subtracting
the published values gives 0. That case goes first in PR A's table, beside two
adjacent files satisfying `is_adjacent_to` and a one-month gap failing it.

### Covers vs SameTimeRange — why both exist

| | `Covers`, branch-aligned | `SameTimeRange` / calendar-aligned |
|---|---|---|
| Relationship | ancestry (parent → child) | sibling, or any pair on one axis |
| Axes | do **not** align; shifted through the branch point | **must** already align |
| Needs a branch point? | yes — absent one, `NeedsEnrichment` | no, never asks for one |
| Axes don't line up | expected, and resolved by the branch point | a loud `Fail` (settled decision 11) |

With ancestry a misaligned axis is normal and the branch point resolves it. For
siblings there *is* no branch point, so the axes have to align, and if they do not
something is wrong with the data. `Covers(align="calendar")` already never reads a
branch time, so the distinction exists in code today and must not be optimised away.
A code comment records this, per settled decision 13.

## The design switch this forces

Moving time information onto catalogue entries changes how constraints get their
data: **from "ask the catalogue" to "ask the entry"**. That is a real switch and it
is argued out here rather than assumed.

**For:**

1. *It closes a hole #69 documented and could not close.* `constraints.py`'s module
   docstring states that a type checker cannot check a **requirement**, because its
   constraints may each need a different catalogue — so the generic-catalogue design
   is unverifiable at the one place it matters, and its failure mode is a mystery
   `Fail`. As a field, the question becomes "were these loaded?", which already has
   a typed error (`UnloadedFacetError`) carrying its own remedy in the message.
2. *It scales with constraints, not with catalogues.* Every future constraint needing
   a stored fact — `Aux` and `linked` (step 16), anything download- or
   checksum-aware — would otherwise mean another protocol and another runtime-raise
   path. As fields it is one mechanism with one loading flag.
3. *The reason it was not done this way has gone.* #69 kept it on the catalogue partly
   so a buggy branch time could be corrected by wrapping and overriding. Settled
   decision 7 replaces that with a recorded `FIX` row, which is durable, auditable and
   shared. The wrapper no longer needs to exist.

**Costs, stated honestly:**

1. *`CatalogueEntry`'s invariant weakens.* Its docstring says the fields "mirror the
   columns of `Dataset`, so that the two cannot drift apart unnoticed". A time range
   is a `DatasetTimeRange` row, not a `Dataset` column, so the entry becomes "a
   dataset row plus facts hanging off it". That must be written into the docstring,
   or the next reader will try to re-align it with `Dataset` and delete the fields.
2. *A branch point is not a property of a dataset* — it is a property of the
   (child → parent) edge, and `Lineage` already models that edge — hence settled
   decision 14, and why the branch point goes on the lineage rather than the entry.
3. *The `Constraint[CatalogueT]` extension point should not be deleted*, only vacated.
   Keep the pattern and its documentation; `Covers` simply stops being its worked
   example, which means `docs/further-background/data-requirements.md` needs a new
   one. The best candidate is an **aggregate over the catalogue**, which cannot be a
   field on an entry even in principle: "this model must have published at least
   three `variant_label`s" is a question about *how many other rows exist*, not about
   any one dataset. A weaker second example is **local availability** ("only use what
   is already downloaded", cf. PLAN step 22), which is not a property of the dataset
   because two machines answer differently for the same one.

### Does `Dataset` grow the same fields?

The symmetry instinct is right, but the answer is not `Dataset`, and the repository
already settles it two ways:

- **A time range is per *version*, not per dataset.** `catalogue.py` carries an
  explicit developer note: *"**one entry describes one `Dataset` row, not one
  `DatasetVersion`**, and the versions filter decides which version's documents speak
  for it."* So the storage hangs off `DatasetVersion`, and the entry carries the range
  of its **describing version**.
- **That pattern is already built and shipped.** `DatasetVersionParent` is keyed on
  the *child's version*, and `parent_of` resolves `describing_version(...)` first
  before reading it. So "an entry-level question answered through the describing
  version" is exactly how #67 already does parents. Time information follows the same
  road rather than opening a new one.
- **No ORM relationships.** `schema.py:337` has the one `Relationship()` in the
  codebase commented out, marked *"TODO: bring this back in once we start doing
  versions"*. Everything else is explicit `select`s. Adding relationship fields to
  `Dataset` would reverse a deliberate choice, so it is out.

So nothing is being decoupled, and in particular the entry does **not** start
mirroring `DatasetVersion` instead. It has none of that table's columns — no
`version`, `is_latest` or `retracted` — and `entry.id` is a `Dataset.id`. Saying
otherwise would contradict the note quoted above and break
`test_catalogue_entry_mirrors_dataset_columns`
(`tests/unit/test_schema.py`), which asserts
`list(mirrored) == list(Dataset.model_fields)`: same names, same order, same declared
types.

What changes is that the entry's **two tiers get named.** That test already has the
mechanism — an explicit allowlist of fields with no column behind them:

```python
CATALOGUE_ENTRY_ONLY_FIELDS = {"project_facets", "other_terms", "facets_loaded"}
```

and its comment already distinguishes kinds within it, noting that `facets_loaded`
*"differs from the two mappings in kind rather than in where its values come from"*.
So `time_range` is a two-line change plus a comment, and it opens a **third
category**: facts resolved through the describing version. It will not be the only
member — `linked`, for `Aux` at step 16, lands in the same place.

The entry's own docstring currently says *"The fields mirror the columns of `Dataset`,
so that the two cannot drift apart unnoticed"*, which read strictly is **already**
untrue of the three fields in that allowlist. `time_range` only makes the
overstatement visible, so the fix is to split the sentence rather than weaken the
promise that matters:

> An entry is one `Dataset` row — its id and its facet columns, mirrored field for
> field and held to that by `test_catalogue_entry_mirrors_dataset_columns` — **plus**
> what else is known about that dataset once a version has been chosen to speak for
> it. `VersionFilter` chooses and `describing_version` is where; `project_facets`,
> `other_terms` and `time_range` are all on that second footing.

That keeps the invariant that earns its keep — *"a facet added to `Dataset` and not to
the entry means requirements silently cannot select on it"* — exactly as strong.

### Where `cftime` can live

Settled decision 20 takes `cftime` as an optional extra. It has one architectural
consequence worth stating before anyone starts.

`esmporium.datasets` is imported by everything, and `esmporium.requirements` may
import only `esmporium.query`, `esmporium.datasets` and `esmporium.formatting`. But
`Covers` needs calendar arithmetic **at solve time** for sub-monthly data: turning
"20 years of pad" into a position on a daily axis depends on the calendar. So:

- the **value types** stay in `esmporium.datasets`, standard library only — they hold
  numbers, a unit and a calendar name, and do no calendar arithmetic;
- the **arithmetic** lives in its own module and imports `cftime` at point of use,
  exactly as `headers/hdf5.py` does with `h5py`:
  `import h5py  # the optional headers extra, imported on use`, inside a
  `try` / `except ImportError`.

The consequence is that `requirements` picks up an optional third-party dependency it
did not have before. That breaks no rule here — this package's import restrictions are
about esmporium cycles, not third parties — but it is a real change to its footprint
and belongs in the PR description.

Practically: monthly and yearly requirements work with nothing extra installed. Daily
and sub-daily need `esmporium[calendars]`, and without it the arithmetic should raise
a plain `ImportError` naming the extra, as `hdf5.py` does. That is an *environment*
problem rather than a data or feature one, so it must not become a solve status
alongside `NEEDS_ENRICHMENT` and `MISSING_FEATURE_PLEASE_RAISE_ISSUE`.

## Where the numbers come from

| Fact | Source | Cost | Available for |
|---|---|---|---|
| start, end, natural units | the filename — `…_320001-325012.nc` | free, `File.filename` already stored | every file we have seen |
| contiguity / gaps | the set of filenames for a version | free | every version |
| calendar, time units | file header global attributes | one byte-range read per *simulation* | anything we can reach a data node for |
| start, end, rich (datetimes) | file header | same read | ditto |
| branch point, raw | already stored — `DatasetVersionParent.branch_time_in_parent` | already paid | CMIP6/7 reliably; CMIP5 not usable as published |
| branch point, interpreted | enrich, or a `FIX` | — | settled decisions 7 and 12 |

Two things worth stating plainly, because both were nearly got wrong:

- **The filename does not give rich values.** It gives natural-unit start and end and
  nothing else — no calendar, no time units. Rich values require opening a file.
- **A header read cannot cheaply give start and end.** `read_global_attributes`
  returns *global* attributes only; the time coordinate's values live in the data
  section, interleaved by record, not in the first 64 KiB. The header is the source
  for *calendar and units*; the filename is the source for *start and end*. They are
  complements, not alternatives.

No filename date parsing exists anywhere in the repository today — all new code. What
is project-specific about it, and where each piece lives, is set out in "What is
project-specific, and where it lives" below.

## Frequency, units, and what needs a header

Three things are easy to run together and are independent:

- **(a) the data's frequency** — `mon`, `day`, `3hr`: decides what the filename
  encodes and whether a calendar is needed;
- **(b) the unit the requirement is stated in** — "20 years of overlap";
- **(c) whether (b) can be resolved against (a)**.

The thing that makes this tractable: **given a calendar, year and month arithmetic on
a date is exact at any frequency.** "20 years after 1850-03-01" is 1870-03-01,
unambiguously, in every CF calendar. Ambiguity only appears if you try to turn
"20 years" into a *fixed number of days* detached from a position on the axis — and
there is never a reason to do that. So:

| What we hold | Data frequency | Requirement in years/months | Requirement in days or finer |
|---|---|---|---|
| filename only | `mon`, `yr` | **exact** — a month count is calendar-independent, since 12 months is a year in every CF calendar | `MISSING_FEATURE` |
| filename only | `day` and finer | `NEEDS_ENRICHMENT` — a date range is not a length without a calendar | `MISSING_FEATURE` |
| filename + header | any | **exact** — calendar arithmetic | `MISSING_FEATURE` |

So the two statuses mean genuinely different things, and daily data is on the
supported side of the line:

- **`MISSING_FEATURE_PLEASE_RAISE_ISSUE`** — we do not support that *way of asking*.
  Nothing will make it work except us adding it, hence the issue tracker.
- **`NEEDS_ENRICHMENT`** — we support it; we just have to go and read the calendar.
  This is the ordinary path for daily and sub-daily data, not a limitation.

The edge cases of month arithmetic (31 January plus one month; 29 February plus one
year in a `standard` calendar) get pinned down by tests in PR A rather than left to
discover.

## What is project-specific, and where it lives

The pattern to mirror is `PARENT_HEADER_READERS_BY_PROJECT` in
`src/esmporium/db/parent_headers.py`: a `Protocol`, a module-level
`Mapping[str, Reader]` keyed by project, and every public function taking that
mapping as a defaulted argument so a caller can override one project without
forking anything. Its docstring already documents the idiom —
`{**PARENT_HEADER_READERS_BY_PROJECT, "CMIP5": my_reader}` — and
`tests/integration/unmodelled_project/` exists to exercise exactly this.

**Project-specific, so it needs a registry:**

1. **The filename time convention.** CMIP5/6/7 put the range in the last
   underscore-separated field (`…_320001-325012.nc`), but its precision varies with
   frequency and CORDEX differs. → `FILENAME_TIME_READERS_BY_PROJECT`.
2. **Which `reporting_interval` values mean "no time axis"** (`fx`, `Ofx`, and CMIP5's
   own spelling). → `NO_TIME_AXIS_INTERVALS_BY_PROJECT`, which is what settled
   decision 15 checks against.

**Not project-specific, so one implementation each:**

3. **Reading calendar and time units out of a header.** These are CF attributes, and
   this repo has already found that *formats are per-file, not per-project* — the same
   reason `esmporium.headers` sniffs the layout rather than keying on project.
4. **Calendar arithmetic, gap detection, combining per-file ranges into a per-version
   range, and choosing which files to read.**

```
src/esmporium/datasets/
    time.py            TimeRange, TimeAxis, BranchPoint value types
                       (here because requirements and db both need them
                        and neither may import the other)

src/esmporium/enrich/
    protocol.py        FilenameTimeReader, and the raw range it returns
    known_readers.py   FILENAME_TIME_READERS_BY_PROJECT
                       NO_TIME_AXIS_INTERVALS_BY_PROJECT
                       reader_for_project(project, readers=...)
    headers.py         read_time_axis(attributes) -> TimeAxis   (project-independent)
    calendars.py       year and month offsets per CF calendar, via cftime
    measure.py         per-file ranges -> per-version range + contiguity
```

An unknown project raises `NotImplementedError(project)`, matching
`search_api_facade/store.py`.

## How `fx` is made explicit

Settled decision 15 says `Covers` must refuse `fx` data out loud. There is an exact
precedent to copy rather than invent: `LineageStartsAtTargetError`, where the design
note says *"A leaf which can only select such datasets is refused when the
requirement is built; a leaf which selects one among others reports it unsatisfied
when solved."* The same two tiers apply here:

- a leaf whose query can **only** select `fx` data, with `Covers` attached → refused
  when the requirement is built, with a named error;
- a leaf that selects `fx` **among others** → that candidate fails when solved,
  naming the constraint and the reason;
- enrichment records `fx` versions with a "no time axis" status so no header is ever
  fetched for them twice.

`reporting_interval` is a facet we already hold, so tier one is cheap. The list of
no-time-axis values per project has to be written down somewhere — it is
project-specific, so it belongs beside the other project knowledge rather than in
generic code.

## How a group reports that we could not answer

`SolveResult` has four mappings — `satisfied`, `unsatisfied`, `ambiguous`,
`couldnt_answer` — which is what its own docstring already promises and what
`ParentStatus` sets as the house style: one answer type carrying a rich reason enum,
rather than a mapping per reason. Six top-level mappings would have been the
alternative, and would read as six unrelated outcomes rather than three verdicts and
a "not yet".

Groups in `couldnt_answer` carry the reasoning, so a reader is never left with a bare
status:

| Status | Means | What to do |
|---|---|---|
| `NEEDS_ENRICHMENT` | the fact is not recorded yet | run `enrich_datasets(result.enrichment_inputs)` |
| `MISSING_FEATURE_PLEASE_RAISE_ISSUE` | the requirement is fair but unsupported, e.g. an overlap in years against daily data | raise an issue — the message carries the link |

`enrichment_inputs` is the actionable extraction from that bucket: flat, deduplicated,
and shaped to hand straight back to the enricher. It names *which* fact is missing —
range, branch point, or calendar — so the enricher knows whether it needs a header
read or only a filename parse. Both interfaces asked for are served over one body:
PR A's pure functions take catalogue entries and have no ids at all, giving
reproducibility across machines, and PR C adds the id-driven entry point for speed
against a database you already hold.

## Plan, as five PRs

PRs A and B need no database and no network, which is the order this repo has built
every previous piece of the requirements system: prove it in memory, then wire it up.

### PR A — time information as a first-class dataset fact

- `src/esmporium/datasets/time.py` gains the value types: a time range, a time axis
  (calendar and units) and a branch point. This package imports nothing else from
  esmporium, so it can never be half of an import cycle — the same reason
  `DATASET_FACET_COLUMNS` and `AccessService` already live there.
- `src/esmporium/enrich/` gains the pure measurement functions and the per-project
  registry, laid out under "What is project-specific, and where it lives" above. All
  of them take plain values and return value types; none touches a session or a
  socket.
- `calendars.py` is the one piece with real arithmetic in it, and it is a thin wrapper
  over `cftime` rather than our own leap rules (settled decision 20), importing it at
  point of use so the monthly and yearly paths need nothing installed.
- Unit tests only, and this is where the awkwardness gets pinned down: real CMIP5,
  CMIP6 and CORDEX filenames; `monC`; a single-file dataset; a version whose files
  disagree or leave a gap; an `fx` dataset with no time axis; 31 January plus one
  month; 29 February plus one year in a `standard` calendar; and the same offset in a
  `360_day` calendar.

### PR B — the new `Covers`, proven in memory

- `src/esmporium/requirements/covers.py`: `min_overlap`, the formula above, Mode 1 as
  the default, comparison in stored units with no year conversion, the `fx` refusal,
  and an unsupported-unit outcome carrying
  `https://github.com/esmporium/esmporium/issues`. Per settled decision 17 this is a
  *returned outcome*, not a raise — so the solve finishes and the other groups still
  get answered. The wording to borrow is the developer note above
  `MixedProjectFacetError` in `solve.py`: *"the requirement is a fair thing to want
  and the answer is 'not yet' rather than 'no'"*.
- `src/esmporium/requirements/constraints.py`: `NeedsEnrichment` and the
  missing-feature outcome join `Pass | Degraded | Fail` as `Outcome` members, and the
  `Constraint` protocol docstring stops telling authors to raise when they cannot
  evaluate.
- `src/esmporium/requirements/solve.py`: `ExplanationStatusNotOk` gains
  `NEEDS_ENRICHMENT` and `MISSING_FEATURE_PLEASE_RAISE_ISSUE`, each with a place in
  `_STATUS_PRIORITY`, plus the fourth `SolveResult` mapping `couldnt_answer` and
  `enrichment_inputs`.
- Also worth doing here, though it is not strictly required once the above are
  outcomes rather than raises: **narrow the blanket `except Exception` in
  `_evaluate_constraints`**. Today it turns *any* bug inside a constraint — a typo, a
  `KeyError` — into `Fail("could not be evaluated: …")` and hence a merely
  unsatisfied group, which is how this whole class of problem stayed invisible in the
  first place.
- `tests/unit/requirements/catalogues.py`: the in-memory catalogue carries the new
  value types instead of floats.
- **Breaking**: `Covers` gains fields, so every stored requirement's hash changes —
  the same note #67 and #69 both carried.

### PR C — storage and the enrich step

- `src/esmporium/db/schema.py`: `DatasetTimeRange` and `DatasetBranchPoint`, plus a
  `TimeInfoSource` enum (`FILENAME`, `FILE_HEADER`, `FIX`). `DatasetTimeRange` is
  unique on `(dataset_version_id, source)`, exactly like `DatasetVersionParent`, so
  `SOURCE_PRECEDENCE` is inherited rather than rebuilt. Note the consequence of
  decision 3: a version can hold a `FILENAME` row *and* a `FILE_HEADER` row at once,
  and that is the point — the cheap answer and the precise one coexist, and
  precedence decides which speaks. Columns:

  | Column | Why |
  |---|---|
  | `start`, `end_exclusive` | the numbers, half-open; no extent column (settled decisions 5 and 9b). Named `end_exclusive` so the convention reaches every call site |
  | `units` | udunits-style, e.g. `months since 0001-01` or `days since 1850-01-01`. One column serves both natural and rich, so no half-empty rows (settled decision 6). Precedent: `parent_time_units` already stores this shape |
  | `calendar` | nullable, and `NULL` *means* something: we didn't have the calendar when we ingested this information |
  | `source` | `FILENAME` / `FILE_HEADER` / `FIX` |
  | `is_contiguous`, `n_files` | settled decision 8 |
  | `file_id`, `recorded_at` | provenance, as every other table here carries |
  | `start_raw`, `end_raw` | text, never used in arithmetic (settled decision 16) |
  | `status` | distinguishes "nobody measured" from "this dataset has no time axis" (`fx`), the same distinction `ParentStatus` draws between `NOT_LOOKED_FOR` and `ROOT` |

  The FK target is `DatasetVersion`, not `Dataset`, and no `Relationship()` is added —
  see "Does `Dataset` grow the same fields?" above.

- `DatasetBranchPoint` hangs off the existing child→parent link rather than off either
  dataset (settled decision 14). The raw facts are already stored on
  `DatasetVersionParent`, keyed on the child's version; this table holds the
  *interpreted* value, keyed on `parent_info_id` exactly as `DatasetParentLink` is, so
  it inherits source precedence and the raw row stays raw.

- One alembic revision via `make migration MESSAGE="…"`, then read by hand —
  autogenerate is a first draft and cannot see data migrations.
- `src/esmporium/db/enrich_to_database.py`: planning and writing, following the
  `plan_downloads` / `download` and `plan_header_reads` /
  `record_header_parent_facts` split that already exists. Reuses `get_or_create`, the
  "asserted wins, silence changes nothing" rule, and the `HeaderReadCallRecord`
  observer path.
- Header reads batched **per simulation**, not per dataset — a calendar belongs to
  the run, exactly as `plan_header_reads` already argues for parents.
- `FIX`-sourced rows reusing `esmporium.fixes` and its mandatory `reason` /
  `evidence`.
- Integration tests: the new tables round-trip through the migrated schema, beside
  `tests/integration/db/test_headers_against_migrations.py`.

### PR D — the catalogue serves it, and the integration tests that were the point

- `CatalogueEntry` gains the time fields, loaded on a `needs_time_info` flag
  mirroring `facets_loaded` / `needs_facets` from #58; `DatabaseCatalogue` and
  `InMemoryCatalogue` both fill them; `CatalogueWithTimeInfo` is vacated (not
  deleted — see "Costs" above).
- `tests/integration/requirements/`: pre-load a migrated SQLite database, enrich it,
  solve a `Covers` requirement against `DatabaseCatalogue`, and assert it agrees with
  `InMemoryCatalogueWithTimeInfo` on the same data. That agreement is the property
  `docs/further-background/data-requirements.md` already says a test holds the two
  catalogues to.
- Assert the distinction that motivated all of this: before enrichment the group is
  `needs_enrichment`, **not** `unsatisfied`; after enrichment it resolves.
- One opt-in live test behind a new marker registered in `pyproject.toml` and
  `tests/conftest.py` alongside `reads_file_headers`, with a `make` target.

### PR E — `download_by_id` (independent, land any time)

`src/esmporium/db/downloads_to_database.py`. `plan_downloads` already calls
`find_datasets(...)` and immediately reduces the result to `dataset_ids` before
calling `_versions_to_plan(session, dataset_ids, …)`. Split it at that line: the
existing query entry point keeps working, and an id-based one wraps the same body.

Worth stating in the PR description, because it answers a question asked explicitly:
**ESGF ids cannot be routed through the existing query interface.** `facet_filters`
*raises* on any name outside `DATASET_FACET_COLUMNS`, and `id_project_specific` is
deliberately excluded from it ("identify a row rather than describe it"). A facet
query is also a cross-product rather than a list, so asking for two specific datasets
by facet returns four. An id-based entry point is the only correct answer, not merely
the more convenient one.

## Verification

```
make migration MESSAGE="add time range and branch point tables"   # then read it
make migration-sql       # the schema applying every migration from scratch gives
make test                # fast suite, no network
make test-headers        # opt-in, live data nodes
```

Then end to end against a populated database: search a small CMIP6 requirement,
`enrich_datasets` what comes back, solve a `Covers` requirement, and confirm that
(a) it is satisfied where the control is long enough, (b) *before* enrichment the
group is in `couldnt_answer` with status `NEEDS_ENRICHMENT` rather than in
`unsatisfied`, and (c) `result.enrichment_inputs` feeds back into
`enrich_datasets` unmodified.

## Out of scope

`SameTimeRange` (`PLAN.md` step 15), with a code comment recording why it was left
until then. Each PR carries its own `changelog/` fragment, as every other change
here does; PRs B and C are `breaking`, because a requirement's hash covers its
constraints.
