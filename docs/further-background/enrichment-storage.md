# Enrichment, part three: storing it, the enrich step, and the plan from here

Design note for everything after PRs A, B and B2 of
[the first enrichment note](enrichment.md).
It builds on [part two](enrichment-sources.md),
which says where each piece of time information comes from, what it costs, and what reads it.
Read that first: most of what changed here follows from it.

Like its predecessors it lands ahead of the implementation,
so every later PR can be read against a stated target.

Written: 2026-11-16, and revised the same day after review of
[#70](https://github.com/esmporium/esmporium/pull/70) to
[#74](https://github.com/esmporium/esmporium/pull/74)
(see "What changed in review" at the end).

We are in prototype mode: no users, so interfaces break wherever that gets them right,
including ones PRs A to B2 shipped days ago.

## Context

PRs A, B and B2 ([#71](https://github.com/esmporium/esmporium/pull/71),
[#72](https://github.com/esmporium/esmporium/pull/72),
[#73](https://github.com/esmporium/esmporium/pull/73)) built the pure half:
value types for a time step, units, an axis, a half-open range and a branch point;
calendar arithmetic over `cftime`;
a filename reader per project and a function combining a version's files into one range with a contiguity flag;
and a `Covers` which answers `NeedsEnrichment` or `MissingFeature` into
`SolveResult.couldnt_answer`, handing back `SolveResult.enrichment_inputs`.

Nothing records a time range yet, so `enrich_datasets(...)` does not exist.

```text
   built (#71-#73)          fix what is built        store and enrich         Covers on a real DB
   ──────────────────►  R1, R2, R3  ──────────►  C1, C2, C3, C4  ──────────►  D  ──────────►  P
   values, Covers           units, branch            readers, parsers,         integration     parallel
   in memory                floats, versions         tables, enrich step       tests           enrichment
```

## Settled decisions

Continuing the numbering of [the first note](enrichment.md#settled-decisions).
The numbers 24 to 32 were used by the draft reviewed in #74;
they are reused here for the revised set, and "What changed in review" maps old to new.

| # | Decision |
|---|---|
| 24 | **Six sources, each recorded as such**: `SEARCH_RECORD`, `FILENAME`, `FILE_HEADER`, `VARIABLE_ATTRIBUTES`, `VARIABLE_VALUES`, `FIX`. What each gives, costs and can get wrong is [part two](enrichment-sources.md#the-short-version). |
| 25 | **Readers: our own classic parser and `h5netcdf`, over one byte source** that is either HTTP ranges or a local file. No `netCDF4` extra. See [part two](enrichment-sources.md#what-reads-it). |
| 26 | **Retrieve once, parse everything.** One pure `parse_*` function per source returns everything that source can say; a retrieval is handed to every parser that applies; `enrich_datasets` plans, retrieves and writes, and does not itself decide what is worth recording. Raw attributes are kept, as `DatasetRawDoc` keeps raw search records. |
| 27 | **Trust order, from most to least: `FIX`, `VARIABLE_VALUES`, then the published metadata (`SEARCH_RECORD`, `FILENAME`, `FILE_HEADER`).** `VARIABLE_ATTRIBUTES` are not in that order because nothing else records what they record: they are authoritative unless fixed. |
| 28 | **Which row answers depends on the question, and the source always travels with the answer**, so a message built on filenames can say so and say how to check it. See "Choosing which row answers". |
| 29 | **Disagreements between sources are kept and surfaced, never overwritten.** When a more trusted row is written, it is compared with the less trusted rows for the same version, and a disagreement is recorded. The more trusted row answers. |
| 30 | **Every number travels with its units, and every comparison checks them where it happens.** Written out at the call site rather than buried in a helper or an operator, so a reader can see at each comparison that units were checked. |
| 31 | **Published numbers are stored as published, and conversions are stored beside them.** A branch time published as a float stays a float with its units, untouched. Its position on the parent's recorded axis is computed at enrich time, with `cftime`, and stored as a separate, derived value naming what it was derived from, so the solve never has to convert. The first note's "no fractional years" (decision 4) is about how we *state and compare* extents, not about raw data. |
| 32 | **A catalogue entry is one *version* of a dataset, chosen once, when it is found, and its `id` is that version's id.** `VersionFilter` is used by `find` and `parent_of`, and nowhere after: not by `Covers`, not by the time lookups, not by the enricher, which is handed version ids. Enriching from a *query* goes through `find`, which is where the filter belongs. See "Fixes to what is built", PR R3. |
| 33 | **`Covers` is only ever about branch alignment.** Its `align` argument goes; aligning on the calendar is `SameTimeRange`'s job (PLAN step 14). |
| 34 | **A parent which has not been looked for is "not yet", not "no".** `NOT_LOOKED_FOR` (and `UNRESOLVED`) put a group in `couldnt_answer` pointing at `esmporium.db.find_parents`, as `NEEDS_ENRICHMENT` points at the enricher, rather than in `unsatisfied`. |
| 35 | **`read_variable_attributes` returns the attributes it was asked for, and reports every attribute it found when it cannot find the ones it is looking for.** |
| 36 | **Expensive retrievals are asked for, never defaulted into.** Reading time bounds is opt-in, per call. The default path is search records, filenames, and one 64 KiB prefix per simulation where a header is needed, read from a local copy where one exists (`prefer_local=True`, the default) and from a data node otherwise. |
| 36b | **Enrichment reads only the first and last time bounds of a file, never every value.** Checking a file's contents for gaps, irregular steps or missing values is a different and much more expensive thing, and not enrichment. |
| 36c | **A fast path may guess, if the guess is named and the slow path can check it.** Filename ends, search-record points, the width of a reporting interval and a missing `branch_time_in_child` all rest on an assumption; each stored value names its assumption, and each answer built on one says so. See [part two](enrichment-sources.md#fast-paths-that-guess-and-the-slow-path-that-checks). |
| 37 | **Parallel enrichment is pluggable per kind of retrieval**, with a default for each, because what is slow depends on the system (local reads included). See "Parallel enrichment". |

## Fixes to what is built (PRs R1 to R3)

Three corrective PRs land before any storage,
because the storage is keyed on what they change.
Each is `breaking` where it touches a requirement's hash or a public signature.

### PR R1 — the values carry their units, and only say what was published

`src/esmporium/datasets/`, `src/esmporium/enrich/`.

- **Units on every return.**
  `TimeRange.extent` returns a `Span` rather than a bare `int`.
  `TimeRange.contains`, `TimeRange.is_adjacent_to` and `spanned` check axes inline,
  and the `_check_comparable` helper goes (decision 30).
- **What counts as comparable is stated, not wildcarded.**
  `TimeAxis.is_comparable_with` treats a `None` calendar as matching anything,
  which is a default in disguise. It becomes explicit rules:
  natural against natural needs the same step and no calendar;
  dated against dated needs a known, identical calendar, and **may differ in epoch**
  (a parent on `days since 0100-01-01` and a child on `days since 1850-01-01` are fine
  once converted);
  natural against dated needs the calendar to convert.
  The conversion is a named function in `esmporium.datasets.calendars`, built on `cftime`.
- **`BranchPoint` stops converting, and gets an axis for each end.**
  It no longer floors a published float into an `int`.
  Each end is a position on its own stated axis (`parent_axis`, and a new `child_axis`,
  since `branch_time_in_child` is in the child's time units and calendar, not the parent's):
  a float on a dated axis, a whole number on a natural one.
  Placing a published float onto a natural axis is the enricher's explicit, recorded conversion
  (see `DatasetVersionBranchPoint`), never something the value type does quietly.
- **`PublishedRange` holds only what the filename published**: the two raw stamps and any suffix.
  `precision` and `reports_every` are our inference, not publication,
  so they become explicit inputs to the conversion,
  which records that it assumed the last step to be one reporting interval wide.
- **No guessing in the stamp reader.**
  `_read_stamp` stops filling in unstated parts (day 1, hour 0):
  a filename stamp is placed only on its own natural axis.
  A *dated* range comes from time bounds (`VARIABLE_VALUES`), which say it exactly,
  rather than from a filename padded out to a date.
  `STEP_BY_STAMP_LENGTH` is the CMIP reader's knowledge, so it moves onto that reader.
  `is_climatology`'s docstring stops implying every project marks it with `-clim`.
- **`SPAN_BY_REPORTING_INTERVAL` is documented as an assumption**, the fast path that time bounds check,
  and point intervals (`…Pt`) are flagged: their values are instants,
  so "the last step is one interval wide" is a convention there, not a fact.
- **No default calendar.** `read_time_axis` stops applying CF's `standard` when `calendar` is absent,
  and raises an error listing the attributes it did find.
  `CF_DEFAULT_CALENDAR` goes. A fix can supply a calendar.
- **`TimeUnits.parse` stops re-implementing CF.**
  It splits `<step> since <epoch>`, and interpreting the epoch is `cftime`'s job,
  which implements the CF and udunits rules (CF conventions §4.4) we would otherwise be copying.
- **No more "measure".** `enrich/measure.py` becomes `enrich/combine_files_to_datasets.py`
  and `measure_version` becomes `combine_file_ranges`;
  "measured" leaves messages and docstrings (e.g. `Covers`' "nothing has measured when …"),
  because nothing is measured: information is retrieved and parsed.

### PR R2 — `Covers` checks units where it computes, and maps through `branch_time_in_child`

`src/esmporium/requirements/covers.py`.

- **Inline unit checks at every computation** (decision 30).
  Today `_aligned_through`, `_check_the_joins` and `_covers` subtract and compare bare ints
  and rely on `_check_the_axes` having run first.
  `_Alignment` and `_Window` gain their axis,
  and each computation checks the axes of what it combines immediately before combining them.
- **Map through `branch_time_in_child`, not through the child's own start.**
  This is the "`step_start`" question from review of #70.
  The mapping is the CMIP one:
  a child position `p` is `in_parent + (p - in_child)` on the parent's axis, after converting units.
  Using the child's first data step in place of `in_child` assumes the data starts exactly at the branch,
  and when a leading file is missing it silently shifts every position above it,
  which the first note already flagged as the worst failure of the stitch.
  `in_child` is published by CMIP6 and CMIP7.
  Where it is absent (CMIP5), it is **assumed** to be the child's own first time bound,
  which is the CMIP5 convention, and the assumption is named (decision 36c):
  every answer that relies on it says so, and a fix overrides it.
- **`align` goes** (decision 33): `Covers` is always branch-aligned.
  The developer note above `CatalogueWithTimeInfo` comparing the two is kept,
  re-pointed at `SameTimeRange`, so step 14 still has it.
- **Messages name their sources** once PR D can supply them; until then, the hooks are there.
- `breaking`: `Covers` loses a field, so every stored requirement's hash changes.

### PR R3 — a catalogue entry is a version

`src/esmporium/requirements/`, `src/esmporium/db/database_catalogue.py`.

Today an entry is a `Dataset` row,
and "which version speaks for it" is answered by `describing_version` at **every** lookup:
the newest version (by version string, ties broken by id) which passes the lookup's `VersionFilter`.
Facets are read through it at `find`, parents through it at `parent_of`,
and PR D's time lookups would have had to do the same.
`CatalogueWithTimeInfo.get_time_range(entry)` takes no filter,
so it could only have guessed which filter the solve used,
and answered for a different version than the one whose facets and parent the solve had read.
That latent inconsistency is the argument for the change, not tidiness.

So:

- `find` chooses the version once, with the requirement's `VersionFilter`,
  and **the entry's `id` is that `DatasetVersion`'s id**,
  with `version`, `retracted` and `is_latest` beside it.
  There is no `dataset_id` field, because nothing in a solve needs dataset identity:
  groups form on facet values, `find` returns one version per dataset,
  and the one lookup that needs the dataset (`parent_of`, going from a child version to the
  parent dataset) happens inside the database catalogue, which already joins through versions.
  A human still sees which dataset an entry is from `id_project_specific`.
  `test_catalogue_entry_mirrors_dataset_columns` changes accordingly:
  the facet fields still mirror `Dataset`'s columns, and `id` and the version fields mirror `DatasetVersion`'s.
- `parent_of` keeps its `VersionFilter`: a parent is a dataset, and choosing which of its versions to use
  is exactly the choice a filter makes. `NO_USABLE_VERSION` stays.
- Everything downstream reads `entry.id` as a version: `Covers`, `CatalogueWithTimeInfo`, `EnrichmentInput`
  (whose `dataset_id` becomes `dataset_version_id`, so the enricher has no version to choose),
  and `download_by_id` (PR E), since a version is what gets downloaded.
- `breaking`: every id a solve reports changes meaning, from dataset to version.
- Enriching from a query is `find` then enrich, so the filter appears there and only there.
- Decision 34 lands here: an unresolved parent puts the group in `couldnt_answer`
  with a status pointing at `find_parents`, so coverage never has to break a deadlock with a filter.
- The developer note above `Catalogue` ("one entry describes one `Dataset` row, not one `DatasetVersion`")
  and the first note's "What does a catalogue entry carry, and for which version?"
  are updated to say this.

Not done, and not needed: the commented-out `Relationship()` in `schema.py`.
Explicit `select`s are the state of the code rather than a recorded decision,
and nothing here is easier with relationships.

## Storage (PR C3)

All in `src/esmporium/db/schema.py`, which deliberately does not use
`from __future__ import annotations` (it breaks SQLModel), gives every table
`model_config = {"validate_assignment": True}`, and stores every enum as
`Field(sa_type=AutoString)`, never a SQLAlchemy `Enum`, so adding a member needs no migration.

Also in this migration: **`DatasetVersionParent` becomes `DatasetVersionParentInfo`**,
as asked in an earlier review, so the new tables can be named in the same pattern.

### `FileMetadataDoc`

One row per file whose bytes we have read: everything the read found (decision 26).

| Column | Why |
|---|---|
| `file_id` | unique; FK `file.id`. The read is of a file |
| `global_attributes` | JSON, as returned, nothing renamed. `DatasetRawDoc.raw_json`'s rationale applies verbatim |
| `variable_attributes` | JSON, keyed by variable name. Every variable the walk reached, not only `time` |
| `layout` | which `HeaderLayout` the bytes announced, so a per-format bug is findable |
| `bytes_read`, `n_requests` | what it cost |
| `read_from` | the route URL and data node, or the local path |
| `retrieved_at` | as `DatasetRawDoc` has it |

*Measured* sizes: CMIP5 globals 1,555 B as JSON, CMIP6 globals 3,161 B, CMIP6 `time` attributes 318 B.
About 3.5 KB kept per 64 KiB fetched, and kept bytes do not need a data node to be alive.

### `FileTimeRange`

One row per file per source that says something about the file's own range:
`FILENAME` (always, for free) and `VARIABLE_VALUES` (when its first and last bounds were read).
This is where the raw text lives, which answers "how can a version's range be raw
when it comes from many files?": it cannot, so raw stays per file.

| Column | Why |
|---|---|
| `file_id`, `source` | unique together |
| `start_raw`, `end_raw` | the observation, as published: stamps for a filename, the first lower and last upper bound for values |
| `start`, `end_exclusive`, `units`, `calendar` | the parse, half-open. Nullable: a daily filename has no calendar-free ordinal |
| `assumption` | for `FILENAME`, the reporting step assumed to close the range; `NULL` for bounds, which need no assumption |
| `recorded_at` | provenance |

### `DatasetVersionTimeAxis`

The time units and calendar of a version, from `VARIABLE_ATTRIBUTES` or a `FIX`.
A table of its own because an axis is known without a range
(a prefix read for parent information gives it for free),
and a branch point needs the axes of both ends.
Unique on `(dataset_version_id, source)`. Columns: `units`, `calendar`, `file_id` (which file said so), `recorded_at`.

### `DatasetVersionTimeRange`

What a version's data spans, per source. Combined from `FileTimeRange` rows, or read off a search record.

| Column | Why |
|---|---|
| `dataset_version_id`, `source`, `units` | unique together, so one source can give both a natural row and a dated one (values give a dated range exactly, and a natural one derived from it, which outranks the filename's) |
| `status` | `RECORDED`, `NO_TIME_AXIS`, `NEEDS_A_CALENDAR`, `UNREADABLE`. A row is written for failures too, so nothing is retried for ever and an `fx` version is never fetched twice. `RECORDED` rather than `MEASURED` (nothing is measured) or `CONFIRMED` (a filename row is recorded, not confirmed: trust is the `source` column's job) |
| `start`, `end_exclusive`, `units`, `calendar` | half-open, no extent column. Nullable as above. Numeric, read back into the value type for `units`, which checks that natural values are whole |
| `is_contiguous`, `n_files` | from the per-file rows; `NULL` for `SEARCH_RECORD`, which cannot see files |
| `assumption` | what the value rests on, if anything: the filename's closing step, or for `SEARCH_RECORD` that each published point lies inside a step as wide as the reporting interval. `NULL` for bounds and fixes |
| `derived_from` | which `FileTimeRange` rows, or which `DatasetRawDoc`, this was computed from |
| `recorded_at` | provenance |

### `DatasetVersionBranchPoint`

The raw branch point is already stored, as published:
`DatasetVersionParentInfo.branch_time_in_parent`, `branch_time_in_child` (floats) and `parent_time_units`.
That row is never rewritten.
This table holds what we derive from it at enrich time (decision 31):
both ends placed on the axes the two versions' ranges are recorded on,
so that `Covers` compares positions without converting anything, and without `cftime`, at solve time.
That keeps decision 20's promise that monthly and yearly solves need nothing installed.

Keyed on `parent_info_id`, as `DatasetParentLink` is, so it inherits source precedence from the raw row:
a `FIX`-sourced parent-info row gets a branch point derived from the fix.

| Column | Why |
|---|---|
| `parent_info_id` | FK, the raw row this was derived from |
| `child_dataset_version_id` | derivable, kept for the same one-indexed-lookup reason `DatasetParentLink` keeps it |
| `in_parent`, `in_parent_units`, `in_parent_calendar` | the published `branch_time_in_parent`, placed on the parent's recorded range axis |
| `in_child`, `in_child_units`, `in_child_calendar` | the same for `branch_time_in_child`, on the child's recorded range axis. Each end has its own units and calendar, because each is on its own run's axis |
| `assumption` | e.g. "`branch_time_in_child` not published; assumed to be the child's first time bound" (CMIP5). `NULL` when both ends were published |
| `derived_from` | the `DatasetVersionTimeAxis` and `DatasetVersionTimeRange` rows used for each end |
| `recorded_at` | provenance |

On a natural axis the derived value is the ordinal of the step the branch instant falls in,
so it is a whole number by construction.
That is a statement about which month the run branched in, not a rounding of the published float,
which is still there, unrounded, on the raw row.

**Staleness is handled by `derived_from`.**
Whenever a more trusted axis or range row is written for either version,
the branch points derived from the older rows are re-derived in the same transaction,
and if the new value differs, that is recorded like any other disagreement (decision 29).

A CMIP5 `branch_time` has no units, so no branch point can be derived until a `FIX`-sourced
`DatasetVersionParentInfo` row supplies them; until then `Covers` reports `NEEDS_ENRICHMENT`
for the branch point, pointing at a fix. `preferred_parent_info` already decides which raw row speaks.

Deriving needs `esmporium[calendars]` at enrich time, because placing a day count on a month axis needs leap rules.
That sits beside the extras enrichment needs anyway.

### Choosing which row answers

```text
preferred_time_range(rows, *, wanted: TimeStep) -> (DatasetVersionTimeRange, source) | None

  1. a FIX row on an axis that can answer `wanted`         -> it, always
  2. otherwise the most trusted source (decision 27)
     with a row that can answer `wanted`                   -> it
       (a natural row derived from VARIABLE_VALUES beats a FILENAME row)
  3. wanted is calendar-free and only SEARCH_RECORD /
     FILENAME rows exist                                    -> the FILENAME row (it knows about gaps),
                                                               else the SEARCH_RECORD row;
                                                               if both exist and disagree, the FILENAME
                                                               row still answers, the disagreement is
                                                               recorded, and the answer says to check
                                                               with the time bounds
  4. wanted is finer than a month and no dated row exists   -> None: NEEDS_ENRICHMENT
```

The source is returned with the row and carried into every `Covers` message,
which is what lets a `Fail` resting on filenames say
"these ranges come from filenames only; enrich #3 and #7 from variable values to check them" (decision 28).

`CatalogueWithTimeInfo.get_time_range` gains the `wanted` step in PR D:
`Covers` knows it, because `pad` and `min_overlap` are `Span`s.

### Consistency between sources

Written as a pure function in PR C2 and called by the writer in C4 (decision 29):
each new row is compared with every less trusted row for the same version,
on a common axis where one exists.
A disagreement is recorded (which rows, which way, by how much) and logged,
and does not stop the write.
The cases worth testing first are the ones that happen:
a filename whose stamps do not match its bounds,
a search record that disagrees with the filenames,
and `branch_time_in_child` that does not match the child's first bound.

## The enrich step (PRs C1 to C4)

```text
enrich_datasets(session, inputs, *, sources=…, prefer_local=True, executors=…)
     │
     ├─ plan_enrichment            which versions, which files, which retrievals
     │      ├─ from_database          search records and filenames: no I/O
     │      ├─ prefixes_to_read       one 64 KiB prefix per simulation, or per file when bounds are read
     │      ├─ bounds_to_read         first and last time bounds, per file, when asked for (36, 36b)
     │      └─ cannot_retrieve        nothing fetchable, locally or from a data node
     │
     ├─ retrieve                   bytes, from data nodes or local files (parallel: see below)
     │
     ├─ parse_information_source   every artefact to every parser that applies
     │      ├─ parse_search_record
     │      ├─ parse_filename
     │      ├─ parse_file_header          (parent facts too: this absorbs db/parent_headers.py's parsing)
     │      ├─ parse_variable_attributes
     │      └─ parse_variable_values
     │
     └─ record                     FileMetadataDoc, FileTimeRange, DatasetVersionTimeAxis,
                                   DatasetVersionTimeRange, DatasetVersionParentInfo,
                                   DatasetVersionBranchPoint (re-derived when inputs improve);
                                   consistency checks; batched commits
```

- **`inputs` are version ids** (PR R3).
  An `enrich_query(session, query, versions=…)` convenience calls `find` and passes the versions on;
  that is the one place a `VersionFilter` meets enrichment.
- **`sources`** says which sources may be used, defaulting to the cheap ones.
  This is also how a user double-checks: `sources={VARIABLE_VALUES}` for datasets whose answer looked wrong.
- **`prefer_local`**, on by default, reads a local copy (from the downloads step) where one exists,
  and goes to a data node only for files with no local copy.
  A local copy is the file we would otherwise fetch from, it needs no data node to be up,
  and it puts no load on anyone else's server.
  Pass `prefer_local=False` on a system where local reads are slower than the network,
  which on some shared filesystems they are (see "Parallel enrichment").
- **Prefixes per simulation by default.** A calendar and a parent belong to the run,
  the argument `HeaderReadPlanItem.dataset_version_ids` already makes for parents.
  When values are being read, every file's prefix is read anyway, and all of it is parsed.
- **Writers take a session and do not commit**; the entry point owns the boundary,
  committing per batch so a long run that fails keeps what it learnt,
  and `plan_enrichment` is idempotent, so running it again picks up where it stopped.
- **Search records are back-filled from `DatasetRawDoc`.**
  New ones are parsed when results are saved (part two, "Retrieve once, parse everything").
- Reused, not restated: `get_or_create`, `flushed_id`, the smallest-first file candidates,
  the buffered `HeaderReadCall` observer written in a `finally`,
  and the "asserted wins, silence changes nothing" loop from `save_parent_info`.

## Fixes, and the check that makes them safe

`src/esmporium/fixes/time.py`, reusing `ParentFix`'s shape: a whole source payload,
plus a **mandatory** `reason` and `evidence`, with that file's own warning kept:

> **Required, and required to be honest.** The one thing that must not happen is a guess
> recorded as a certainty: a fix outranks the file itself, so an unfounded one is worse
> than no fix at all.

Fixes can set a version's time axis, its range, or a parent-info row (for branch times).

`schema.py` records a sampling across 47 CMIP5 models on 2026-11-04,
in which one `branch_time` attribute carried at least five incompatible encodings:

| Model / case | Raw | Reading | What a fix needs |
|---|---|---|---|
| IPSL-CM5A-LR | `185001.0` | `YYYYMM` | nothing outside the file: six digits, plausible year, month 01 |
| FIO-ESM | `20060101.0` | `YYYYMMDD` | nothing outside the file: eight digits, an rcp start date |
| bcc-csm1-1-m `abrupt4xCO2` | `240.0` | a year: the child's own filename starts `024001` | nothing outside the file, and it is checkable |
| IPSL-CM5A-MR | `1850.0` | year 1850, or 1850 days into a control | evidence from outside the file |
| CCSM4, bcc-csm1-1 | `2.0`, `160.0` | years, or days | evidence from outside the file |
| BNU-ESM, GFDL-CM3, HadGEM2-ES, MPI-ESM-MR | `0.0` | nothing was recorded | evidence from outside the file |

None of these is unfixable.
The last rows simply cannot be fixed *from the file*:
the answer has to come from somewhere else, such as the modelling centre or the model's documentation,
and that is what `evidence` records.
What the status enum must not do is pretend the file determines them.

**A fix is checked against the data where data exists.**
After enrichment we hold the parent's recorded range,
so the writer refuses a branch-time fix which places the branch outside it.
The bcc-csm1-1-m row above is the worked example: read as a year on the parent's axis,
`240.0` is coherent with a child starting at `024001`, and checkable against `piControl`'s range.

## Parallel enrichment

PR P. What we know:
retrieving is where the time goes, and **we cannot know in advance which retrievals are slow on a given system**.
Remote reads are slow for the obvious reasons;
local reads, which look free, can be slower still on some shared filesystems.
So there is a default, and it is replaceable per kind of retrieval.

The work splits into four stages, and only one of them is worth spreading out:

| Stage | Where it runs | Why |
|---|---|---|
| plan | the calling thread | reads the database; a `Session` is not thread-safe |
| **retrieve** | **an executor per kind of retrieval** | this is the slow, system-dependent part |
| parse | beside the retrieve, in the same worker | cheap, and parsing in the worker means parsed results (small) rather than bytes cross back |
| record | the calling thread, in batches | writes to the database |

Kinds of retrieval, and their defaults:

| Kind | Default | Notes |
|---|---|---|
| from the database (search records, filenames) | inline | no I/O beyond the session |
| remote prefix, remote values | the existing threaded fan-out in `esmporium.headers.run`, with its per-data-node cap | threads are right for waiting on the network |
| local prefix, local values | a small thread pool | a guess, and the knob most likely to need turning |

The interface is `executors: Mapping[RetrievalKind, concurrent.futures.Executor]`.
Anything implementing the standard `Executor` interface fits:
a `ThreadPoolExecutor` of a different size, a `ProcessPoolExecutor`,
or a dask cluster's `Client.get_executor()`.
That is the flexibility we owe users on systems we do not know,
without us writing a scheduler.
The per-data-node cap stays ours, because it is a courtesy to the data nodes rather than a tuning choice.

For processes to work, retrieve-and-parse is a top-level function over frozen dataclasses of plain values,
with no `Session` crossing the boundary: the seam PR A's value types already provide.
Threads parallelise waiting but not `h5py` parsing,
which runs behind a global lock (see [part two](enrichment-sources.md#the-decision)).

Every retrieval, local ones included, records how long it took and how many bytes it read,
as `HeaderReadCall` already does for remote reads,
so a user tuning `executors` is looking at numbers rather than guessing.

And one lesson carried over from the download work (PLAN step 8):
the download prototype grew an adaptive scheduler (AIMD congestion control, node eviction, affinity clustering,
about 330 lines) **none of which ever fired against the real network**.
So: fixed pools, configurable; adaptation only if a measurement asks for it.

Parallel enrichment lands after the serial path (C4) and the integration tests (D),
so it is a change to how retrievals are run and not to what they return.

## The plan from here

In order. R1 to R3 correct what is built; C1 to C4 and D are the storage and wiring;
P and E can follow in either order.

### PR R1, R2, R3

See "Fixes to what is built" above.
R1 before R2 (Covers uses R1's types); R3 is independent of both and must land before C3.

### PR C1 — readers (`esmporium.headers`)

- A range-backed byte source replacing `_PrefixFile`'s fixed prefix:
  fetches what is asked for, on demand, up to a byte and request budget, raising past it;
  and the same interface over a local file.
- `netcdf3.py` grows a variable-list walk and record offsets
  (`numrecs`, `recsize`, each variable's `begin`; eight-byte `begin` for `CDF\x02`),
  written from the format specification alone, citing the grammar rule each piece implements,
  and without reading other parsers (VirtualiZarr's included) along the way.
  See [part two](enrichment-sources.md#the-decision).
- `hdf5.py` reads through `h5netcdf` rather than bare `h5py`; `h5netcdf` joins the `headers` extra.
- `read_variable_attributes(source, name, *, wanted=…)` (decision 35) and
  `read_first_and_last_time_bounds(source)` join `read_global_attributes`.
  There is deliberately no "read every value" (decision 36b).
- A longer re-fetch of the CMIP5 fixture: today's `cmip5-netcdf3-prefix.bin` (6,364 B) stops after the
  first `time` attribute. The short one is kept, because it pins "the globals end at 6300".
  A small synthetic classic file with a record dimension pins the offsets.
- A CMIP7 (repacked) fixture, so that the single-chunk time coordinate is measured
  rather than assumed ([part two](enrichment-sources.md#variable-values)),
  and a test that reading its first and last time bounds stays within one prefix-sized budget.

### PR C2 — the `parse_*` functions, pure

No database and no network, as PRs A and B were.
`parse_search_record` in the search result parsers (field names are per API);
`parse_filename`, `parse_file_header`, `parse_variable_attributes`, `parse_variable_values`
in `esmporium.enrich`; and the consistency comparison.
Unit tests on every recorded search response and header fixture we hold,
and the cases from "Consistency between sources".

### PR C3 — storage

The tables under "Storage", the `DatasetVersionParentInfo` rename,
one alembic revision via `make migration MESSAGE="…"`, read by hand
(autogenerate is a first draft and cannot see data migrations),
and round-trip tests beside `tests/integration/db/test_headers_against_migrations.py`.

### PR C4 — `enrich_datasets`, serial

Planning, retrieving (serially), parsing, recording, back-filling search records,
`sources`, `prefer_local`, fixes and the fix check.
`db/parent_headers.py`'s parsing moves into `parse_file_header`,
so a prefix read for parents and one read for time are the same read.

### PR D — the catalogue serves it, and the integration tests that were the point

- `CatalogueEntry` gains the time fields, loaded on a `needs_time_info` flag mirroring `needs_facets`;
  `DatabaseCatalogue` and `InMemoryCatalogue` both fill them, from the entry's own version (R3);
  `CatalogueWithTimeInfo` is vacated, not deleted (first note, "Costs").
- `get_time_range` takes `wanted`; answers carry their source; `Covers` messages name it.
- `tests/integration/requirements/`: a migrated SQLite database, enriched, solved,
  agreeing with `InMemoryCatalogueWithTimeInfo` on the same data;
  before enrichment the group is `couldnt_answer` / `NEEDS_ENRICHMENT`, not `unsatisfied`.
- One opt-in live test behind a new marker beside `reads_file_headers`, with a `make` target.

### PR P — parallel enrichment

As above.

### PR E — `download_by_id`

`src/esmporium/db/downloads_to_database.py`. `plan_downloads` already calls
`find_datasets(...)` and immediately reduces the result to ids before planning,
so the split is at that line: the existing query entry point keeps working,
and an id-based one wraps the same body.
After R3 the ids it takes are version ids, which is what a download is of.
Land any time after R3.

Worth stating in the PR description, because it answers a question asked explicitly:
**ESGF ids cannot be routed through the existing query interface.**
`facet_filters` *raises* on any name outside `DATASET_FACET_COLUMNS`,
and `id_project_specific` is deliberately excluded from it
("identify a row rather than describe it").
A facet query is also a cross-product rather than a list,
so asking for two specific datasets by facet returns four.

## Verification

```text
make migration MESSAGE="add file metadata, time axis, time range and branch point tables"
make migration-sql       # the schema applying every migration from scratch gives
make test                # fast suite, no network
make test-headers        # opt-in, live data nodes
make licence-check       # h5netcdf joins the headers extra
make checks
```

Then end to end against a populated database:
search a small CMIP6 requirement, solve a `Covers` requirement against it,
hand `result.enrichment_inputs` to `enrich_datasets(session, …)` unmodified,
and confirm that it writes a row for every version named,
that a second `plan_enrichment` has nothing left to do,
and that asking again with `sources={VARIABLE_VALUES}` adds rows without removing any.

## Questions settled in review

Asked and answered on 2026-11-16, recorded so they are not asked again:

- **Every time value, or first and last?** First and last only (decision 36b).
  A thorough check of a file's contents is a separate and much more expensive thing.
  This also removed the question of whether to store whole time arrays.
- **Search record and filename disagree.** The filename answers, because it can see gaps;
  the disagreement is recorded, and the answer says to check with the time bounds.
- **What is an entry's `id`?** The `DatasetVersion` id, with no `dataset_id` field (PR R3).
- **CMIP5 has no `branch_time_in_child`.** Assume the child's first time bound, name the assumption,
  let a fix override it (PR R2, decision 36c).
- **Status name.** `RECORDED`.
- **Local copies.** Preferred by default (`prefer_local=True`), falling back to data nodes.
- **Convert at enrich time, or at solve time?** Both the raw value and the converted one are stored,
  the conversion at enrich time (decision 31, `DatasetVersionBranchPoint`),
  so monthly solves stay free of `cftime`.

## What changed in review

Against the draft reviewed in #74:

| Was | Now | Why |
|---|---|---|
| 24, 25: three readers, `netCDF4` as a new extra for whole files | 25: own classic parser and `h5netcdf`, one byte source, no `netCDF4` | `h5netcdf` reads lazily and cleanly through our file object, and local files go through the same readers ([part two](enrichment-sources.md#what-reads-it)) |
| 26: four tiers, filename beats header, `time_coverage_*` as tier 2 | 24, 27: six named sources and a trust order | headers carry no start or end; search records were missing entirely |
| 27: parse all of a header | 26: parse all of *every* source, via `parse_*` | the same principle, applied everywhere |
| 28: answer depends on the units wanted | 28: and on trust, with the source carried into messages | so filename-based answers can say how to check them |
| — | 29: disagreements kept and surfaced | filenames can contradict the data, and the data should win |
| 29: fixes checked against the data | kept, under "Fixes" | |
| 30: parallelism targets measuring | 37: pluggable executors per kind of retrieval | the slow part is retrieving, and which retrieval is slow depends on the system |
| 31 | 35, reworded | |
| `DatasetTimeRange` with raw columns and one `file_id` | `FileTimeRange` (raw, per file) and `DatasetVersionTimeRange` | a version's range comes from many files |
| `DatasetBranchPoint`, interpreted, `int`, one set of units | `DatasetVersionBranchPoint`: derived positions for *both* ends, each with its own units and calendar, the raw floats untouched on `DatasetVersionParentInfo`, and re-derived when an input improves | 31 |
| 32: whole files per question | 36, 36b: expensive retrievals opt-in, and only first and last bounds ever read; local copies preferred by default | |
| status `MEASURED` | `RECORDED` | trust is the source's job, not the status's |
| a fix-less `0.0` branch time "unfixable" | fixable with evidence from outside the file | |
| `describing_version` resolved in each lookup | 32: entries are versions | the time lookups could not have known which version the solve meant |

Plus, from review of #70 to #73 and carried in R1 to R3:
units carried and checked inline, `extent` returning a `Span`, `BranchPoint` floats with a child axis,
mapping through `branch_time_in_child`, no default calendar, no guessing in the stamp reader,
`PublishedRange` holding only what was published, `measure` renamed, `align` removed,
and unresolved parents reported as "not yet".
