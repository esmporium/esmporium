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
| 6 | **One pair of value columns plus a `units` column**, not a natural column group and a rich column group. No half-empty rows. Natural values are a 1-based ordinal step count with no epoch, rich values carry one; see "Natural units are an ordinal count" below. |
| 7 | A corrected branch time is a **`FIX`-sourced row**, not a Python-level catalogue wrapper. |
| 8 | A version whose files have a gap gets start and end recorded **plus a contiguity flag**, and `Covers` refuses a gapped range rather than treating it as continuous. |
| 9 | **`Covers` defaults nothing.** Both modes are first-class and the user states which one they mean, in full, every time. This follows the rule `Requirement` already sets for `group_by` — *"a grouping decides what one group is, so it is stated rather than defaulted"* — and it is forced anyway, because containment cannot be written as a number: nobody knows a dataset's extent until we have measured it. |
| 9b | **Time ranges are stored half-open, `[start, end_exclusive)`, and the field is named for it.** See "The half-open convention" below. |
| 10a | **A requirement may be stated in years or months only, for now.** `min_overlap` and `pad` in days or hours get `MISSING_FEATURE_PLEASE_RAISE_ISSUE`. This is a decision about the *API surface*, and says nothing about which data we support — see "Frequency, units, and what needs a header". |
| 10b | **Data at any frequency is supported, including daily and sub-daily** — but anything finer than monthly needs a calendar to unambiguously calculate the extent, so it needs a header read, so it reports `NEEDS_ENRICHMENT` until enriched. Monthly and yearly data never need a file opened to calculate their extent in natural units (they will need files opened to calculate extent in other units). The consequence for storage is sharper than it looks: a daily filename gives *dates*, not a count, so `start` and `end_exclusive` are **nullable** and the status says which fact is missing. |
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
| 21 | **The calendar and the time units are attributes of the *time variable*, not global attributes.** Measured, not assumed — see "Where the numbers come from". `esmporium.headers` reads globals only today, so reaching them is a new slice of that package rather than a call to `read_global_attributes`. |
| 22 | **`Covers` ships for a direct parent first; the chain is its own step.** "The control covers this experiment *and every parent back up to the control*" is the rule the scenario and ZEC analyses actually need, and it takes thinking that should not happen next to new value types and a new table. So `Covers` raises `NotImplementedError` when the covering role is not the target's direct parent, and the chain rule is settled in a PR of its own, starting from an agreed picture. |
| 23 | **Natural values are a 1-based ordinal step count, with no epoch.** `0001-01` is month 1, not month 0, which a udunits `since` epoch cannot give because `since` counts *elapsed* steps. See "Natural units are an ordinal count". |

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

**The soft thresholds survive, and have to.** `ideal_pad_years` is today the *only*
producer of `Degraded`, which is #69's "the analysis can be run, with something to say
in the write-up" outcome. Dropping it would quietly remove that outcome's only source,
so it carries over as `ideal_pad` (and `ideal_min_overlap` in overlap mode):

```
Covers(align="branch", mode="overlap",
       min_overlap=100, pad=(10, 10),            # hard: Fail if not met
       ideal_min_overlap=150, ideal_pad=(20, 20))  # soft: Degraded if not met
```

These stay genuinely optional, and that is not a breach of decision 9. "I have no
ideal" is a coherent thing to mean, and `None` says exactly that rather than standing
in for a decision the user did not make. The hard thresholds are the ones that must
always be stated. The old `pad_years=None` spelling for "nothing required, check only
the ideal" becomes explicit in the same way.

### The one piece of arithmetic

The simplification that makes this readable: **after mapping, the child's start *is*
the branch point.** `_needed_span` computes `start' = branch + (start - step_start)`,
so the mapped child start equals `branch` by construction. Everything below is in the
control's axis, after that mapping, and `BP` is the branch point.

**Case 1 — an overlap is asked for (`mode="overlap"`, `min_overlap=A`).** The control
has to hold `A` of data from the branch point — the window an anomaly actually uses —
plus padding either side:

```
                     BP
                      │
  piControl    |──────┼───────────────|
                      │
  1pctCO2             |═══════════════════════════|
                      │
         ◄── pad B ───┤◄───── A ─────►├─ pad C ─►
                      │
  needed:  [ BP - B ,  BP + A + C )
```

**Case 2 — no overlap is asked for (`mode="containment"`).** The control has to hold
everything the covered run covers, plus padding either side:

```
                     BP
                      │
  piControl    |──────┼───────────────────────────────|
                      │
  1pctCO2             |═══════════════════════════|
                      │                           │
         ◄── pad B ───┤                           ├─ pad C ─►
                      │                           │
  needed:  [ BP - B ,  end(1pctCO2) + C )
```

Written as comparisons, which is one function with the upper bound swapped:

```
child_extent = child_end_exclusive - child_start

PASS iff  child_extent >= min_overlap                        (overlap mode only)
     AND  control_start          <= branch_point - pad_before
     AND  control_end_exclusive  >= needed_end + pad_after

     where needed_end is  branch_point + min_overlap   in overlap mode
                      or  child_end_exclusive          in containment mode
```

Case 2 is `_covers()` exactly as #69 implements it, so one private function serves
both modes.

The analysis window being **anchored at the branch point** is the part worth not
losing. An earlier draft of this note let the window slide anywhere inside the
overlap, which passes cases where the control does not reach the branch point at all:

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

### Along a chain, which is deferred

Both pictures above put the control one branch below the covered run. The rule the
scenario and ZEC analyses actually need is the chain version: *the control must span
the covered run **and every parent back up to the control**, plus padding before the
branch point and after the end of the covered run* — where the branch point is the one
between the control and the **first** experiment that branches off it, the bottom of
the chain.

```
                     BP = where historical branches off piControl
                      │
  piControl    |──────┼─────────────────────────────────────────|
                      │
  historical          |═══════════════════|
  ssp245                                  |═══════════════════|
                      │                                       │
         ◄── pad B ───┤                                       ├─ pad C ─►

  case 2 (no A):   [ BP - B , max(end of ssp245, end of historical) + C )
  case 1 (A given):[ BP - B , BP + A + C )
```

Per settled decision 22 this does **not** ship with `Covers`: a covering role which is
not the target's direct parent raises `NotImplementedError`, and the chain rule gets a
step of its own, agreed as a picture before any code. The questions it has to settle,
recorded here so they are not rediscovered: what happens when an intermediate run's
mapped end exceeds the covered run's; whether `A` is ever measured anywhere but the
bottom branch point; and what a gapped intermediate does, given decision 8 refuses a
gapped range. The mapping machinery is not the hard part — `_needed_span` already
walks every member one branch at a time — the *rule* is.

### The half-open convention

Settled decision 5 notes that an extent is sometimes `end - start` and sometimes
`end - start + 1`, and that this has tripped over many people. **Storing half-open is
how we stop it being "sometimes".**

A range is `[start, end_exclusive)`, and the field is *named* `end_exclusive` rather
than `end`, so the convention travels to every call site, query and log line instead
of living in a docstring somebody read once. That is the same move this repo already
made with `reporting_interval` over `frequency`, and with
`branch_time_in_parent` / `branch_time_in_child`.

"Half-open" is the one word in this note that reads as jargon, so: it means we store
the first step *not* in the data rather than the last step in it. Count the boxes, not
the posts.

```
 yearly data published as "1850 to 1860"

        1850   1851   ...    1859   1860
      ┌──────┬──────┬──────┬──────┬──────┐
      │      │      │      │      │      │
      └──────┴──────┴──────┴──────┴──────┘
      ▲                                  ▲
      start = 1850                       end_exclusive = 1861
                                         the first year NOT in the data

 extent = 1861 - 1850 = 11 years
```

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
filename  "…_320001-325012.nc"   + reporting_interval "mon"
   │
   ├─ PublishedRange(start_raw="320001", end_raw="325012",
   │                 precision=MONTHS, reports_every=1 month)   inclusive, as published
   │
   └─ to_half_open(...)        <- the ONLY place the +1 lives
         │
         └─ TimeRange(start=38389, end_exclusive=39001, axis=months)     half-open
```

The numbers come from decision 23's ordinal: `(3200 - 1) * 12 + 1 = 38389` for January
3200, and `(3250 - 1) * 12 + 12 = 39000` for December 3250, whose half-open end is
`39001`. The extent is `39001 - 38389 = 612` months, i.e. 51 years, which is what
fifty-one calendar years of monthly data should come to.

Note the second input. **The half-open end is the last stamp advanced by one
*reporting* step**, not by one of whatever the filename was written to, and those are
not the same thing: a CMIP6 `3hr` file is stamped to the minute
(`185001010300-185101010000`, twelve digits) and runs three hours past its last stamp,
to midnight. Taking the filename's own precision as the step would quietly lose two
hours off every sub-daily dataset. The reporting interval is a facet we already hold,
so asking for it costs nothing, and one rule — *end exclusive is the last stamp plus
one reporting step* — then gives an exact answer at every frequency from `dec` down to
`1hr`. `subhr` is the one interval with no answer: it says the data is finer than
hourly without saying how much finer, so there is no step to add.

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

### Natural units are an ordinal count

Decision 6 said "one `units` column, udunits-style", and the obvious reading — natural
monthly values as `"months since 0001-01"` — is wrong by one. A udunits `since` counts
*elapsed* steps, so `0001-01` would be month **0**. What we want is the ordinal: the
first month of year 1 is month 1.

So a natural value carries a step and **no epoch**, and the arithmetic is plain:

```
 YEARS    value = year                        1850      ->  1850
 MONTHS   value = (year - 1) * 12 + month     0001-01   ->  1
                                              1850-01   ->  22189
                                              1861-01   ->  22321
                                              extent 22321 - 22189 = 132 months = 11 years
```

An epoch is exactly what a natural value does not have, and saying so is the point: a
count of years or months is the same count in every CF calendar, which is why monthly
and yearly data never need a header. An epoch date would imply a calendar the value
does not carry.

Rich values, from a header, do carry one, and keep udunits' own spelling —
`days since 1850-01-01` — because that is the string the file wrote and
`parent_time_units` already stores that shape. One column holds either:

```
  natural   "months"                    epoch is None
  rich      "days since 1850-01-01"     epoch is "1850-01-01"
```

`DAYS` and `HOURS` have no calendar-free ordinal at all — two dates are a count of
days only once leap rules are known — so a sub-monthly filename fills `start_raw` and
`end_raw` and nothing else, which is what decision 10b's nullability is for.

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
- the **arithmetic** lives in its own module, `esmporium.datasets.calendars`, and
  imports `cftime` at point of use, exactly as `headers/hdf5.py` does with `h5py`:
  `import h5py  # the optional headers extra, imported on use`, inside a
  `try` / `except ImportError`. It sits in `datasets` rather than `enrich` because
  `requirements` may import the one and not the other, and the only rule `datasets`
  has to keep — no esmporium imports — is untouched by a third party.

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
| calendar, time units | file header, the **time variable's** attributes — *not* global ones | one byte-range read per *simulation*, plus a new reader (see below) | anything we can reach a data node for |
| start, end, rich (datetimes) | file header | same read | ditto |
| branch point, raw | already stored — `DatasetVersionParent.branch_time_in_parent` | already paid | CMIP6/7 reliably; CMIP5 not usable as published |
| branch point, interpreted | enrich, or a `FIX` | — | settled decisions 7 and 12 |

Two things worth stating plainly, because both were nearly got wrong:

- **The filename does not give rich values.** It gives natural-unit start and end and
  nothing else — no calendar, no time units. Rich values require opening a file.
- **A header read cannot cheaply give start and end.** The time coordinate's *values*
  live in the data section, interleaved by record, not in the first 64 KiB. The header
  is the source for *calendar and units*; the filename is the source for *start and
  end*. They are complements, not alternatives.

- **And the calendar is not a global attribute**, which is settled decision 21 and was
  measured rather than assumed. Against this repository's own captured prefixes:

  | File | `calendar` present? | Where |
  |---|---|---|
  | `tests/test-data/headers/cmip5-netcdf3-prefix.bin` (6,364 B, enough for every global) | **no** — nor `days since`, not one byte of either | the globals end before the variable list begins |
  | `tests/test-data/headers/cmip6-netcdf4-prefix.bin` (42,487 B) | yes — `calendar`, `noleap`, `days since` | byte ~24,900, inside the 64 KiB `DEFAULT_WANT_BYTES` ask |

  `read_global_attributes` returns globals only, and
  `read_netcdf3_global_attributes` stops at the end of the global attribute section
  without parsing the variable list at all. So reaching the calendar is a **new slice
  of `esmporium.headers`** — `read_variable_attributes(prefix, "time", file_size=…)`
  — and not a call to something that exists.

  Worth recording why that reader cannot just be h5py. h5py reads HDF5; classic
  netCDF is a different format, beginning `CDF\x01`, and h5py refuses it outright
  (`OSError: file signature not found`, confirmed against the CMIP5 prefix above).
  Classic is most of CMIP5, so "h5py for everything" would mean no CMIP5 calendars at
  all. `netCDF4` can read both, and is rejected for the reason `headers/hdf5.py`'s
  docstring already gives: it does its own HTTP inside the C library, so a stalled
  read cannot be timed out from Python and an HTTP status arrives as text inside an
  `OSError`. So `netcdf3.py` grows a variable-list walk instead — the list sits
  directly after the global attributes and reuses the same `NC_ATTRIBUTE` structure
  the parser already reads, so it is a short addition rather than a new parser. The
  one trap is `CDF\x02`, where `begin` widens to eight bytes.

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
   frequency and other projects may differ. → `FILENAME_TIME_READERS_BY_PROJECT`.
2. **Which `reporting_interval` values mean "no time axis"** (`fx`, `Ofx`, and CMIP5's
   own spelling). → `NO_TIME_AXIS_INTERVALS_BY_PROJECT`, which is what settled
   decision 15 checks against. It lives in `esmporium.datasets` rather than beside the
   other two, for the reason the layout below gives.

**Both are keyed to the projects esmporium has a query class for, and no others**:
CMIP5, CMIP6, CMIP6Plus and CMIP7. An entry is a claim about a convention we have
seen, so a project we cannot search is one we do not register a reader for either,
however likely it is that the shared reader would work. CORDEX-CMIP6 is the case which
makes the rule worth stating: its trailing field looks like everyone else's, and
`tests/integration/requirements/test_cordex_other_terms_live.py` searches it end to
end — but only by injecting a facade, a selector and a query mapping, none of which
ship, and no CORDEX filename has ever been recorded in this repository to check a
reader against. These two registries are simply two more mappings to inject beside the
others.

The asymmetry in the cost is what settles it. A missing project is an
`UnknownProjectFilenameTimeReaderError` naming the override to pass. A *wrong* project
is silent: `read_trailing_range` answers `None` for any filename whose last field is
not two numbers, and `None` means "this file carries no range", which is the ordinary
answer for a fixed field. A convention we had misread would be recorded as a dataset
with no time axis.

**Not project-specific, so one implementation each:**

3. **Reading calendar and time units out of a header.** These are CF attributes on the
   time variable (decision 21), and this repo has already found that *formats are
   per-file, not per-project* — the same reason `esmporium.headers` sniffs the layout
   rather than keying on project.
4. **Calendar arithmetic, gap detection, combining per-file ranges into a per-version
   range, and choosing which files to read.**

```
src/esmporium/datasets/
    time.py            TimeStep, TimeUnits, TimeAxis, TimeRange, Span,
                       BranchPoint, VersionTimeRange value types
                       (here because requirements and db both need them
                        and neither may import the other)
    calendars.py       year and month offsets per CF calendar, via cftime
                       (here, not in enrich/, because `Covers` needs it at
                        solve time and `requirements` may not import `enrich`)
    no_time_axis.py    NO_TIME_AXIS_INTERVALS_BY_PROJECT, has_no_time_axis
                       (here for the same reason: `Covers` refuses `fx` data
                        both when a requirement is built and when it is solved)

src/esmporium/enrich/
    protocol.py        FilenameTimeReader, and the raw range it returns
    known_readers.py   FILENAME_TIME_READERS_BY_PROJECT
                       SPAN_BY_REPORTING_INTERVAL
                       reader_for_project(project, readers=...)
    headers.py         read_time_axis(attributes) -> TimeAxis   (project-independent;
                       `attributes` are the time variable's, per decision 21)
    measure.py         per-file ranges -> per-version range + contiguity

src/esmporium/headers/
    netcdf3.py         grows a variable-list walk, so that
    hdf5.py            read_variable_attributes(prefix, "time", ...) exists at all
    parsing.py         (decision 21; lands with the storage step, where reads happen)
```

`calendars.py` sitting in `datasets` rather than `enrich` is forced, not stylistic:
`tests/unit/requirements/test_package.py` asserts the *exact* set of esmporium packages
`requirements` imports, and `datasets` is on it while `enrich` is not. The rule
`datasets` has to keep is "imports nothing else *from esmporium*", which a third-party
`cftime` does not breach.

Two entries above were moved into `datasets` while PR B was built, by that same force
rather than by new reasoning, and the rule is worth stating once because the next piece
of project knowledge a constraint needs will hit it too: **anything a constraint reads
at solve time has to live in `datasets`**, whatever else it has in common with its
neighbours in `enrich`.

- `VersionTimeRange` is a value, and `Covers` reads one to get at the contiguity flag
  decision 8 turns on, so it belongs beside the other values. `measure_version`, which
  produces it, stays in `enrich/measure.py`: the measurement is enrichment's, the value
  is not.
- The no-time-axis vocabulary is project knowledge, which would put it beside the other
  two registries, except that decision 15's refusal happens inside `requirements` --
  at solve time *and* when a requirement is built.

Both are re-exported from `esmporium.enrich`, so the three registries are still
reachable from one place.

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

## Plan, as six PRs

PRs A and B need no database and no network, which is the order this repo has built
every previous piece of the requirements system: prove it in memory, then wire it up.

### PR A — time information as a first-class dataset fact

- **This note goes first**, in its own commit: decisions 21 to 23, the pictures above
  and the scope split all land before any code, because every later PR gets read
  against it.
- `src/esmporium/datasets/time.py` gains the value types: a step, units, a time axis
  (calendar and units), a time range, a span and a branch point. This package imports
  nothing else from esmporium, so it can never be half of an import cycle — the same
  reason `DATASET_FACET_COLUMNS` and `AccessService` already live there.
- `src/esmporium/enrich/` gains the pure measurement functions and the per-project
  registry, laid out under "What is project-specific, and where it lives" above. All
  of them take plain values and return value types; none touches a session or a
  socket.
- `src/esmporium/datasets/calendars.py` is the one piece with real arithmetic in it,
  and it is a thin wrapper over `cftime` rather than our own leap rules (settled
  decision 20), importing it at point of use so the monthly and yearly paths need
  nothing installed.
- Unit tests only, and this is where the awkwardness gets pinned down: real CMIP5,
  CMIP6 and CMIP7 filenames, the last taken off a recorded ESGF-NG response; `monC`; a
  single-file dataset; a version whose files disagree or leave a gap; an `fx` dataset
  with no time axis; a project we do not read being refused rather than guessed at; 31
  January plus one month; 29 February plus one year in a `standard` calendar; and the
  same offset in a `360_day` calendar.

### PR B — the new `Covers`, proven in memory, direct parent only

- `src/esmporium/requirements/covers.py`: a required `mode` with `min_overlap` and
  `pad` beside it and nothing defaulted (settled decision 9), the soft `ideal_*`
  thresholds kept so `Degraded` still has a producer, the two cases drawn above,
  comparison in stored units with no year conversion, the `fx` refusal,
  **`NotImplementedError` when the covering role is not the target's direct parent**
  (settled decision 22), and an unsupported-unit outcome carrying
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
- The two tests which solve a two-hop chain —
  `test_branch_chain_back_to_the_control` and
  `test_branch_chain_control_must_cover_child_and_parents` — become assertions that a
  longer chain raises, and PR B2 restores them. `test_use_cases.py` is untouched,
  because its `satisfying_catalogue` fixture fabricates exactly one hop by design, so
  `05-zec-flat10` and `11-tas-scenario-anomalies` keep solving; their docstrings gain
  a note that the intermediate run is only covered once B2 lands.

### PR B2 — coverage along a chain

Settled decision 22's other half, and the one piece of this whole step whose *rule* is
not yet written down. It starts with the picture under "Along a chain, which is
deferred" above, agreed before any code, and it answers the three questions recorded
there. Nothing else moves in it: no new value types, no new table, no new column — so
the only thing under discussion is the rule.

### PR C — storage and the enrich step

- **First**, decision 21's reader: `netcdf3.py` grows a variable-list walk, `hdf5.py`
  grows the matching path, and `read_variable_attributes(prefix, name, file_size=…)`
  joins `read_global_attributes` in `parsing.py`. Without it nothing can record a
  calendar for a classic netCDF file, which is most of CMIP5.

- `src/esmporium/db/schema.py`: `DatasetTimeRange` and `DatasetBranchPoint`, plus a
  `TimeInfoSource` enum (`FILENAME`, `FILE_HEADER`, `FIX`). `DatasetTimeRange` is
  unique on `(dataset_version_id, source)`, exactly like `DatasetVersionParent`, so
  `SOURCE_PRECEDENCE` is inherited rather than rebuilt. Note the consequence of
  decision 3: a version can hold a `FILENAME` row *and* a `FILE_HEADER` row at once,
  and that is the point — the cheap answer and the precise one coexist, and
  precedence decides which speaks. Columns:

  | Column | Why |
  |---|---|
  | `start`, `end_exclusive` | the numbers, half-open; no extent column (settled decisions 5 and 9b). Named `end_exclusive` so the convention reaches every call site. **Nullable**, because a sub-monthly filename gives dates rather than a count (settled decision 10b), and then `status` says which fact is missing |
  | `units` | `months` or `years` for a natural value, which carries no epoch (settled decision 23), and udunits' own `days since 1850-01-01` for a rich one. One column serves both, so no half-empty rows (settled decision 6). Precedent: `parent_time_units` already stores the rich shape |
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
until then. Coverage along a chain, which is PR B2 rather than part of `Covers` itself
(settled decision 22). Each PR carries its own `changelog/` fragment, as every other
change here does; PRs B and C are `breaking`, because a requirement's hash covers its
constraints.
