# Enrichment, part one: the time information ESGF does not publish

Design note for the `enrich` step, and for making
[`Covers`](data-requirements.md#constraints) work against a real catalogue rather
than a hand-written one. Like
[the requirements design note](data-requirements.md), it lands ahead of the
implementation, so that every later PR can be read against a stated target.

Written: 2026-11-12.

/// note | Status, 2026-11-16
PRs A, B and B2 below are built
([#71](https://github.com/esmporium/esmporium/pull/71),
[#72](https://github.com/esmporium/esmporium/pull/72),
[#73](https://github.com/esmporium/esmporium/pull/73)).
Building them, and reviewing them, changed parts of this note.
It is kept as the record of why things were built the way they were,
and these parts are superseded:

- **Where the numbers come from** is replaced by
  [part two](enrichment-sources.md), which adds search records as a source,
  separates header, variable attributes and variable values,
  and settles which tools read them.
- **PRs C, D and E** are replaced by
  [part three](enrichment-storage.md#the-plan-from-here),
  which also adds three corrective PRs (R1 to R3) to what is built.
- **Does `Dataset` grow the same fields?** is revised:
  a catalogue entry becomes one *version* of a dataset, chosen once when it is found
  (part three, decision 32). That section is now
  **What does a catalogue entry carry, and for which version?**
- **Decision 4** is narrowed (published numbers stay as published, see below),
  `Covers`' **`align`** argument goes (part three, decision 33),
  and the mapping goes through `branch_time_in_child`
  rather than the child's own start (part three, PR R2).
///

/// note | Status, 2026-11-24
Parts two and three changed more of this note than the banner above records, and
those changes are now made in place rather than left to be noticed:

- **the sources**, which are six rather than four, and which of them gives a start
  and an end: a file header gives neither, the time bounds give both exactly, and a
  search record gives a version's first and last time value for nothing
  ([part two](enrichment-sources.md#the-short-version); decisions 3, 24 and 27);
- **the readers**: our own classic netCDF parser and `h5netcdf`, over one byte source
  that is HTTP ranges or a local file, and no `netCDF4` extra
  ([part two](enrichment-sources.md#the-decision); decision 25);
- **the storage**, which is five tables rather than two, raw text per *file* and the
  combined range per *version*
  ([part three](enrichment-storage.md#storage-pr-c3); decisions 6 and 16);
- **when a conversion happens**: at enrich time, stored beside the published value,
  so no solve converts anything ([part three](enrichment-storage.md), decision 31;
  decision 20 and "Where `cftime` can live" below);
- **what a catalogue entry is**: one *version* of a dataset, chosen once when it is
  found ([part three](enrichment-storage.md), decision 32, built in PR R3).

**The order of work, and where we are up to, live in `PLAN.md` step 12c**, not here.
This note is the record of the decisions and of why A, B and B2 look the way they do.
///

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
its calendar, or an interpreted branch point. `PLAN.md` says so outright at the step
`Covers` belongs to (12b, which was step 13): the constraint cannot be finished until
something records a time range.

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
| 1 | The step is **`enrich`** / enrichment. `src/esmporium/enrich/`, `enrich_datasets(...)`, PLAN step 12c, "Enrichment". |
| 2 | The new outcome is **`NeedsEnrichment`**, not `Undetermined` — "undetermined" implies we might look and still not know, when in fact we just have to go and do the work so that the answer is available. |
| 3 | **Store both** natural and rich time values. Natural values are ones in the dataset's own frequency and come from the filename, which is basically free to parse and is free (natural values match the dataset's frequency e.g. for yearly datasets you get values like 2024, 1840, for monthly datasets values like 1850-01, 2023-12); rich comes from a file's **time bounds** and is read only where needed (rich means information that allows you to create a full datetime object without guessing i.e. values like 2023-01-01 00:00:00, you don't get this from the filenames alone). *Corrected 2026-11-24:* this said rich values come from a file header. They do not. No CMIP5 or CMIP6 file sampled carries `time_coverage_start` or `time_coverage_end`, so a header gives a calendar and units but neither a start nor an end; the first lower and last upper time bound give both, exactly, for a few small ranged reads ([part two](enrichment-sources.md#variable-values)). |
| 4 | **Never store fractional years, and never convert to them.** Natural units are the data's own frequency; rich values are datetimes with a calendar. No year conversion at solve time, and none at enrich time. *Narrowed 2026-11-16:* this is about how extents are stated and compared, not about published data. A branch time published as a float is stored as that float with its units ([part three](enrichment-storage.md), decision 31), and the `BranchPoint` built in PR A, which floors it to an `int`, is corrected in PR R1. |
| 5 | **Store start and end, not extents.** An extent is `end - start` (or `end - start + 1` sometimes because of how the indexing/conversion works e.g. the extent of a dataset that goes from 1850 to 1860 when its natural units are years is 11 years, not 10, because the actual bounds can be assumed to be 1850-01-01 to 1860-12-31, this has tripped over many people, hopefully not us), computed outside the database when wanted. Storing both would be the same fact twice, with a consistency burden (that is impossible to get right given that the correct answer depends on assumptions/context that we don't have at enrichment time). |
| 6 | **One pair of value columns plus a `units` column**, not a natural column group and a rich column group. No half-empty rows. Natural values are a 1-based ordinal step count with no epoch, rich values carry one; see "Natural units are an ordinal count" below. *Revised 2026-11-24:* the pair of columns and the one `units` column survive, split over two tables — the raw text per file (`FileTimeRange`) and the combined range per version (`DatasetVersionTimeRange`), whose uniqueness includes `units`, so one source can give both a natural row and a dated one ([part three](enrichment-storage.md#storage-pr-c3)). |
| 7 | A corrected branch time is a **`FIX`-sourced row**, not a Python-level catalogue wrapper. |
| 8 | A version whose files have a gap gets start and end recorded **plus a contiguity flag**, and `Covers` refuses a gapped range rather than treating it as continuous. |
| 9 | **`Covers` defaults nothing.** Both modes are first-class and the user states which one they mean, in full, every time. This follows the rule `Requirement` already sets for `group_by` — *"a grouping decides what one group is, so it is stated rather than defaulted"* — and it is forced anyway, because containment cannot be written as a number: nobody knows a dataset's extent until we have measured it. |
| 9b | **Time ranges are stored half-open, `[start, end_exclusive)`, and the field is named for it.** See "The half-open convention" below. |
| 10a | **A requirement may be stated in years or months only, for now.** `min_overlap` and `pad` in days or hours get `MISSING_FEATURE_PLEASE_RAISE_ISSUE`. This is a decision about the *API surface*, and says nothing about which data we support — see "Frequency, units, and what needs a header". |
| 10b | **Data at any frequency is supported, including daily and sub-daily** — but anything finer than monthly needs a calendar to unambiguously calculate the extent, so it needs a header read, so it reports `NEEDS_ENRICHMENT` until enriched. Monthly and yearly data never need a file opened to calculate their extent in natural units (they will need files opened to calculate extent in other units). The consequence for storage is sharper than it looks: a daily filename gives *dates*, not a count, so `start` and `end_exclusive` are **nullable** and the status says which fact is missing. |
| 11 | A **sibling** pair whose axes do not align is a **loud `Fail`** naming the constraint, not a raise: it is a question we can answer, and the answer is no. |
| 12 | An **uninterpretable raw CMIP5 branch time** is `NeedsEnrichment` pointing at a fix. |
| 13 | **`SameTimeRange` is deferred** (PLAN step 14), with a comment in the code recording this decision so it is not lost when step 14 lands. |
| 14 | **The branch point lives on the edge, not on either dataset** — a deliberate departure from ESGF, whose data model has no edges. The *facts* come from the child (which is where they are published and where `DatasetVersionParent` already stores them); the *place they belong* is the child→parent link. |
| 15 | **`Covers` does not apply to `fx` data at all, and says so explicitly** rather than relying on nobody ever attaching it to an `fx` leaf. |
| 16 | **Keep the raw text beside the parsed value** (`start_raw`, `end_raw`). The string is the observation; the number is our parse of it. Different facts, and keeping the string lets a wrong parser be corrected without re-reading anything. *Revised 2026-11-24:* the raw text is kept **per file**, on `FileTimeRange`, because a version's range is combined from many files and no one pair of stamps is the raw form of it. |
| 17 | An unsupported requirement unit is **reported as a group that failed to solve**, with a status along the lines of `MISSING_FEATURE_PLEASE_RAISE_ISSUE` pointing at the issue tracker — *not* an exception that aborts the solve. |
| 18 | The actionable list is **`enrichment_inputs`** — it names what you pass to the enricher. |
| 19 | `SolveResult` stays at **four mappings**: `satisfied`, `unsatisfied`, `ambiguous`, `couldnt_answer`. The fourth holds every group we could not answer, **with the reasoning** — each group carries which status applied and why. (`couldn't_answer` cannot be a Python identifier, so the apostrophe is dropped.) |
| 20 | **Calendar arithmetic uses `cftime`, from a new optional extra. We do not roll our own.** Six CF calendars with six sets of leap rules is exactly the job that looks small and is not. It is an *extra* rather than a dependency, following `h5py`: monthly and yearly requirements never need it, because twelve months is a year in every calendar, so only the sub-monthly path pays. See "Where `cftime` can live" below for the one architectural consequence. *Revised 2026-11-24:* the conversions are done at enrich time and stored ([part three](enrichment-storage.md), decision 31), so no solve converts anything, not even a sub-monthly one. `cftime` is still an optional extra; it is enrichment that needs it. |
| 21 | **The calendar and the time units are attributes of the *time variable*, not global attributes.** Measured, not assumed — see "Where the numbers come from". `esmporium.headers` reads globals only today, so reaching them is a new slice of that package rather than a call to `read_global_attributes`. |
| 22 | **Coverage along a chain is a stitch.** "The control covers this experiment *and every parent back up to the control*" is the rule the scenario and ZEC analyses actually need, and the target and its parents are judged as *one continuous series* mapped onto the covering dataset's axis a branch at a time. The anchor is the bottom branch point, the needed end is the target's mapped end, and a break anywhere in the series — a gap inside a member, or a run which branched where its parent holds no data — is a failure. See "Along a chain" below. |
| 23 | **Natural values are a 1-based ordinal step count, with no epoch.** `0001-01` is month 1, not month 0, which a udunits `since` epoch cannot give because `since` counts *elapsed* steps. See "Natural units are an ordinal count". |

## The `Covers` question, settled

Two modes over the same stored values, and the user names which one they mean —
nothing is defaulted (settled decision 9):

```
Covers(mode="containment", pad=(10, 10))
Covers(mode="overlap", min_overlap=100, pad=(10, 10))
```

One class with a required `mode` rather than two classes, because the lineage mapping
is common to both and two classes would duplicate that surface.
`mode` is required rather than inferred from whether `min_overlap` was given, because
inferring it is exactly the kind of default decision 9 rules out — and because
containment cannot be written as a number anyway.

**The soft thresholds survive, and have to.** `ideal_pad_years` is today the *only*
producer of `Degraded`, which is #69's "the analysis can be run, with something to say
in the write-up" outcome. Dropping it would quietly remove that outcome's only source,
so it carries over as `ideal_pad` (and `ideal_min_overlap` in overlap mode):

```
Covers(mode="overlap",
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

> *Revised 2026-11-16.* The `step_start` above became, in the built code, the child's own
> first data step: a child position `p` maps to `in_parent + (p - child_start)`. That
> assumes the child's data starts exactly at its branch, which is false whenever a leading
> file is missing. The mapping CMIP defines is through `branch_time_in_child`:
> `in_parent + (p - in_child)`, with units checked at each step. Where `in_child` is
> not published (CMIP5) it is assumed to be the child's first time bound, and the
> assumption is named in every answer that uses it. PR R2 in
> [part three](enrichment-storage.md#pr-r2-covers-checks-units-where-it-computes-and-maps-through-branch_time_in_child)
> makes that change. The pictures below are unaffected, because they draw a child whose
> data does start at its branch.

**Overlap mode — a window is asked for (`min_overlap=A`).** The control has to hold
`A` of data from the branch point — the window an anomaly actually uses — plus padding
either side:

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

**Containment mode — the whole covered run.** The control has to hold everything
the covered run covers, plus padding either side:

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

Containment is `_covers()` exactly as #69 implements it, so one private function serves
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

### Along a chain

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

  containment:  [ BP - B , end of ssp245 mapped + C )
  overlap:      [ BP - B , BP + A + C )
```

The model is a **stitch**: the covered run and every parent up to the covering one are
one continuous series, mapped onto the covering dataset's axis a branch at a time.
Each run in between contributes how far into its own parent it branched, measured from
that parent's own start, so the covered run's start lands wherever the chain of
branches puts it. That makes the comparison the same one as for a single hop, with the
anchor and the mapped end composed rather than read off one branch point.

The three questions this left open, and the answers:

- **An intermediate which outlasts its child does not raise the bar.** The needed end
  is the covered run's mapped end, not `max` over every member. The analysis never
  uses 1pctCO2's years after the ZEC run left it, and because each child's mapped start
  *is* its parent's branch point, the path through the chain is exactly
  `[BP, covered run's mapped end)` — one span, no maximum. Wanting the control over the
  whole of an intermediate as well is a second `Covers` with `target="chain.0"`, which
  composes; folding it into one check would answer two questions with one verdict.
- **`A` is measured from the bottom branch point, and nowhere else.** The overlap
  window is the baseline an anomaly is taken against, and for a scenario that baseline
  is the control concurrent with the *start* of the chain. The consequence worth
  stating: the companion test — that there is something to overlap *with* — is then
  about the stitched series rather than the covered run alone, so a covered run shorter
  than `A` still meets it when the parents in between fill the window. Measuring `A`
  from the covered run's own mapped start instead is coherent and unsupported; saying
  which is a field, so it waits for somebody to ask.
- **A gapped member fails, and so does a broken join.** Decision 8 refuses a gapped
  range because a gap treated as continuous reports a missing decade as covered, and
  that reasoning does not weaken along a chain: the whole premise is that a continuous
  series can be stitched. The stitch also has *joins*, which contiguity does not catch
  — a run which branched after its parent's data ends leaves a hole, and one which
  branched before its parent's data starts has no parent data at the branch at all.
  The second is the worse of the two, because the mapping is computed from the
  intermediate's own start, so a missing leading file shifts every position above it
  while nothing about the arithmetic looks wrong. Both are a failure naming the hop.
  The join onto the covering dataset itself is not checked separately: whether *it*
  holds data at the branch point is the coverage question, and it is answered with the
  window it needed in the message.

One more thing the chain decides, which a single hop never had to: **which datasets
are in the stitch.** It is the inclusive run from `target` up to `role` and nothing
else, so a member of the lineage below `target` or above `role` is neither mapped, nor
gap-checked, nor named as something to enrich. A `target="chain.0"` check which
reported `NEEDS_ENRICHMENT` for the leaf below it would send somebody to fetch headers
for a dataset the check never reads.

The mapping machinery was never the hard part — the arithmetic walks every member one
branch at a time — the *rule* was.

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

> *Revised 2026-11-16.* "Exact" overstates it. The rule is an *assumption*: that the
> data runs to the end of its last time bound, and that the bound is one reporting
> interval wide. That holds when the bounds are contiguous and regular, and is only a
> convention for point data (`…Pt`), whose values are instants. So a filename's end is
> recorded as derived under that assumption, and the time bounds are what check it.
> See [part two](enrichment-sources.md#filenames).

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
something is wrong with the data. The distinction must not be optimised away, and a
code comment records it, per settled decision 13.

> *Revised 2026-11-16.* The distinction stands, but it is drawn between two classes
> rather than inside one: `Covers` loses its `align` argument and is only ever
> branch-aligned, and comparing on a shared calendar waits for `SameTimeRange`
> ([part three](enrichment-storage.md), decision 33). The developer note in
> `covers.py` is kept and re-pointed so step 14 still finds it.

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
   a stored fact — `Aux` and `linked` (step 15), anything download- or
   checksum-aware — would otherwise mean another protocol and another runtime-raise
   path. As fields it is one mechanism with one loading flag.
3. *The reason it was not done this way has gone.* #69 kept it on the catalogue partly
   so a buggy branch time could be corrected by wrapping and overriding. Settled
   decision 7 replaces that with a recorded `FIX` row, which is durable, auditable and
   shared. The wrapper no longer needs to exist.

**Costs, stated honestly:**

1. *`CatalogueEntry`'s invariant weakens.* Its docstring says the fields "mirror the
   columns of `Dataset`, so that the two cannot drift apart unnoticed". A time range
   is a `DatasetVersionTimeRange` row, not a `Dataset` column, so the entry becomes
   "a version plus facts hanging off it". That must be written into the docstring,
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
   is already downloaded", cf. PLAN step 21), which is not a property of the dataset
   because two machines answer differently for the same one.

### What does a catalogue entry carry, and for which version?

**A catalogue entry is one *version* of a dataset, chosen once when it is found, and
`entry.id` is that `DatasetVersion`'s id** ([part three](enrichment-storage.md),
decision 32, built in
[PR R3](enrichment-storage.md#pr-r3-a-catalogue-entry-is-a-version)). `VersionFilter`
is used by `find`, and by `parent_of` as a check on the parent version its link
records, and nowhere after.

> *Revised 2026-11-24.* This section was headed "Does `Dataset` grow the same
> fields?" and answered that an entry stays one `Dataset` row with every lookup
> resolving its describing version, the way `parent_of` already did. That argument is
> cut rather than kept, because it fails for exactly the facts this note adds:
> `get_time_range(entry)` is handed no `VersionFilter`, so it could only guess which
> filter the solve used, and could answer for a different version than the one whose
> facets and parent the solve had read.

`Dataset` itself grows nothing either way, and two of the three reasons are unchanged:

- **A time range is per *version*, not per dataset**, so the storage hangs off
  `DatasetVersion` — `DatasetVersionTimeRange`, `DatasetVersionTimeAxis` and
  `DatasetVersionBranchPoint` all do.
- **That pattern is already built and shipped.** `DatasetVersionParent` is keyed on
  the *child's version*, which is how #67 does parents, and time information follows
  the same road rather than opening a new one. (PR C3 renames it
  `DatasetVersionParentInfo`, so the new tables can be named in one pattern.)
- **No ORM relationships.** `schema.py:337` has the one `Relationship()` in the
  codebase commented out, marked *"TODO: bring this back in once we start doing
  versions"*. Everything else is explicit `select`s. Adding relationship fields to
  `Dataset` would reverse a deliberate choice, so it is out.

What changes for `CatalogueEntry` is that its **two tiers get named.**
`test_catalogue_entry_mirrors_dataset_columns` (`tests/unit/test_schema.py`) already
has the mechanism — an explicit allowlist of fields with no column behind them:

```python
CATALOGUE_ENTRY_ONLY_FIELDS = {"project_facets", "other_terms", "facets_loaded"}
```

and its comment already distinguishes kinds within it, noting that `facets_loaded`
*"differs from the two mappings in kind rather than in where its values come from"*.
So `time_range` is a two-line change plus a comment, and it opens a **third
category**: facts known about the version the entry *is*. It will not be the only
member — `linked`, for `Aux` at step 15, lands in the same place. The test's own
assertion changes with R3: the facet fields still mirror `Dataset`'s columns, field
for field, and `id` and the version fields mirror `DatasetVersion`'s.

The entry's own docstring currently says *"The fields mirror the columns of `Dataset`,
so that the two cannot drift apart unnoticed"*, which read strictly is **already**
untrue of the three fields in that allowlist. `time_range` only makes the
overstatement visible, so the fix is to split the sentence rather than weaken the
promise that matters:

> An entry is one `DatasetVersion` — its id, its version columns, and the facet
> columns of the dataset it belongs to, mirrored field for field and held to that by
> `test_catalogue_entry_mirrors_dataset_columns` — **plus** what else is known about
> that version. `find` chooses the version, with the requirement's `VersionFilter`;
> `project_facets`, `other_terms` and `time_range` are all on that second footing.

That keeps the invariant that earns its keep — *"a facet added to `Dataset` and not to
the entry means requirements silently cannot select on it"* — exactly as strong.

### Where `cftime` can live

Settled decision 20 takes `cftime` as an optional extra. It has one architectural
consequence worth stating before anyone starts.

> *Revised 2026-11-24.* This section used to rest on `Covers` doing calendar
> arithmetic **at solve time** for sub-monthly data. It does not: the positions are
> derived at enrich time and stored beside the published values
> ([part three](enrichment-storage.md), decision 31), so `esmporium[calendars]` is
> what *enrichment* needs rather than what a sub-monthly solve needs. The layout
> below is unchanged, because an import rule forced it rather than who calls the
> arithmetic.

`esmporium.datasets` is imported by everything, and `esmporium.requirements` may
import only `esmporium.query`, `esmporium.datasets` and `esmporium.formatting`. The
value types still have to be able to say whether two axes are comparable, and
answering that for a natural axis against a dated one is a conversion (PR R1). So:

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

Practically: a solve works with nothing extra installed, because what it reads was
converted when it was recorded. Enrichment needs `esmporium[calendars]` to place a
published float on a recorded axis, and without it the arithmetic should raise
a plain `ImportError` naming the extra, as `hdf5.py` does. That is an *environment*
problem rather than a data or feature one, so it must not become a solve status
alongside `NEEDS_ENRICHMENT` and `MISSING_FEATURE_PLEASE_RAISE_ISSUE`.

## Where the numbers come from

> *Superseded 2026-11-16* by [part two](enrichment-sources.md), which is the list to
> read: six sources, what each gives, what it costs, and how far each can be believed.
> The table that was here has been cut rather than kept beside it.

Three corrections it makes, because each was nearly got wrong twice:

- **The filename does not give rich values.** It gives natural-unit start and end and
  nothing else — no calendar, no time units.
- **A file header does not give start and end either**, cheaply or at all: no CMIP5 or
  CMIP6 file sampled carries `time_coverage_start` or `time_coverage_end`. The header
  is the source for the branch times; the *time variable's attributes* are the source
  for calendar and units; the *time bounds* are the source for an exact start and end,
  and their first and last values are a few small ranged reads
  ([part two](enrichment-sources.md#variable-values)).
- **Search records give a version's first and last time value for nothing**, since
  `DatasetRawDoc` already holds the raw records. This note missed them entirely, and
  they are the cheapest source there is.

Which reader reads what is settled in
[part two](enrichment-sources.md#what-reads-it): our own classic netCDF parser, and
`h5netcdf` for netCDF-4, both over one byte source that is either HTTP ranges or a
local file. There is no `netCDF4` extra.

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

  `netcdf3.py` grows a variable-list walk rather than being replaced: the list sits
  directly after the global attributes and reuses the same `NC_ATTRIBUTE` structure
  the parser already reads, so it is a short addition. The one trap is `CDF\x02`,
  where `begin` widens to eight bytes. Why a classic file cannot be read by h5py, or
  by `netCDF4`, is measured in
  [part two](enrichment-sources.md#what-was-looked-at).

Filename date parsing was all new code, and landed in PR A. What is project-specific
about it, and where each piece lives, is set out in "What is project-specific, and
where it lives" below.

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
there is never a reason to do that right now, based on the use cases we have. So:

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
                       (here, not in enrich/, because the value types' own
                        comparison rules may need a conversion and
                        `requirements` may not import `enrich`)
    no_time_axis.py    NO_TIME_AXIS_INTERVALS_BY_PROJECT, has_no_time_axis
                       (here for the same reason: `Covers` refuses `fx` data
                        both when a requirement is built and when it is solved)

src/esmporium/enrich/
    protocol.py        FilenameTimeReader, and the raw range it returns
    known_readers.py   FILENAME_TIME_READERS_BY_PROJECT
                       SPAN_BY_REPORTING_INTERVAL
                       reader_for_project(project, readers=...)
    headers.py         read_time_axis(attributes) -> TimeAxis   (project-independent;
                       `attributes` are the time variable's, per decision 21.
                        PR R1: raises rather than defaulting to `standard`
                        when `calendar` is absent)
    combine_files_to_datasets.py
                       per-file ranges -> per-version range + contiguity
                       (built as measure.py; PR R1 renames it, because
                        nothing is measured: information is retrieved and parsed)

src/esmporium/headers/
    netcdf3.py         grows a variable-list walk and record offsets, and
    hdf5.py            reads netCDF-4 through h5netcdf rather than bare h5py,
    fetching.py        over one byte source that is HTTP ranges or a local file,
    parsing.py         so that read_variable_attributes(source, "time", ...) and
                       read_first_and_last_time_bounds(source) exist at all
                       (decision 21 and part two; PR C1, where the reads live)
```

The one `parse_*` function per source, and the tables they are written to, are
[part three](enrichment-storage.md#the-enrich-step-prs-c1-to-c4)'s, and so is the
enrich step which dispatches an artefact to every parser that applies.

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
  decision 8 turns on, so it belongs beside the other values. The function which
  produces it stays in `enrich/` (`combine_file_ranges`, after PR R1): combining a
  version's files is enrichment's, the value is not.
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
| `NOT_LOOKED_FOR` / `UNRESOLVED` | nobody has looked for this dataset's parent yet, so the lineage the check needs is not there | run `esmporium.db.find_parents` ([part three](enrichment-storage.md), decision 34, added 2026-11-24) |

`enrichment_inputs` is the actionable extraction from that bucket: flat, deduplicated,
and shaped to hand straight back to the enricher. It names *which* fact is missing —
range, branch point, or calendar — so the enricher knows whether it needs a header
read or only a filename parse. Both interfaces asked for are served over one body:
PR A's pure functions take catalogue entries and have no ids at all, giving
reproducibility across machines, and PR C4 adds the id-driven entry point for speed
against a database you already hold.

## Plan: the three PRs that landed here

> *Status 2026-11-24.* A, B and B2 are built (#71 to #73) and are kept below as the
> record of what each one was for. Everything after them — the three corrective PRs
> R1 to R3, then readers, parsers, storage, the enrich step, the integration tests,
> parallel enrichment and `download_by_id` — is
> [the plan in part three](enrichment-storage.md#the-plan-from-here), and the order of
> work and where we are up to are in `PLAN.md` step 12c. The PR C, D and E sketches
> that used to sit at the end of this section have been cut: part three replaced all
> three, and two plans in two files is what let them drift.

PRs A and B need no database and no network, which is the order this repo has built
every previous piece of the requirements system: prove it in memory, then wire it up.
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
  thresholds kept so `Degraded` still has a producer, both modes drawn above,
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

Settled decision 22's other half: the rule under "Along a chain" above, and nothing
else. No new value types, no new table, no new column, no new field — so `Covers`'
signature is untouched and no stored requirement's hash changes, which makes this the
one PR here that is not `breaking`.

- `covers.py`: the covering role has to be an *ancestor* of the target rather than its
  direct parent, and the datasets this check reads become the inclusive run from
  `target` up to `role`. The no-time-axis check, the ranges and the contiguity check
  all run over that run rather than over a pair; the mapping composes a branch point
  per hop; and two new checks join it — one per hop that the branch point and the two
  runs it joins are on one axis, and one that each run in between branched where its
  own parent holds data.
- The order the facts are asked for matters, and is: every range, then gaps, then every
  branch point. Ranges and branch points each name *all* the datasets missing them, so
  one run of the enricher clears the lot, and the gap check sits between the two
  because a gap is a definite answer — enriching branch points first would cost a round
  trip and still fail.
- A chain solve can therefore return `NEEDS_ENRICHMENT` naming a dataset which is
  neither the covering nor the covered one, which no single-hop solve ever did.
- The two tests PR B's assertions parked come back —
  `test_branch_chain_back_to_the_control` and
  `test_branch_chain_control_must_cover_child_and_parents` — and the notes on
  `05-zec-flat10` and `11-tas-scenario-anomalies` saying the intermediate run is not
  covered yet come off, because now it is.

## Verification

A, B and B2 need no database and no network, so for them it is:

```
make test                # fast suite, no network
make checks
```

Everything from the migration onwards, and the end-to-end run against a populated
database, is in
[part three's verification](enrichment-storage.md#verification).

## Out of scope

`SameTimeRange` (`PLAN.md` step 14), with a code comment recording why it was left
until then — re-pointed there by PR R2, which takes `align` off `Covers`
([part three](enrichment-storage.md), decision 33).

Each PR carries its own `changelog/` fragment, as every other change here does. B is
`breaking`, and so are R1, R2 and R3, wherever they touch a requirement's hash or a
public signature: the hash covers a requirement's constraints, and `Covers` loses a
field. B2 is the one PR here that is not.
