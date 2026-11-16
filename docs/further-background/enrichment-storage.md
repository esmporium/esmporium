# Enrichment, part two: storing it, and where the numbers really come from

Design note for **PR C** of [the enrichment note](enrichment.md) — the step which
records the time information ESGF does not publish, so that `Covers` can one day be
asked a question about a real database rather than a hand-written dictionary.

Like its predecessor it lands ahead of the implementation, so every later PR can be
read against a stated target. Unlike its predecessor it is written **after** a round of
measurement, and three of its conclusions contradict what the first note assumed. Those
are called out where they occur rather than quietly corrected.

Written: 2026-11-16.

Cross-references are plain code spans rather than `mkdocstrings` links where the thing
named does not exist yet; they become links as each piece lands.

## Context

PRs A, B and B2 ([#71](https://github.com/esmporium/esmporium/pull/71),
[#72](https://github.com/esmporium/esmporium/pull/72),
[#73](https://github.com/esmporium/esmporium/pull/73)) built the pure half. There are
value types for a time step, units, an axis, a half-open range and a branch point; there
is calendar arithmetic over `cftime`; there is a filename reader per project and a
measurement that combines a version's files into one range with a contiguity flag. And
`Covers` now answers `NeedsEnrichment` or `MissingFeature` into
`SolveResult.couldnt_answer`, handing back `SolveResult.enrichment_inputs`.

`changelog/71.feature.md` states the gap outright:

> None of it is wired to a search, a database or a solve yet.

Nothing records a time range, so `enrich_datasets(...)` — named in
`SolveResult.enrichment_inputs`' own docstring as the round trip that type exists for —
does not exist, not even as a stub.

```text
   PR A  ─────────►  PR B / B2  ─────────►   PR C   ─────────►  PR D
   values            Covers asks and         THIS NOTE          Covers runs
   + measuring       answers "needs                             against the
                     enrichment"                                database
                          │                      │
                          │ enrichment_inputs    │ enrich_datasets(...)
                          ▼                      ▼
                   [dataset_id,            DatasetTimeRange
                    missing fact]          DatasetBranchPoint
                                           FileHeaderDoc
```

This note covers PR C only. Making `Covers` run against `DatabaseCatalogue` is PR D.

## Where a dataset's start and end can come from

Four places, and they sit in physically different parts of the world. Knowing *which
part* is what decides what each one costs and how much it should be believed.

```text
tas_Amon_bcc-csm1-1-m_abrupt4xCO2_r1i1p1_024001-038912.nc
                                         └──────┬──────┘
                                     TIER 1 — the filename
                              "year 240 month 01 → year 389 month 12"
                              FREE. Every filename is already stored.
                              Resolution matches the data's own frequency.

┌──────────────── the file itself, 368,702,896 bytes ─────────────────┐
│                                                                     │
│  ┌─ header, first ~6 KB ─────────────────────┐                      │
│  │ GLOBAL attributes:                        │  TIER 2 — dates      │
│  │    experiment_id  = "abrupt4xCO2"         │  written into the    │
│  │    branch_time    = 240.0                 │  header              │
│  │    creation_date  = "2012-04-11"          │                      │
│  │    ⚠ no start date, no end date anywhere  │  One 64 KiB ranged   │
│  ├─ VARIABLE attributes ─────────────────────┤  GET. But no CMIP5   │
│  │    time: units    = "days since …"        │  or CMIP6 file       │
│  │    time: calendar = "noleap"              │  sampled carries a   │
│  │    time: bounds   = "time_bnds"           │  start or an end.    │
│  └───────────────────────────────────────────┘                      │
│                                                                     │
│  data section — interleaved, one record at a time:                  │
│   [time₁][bnds₁][ tas₁  204,800 B ][time₂][bnds₂][ tas₂ ] …         │
│     ▲                                 ▲                             │
│     └─────────── 204,835 bytes ───────┘                             │
│                                                                     │
│  TIER 3 — the time variable's own values. 1800 of them in this      │
│  file, each one a stride apart. This is the truth: exact, and via   │
│  `time_bnds` *inclusive* (1850-01-01 to 1850-01-31, not merely      │
│  "1850-01"). For a classic netCDF file: unreachable without all     │
│  368 MB — see "A CMIP5 time variable" below.                        │
└─────────────────────────────────────────────────────────────────────┘

                        TIER 4 — a fix
            A row we write ourselves, because what was
            published is wrong. Beats all three above.
```

Going down the list, **trust rises** — each tier is closer to what the model actually
computed — and **cost rises** with it, from free, to one small request, to the whole file.

The rule when two disagree:

- **tier 1 beats tier 2.** There is no validation of a published CMIP5 or CMIP6 file, so
  a file written incorrectly can carry a header that contradicts its own name. The
  filename is what the archive indexes, searches and serves by, so it wins.
- **tier 3 beats both.** It is the data rather than a description of the data. From rich
  values the natural ones can always be derived; the reverse needs a calendar.
- **tier 4 beats everything**, which is the whole reason the source column exists.

## What was measured before planning

The first note's section "Where the numbers come from" asserted a cost model. Most of it
held; three parts did not. Everything below was run against this repository's own
captured header prefixes in `tests/test-data/headers/`, and is reproducible from them.

### Which reader can read what

| Reader | classic netCDF (`CDF\x01`) | netCDF-4 (HDF5) | needs | gives tier 3 |
|---|---|---|---|---|
| `headers/netcdf3.py`, ours | **yes**, from a 64 KiB prefix | no | nothing installed | no |
| `h5py` over `_PrefixFile` | **no** — `OSError: file signature not found` | yes, from a 64 KiB prefix | `headers` extra | not as written |
| `netCDF4` with `memory=` and the **whole file** | **yes** | **yes** | `netcdf4` extra, and the whole file | **yes** |
| `netCDF4` with `memory=` and a prefix | no — `NetCDF: Invalid argument` | no — `NetCDF: HDF error` | | no |
| `netCDF4` with a prefix **zero-padded to the real file size** | no — `NetCDF: Invalid argument` | yes, attributes only, at **875 MB** peak RSS | | no |

Measured with netCDF4 1.7.5.1 against libnetcdf 4.10.1, and h5py 3.14.0.

Two things follow, and the first is a departure from the plan of record:

- **`netCDF4` cannot read a prefix.** Its in-memory open wants a complete file image.
  That is a property of the C library, not a preference of ours, and no amount of
  zero-padding rescues the classic case.
- **`netCDF4` earns its place anyway, for a file we hold in full.** On a complete file it
  reads both formats, reads tier 3, and presents the netCDF data model rather than
  HDF5's — which solves a real problem, below.

The last row is recorded only so nobody tries it: it works for one format, costs 875 MB
of resident memory per file, and still cannot reach tier 3.

### netCDF4 hides HDF5's bookkeeping; h5py does not

Read through `h5py`, a netCDF-4 file's `time` variable carries four attributes that are
not in the file in any meaningful sense:

```text
h5py     ['CLASS', 'NAME', '_Netcdf4Dimid', 'bounds', 'units',
          'calendar', 'axis', 'long_name', 'standard_name', 'REFERENCE_LIST']
netCDF4  ['bounds', 'units', 'calendar', 'axis', 'long_name', 'standard_name']
```

`CLASS`, `NAME`, `_Netcdf4Dimid` and `REFERENCE_LIST` are how netCDF-4 encodes "this
variable is a dimension" *inside* HDF5. The classic version of the same dataset has none
of them. `h5py` reads the container, so it sees them; `netCDF4` reads the data model, so
it does not. One of them also reduces badly: `REFERENCE_LIST` comes back as h5py object
references, which breaks the invariant `tests/unit/headers/test_hdf5.py` holds for global
attributes — that every value's type is a builtin.

### CMIP5 and CMIP6 global attributes carry no start or end date

The first note said the header is the source for *calendar and units* and the filename is
the source for *start and end*. The sampled files agree, and more strongly than expected
— there is no `time_coverage_start` or `time_coverage_end` anywhere:

| File | globals | anything date-like |
|---|---|---|
| CMIP5 `tas_Amon_bcc-csm1-1-m_abrupt4xCO2_r1i1p1` | 27 | `creation_date`, `branch_time = 240.0` |
| CMIP6 `tas_Amon_TaiESM1_abrupt-4xCO2_r1i1p1f1` | 52 | `creation_date`, `branch_time_in_parent = 438000.0`, `parent_time_units = 'days since 0701-01-01'` |

`time_coverage_*` are CF/ACDD *recommendations*, and other archives do publish them, so
tier 2 is worth modelling. It simply has no CMIP5 or CMIP6 customer in the files sampled,
and this note should not imply one.

### A CMIP5 time variable's values are not cheaply reachable

Walking the CMIP5 prefix's variable list — which also prototyped the walk this PR adds:

```text
numrecs = 1800
dims    = [('time', 0), ('lat', 160), ('lon', 320), ('bnds', 2)]
                     ^ length 0 is the UNLIMITED (record) dimension
globals end at byte 6300;  var_list tag 0xb, 8 variables;  `time` is the first
```

`time` is the record dimension, so `time` is a **record variable** and its 1800 values are
interleaved one per record across the file: 368,702,896 bytes over 1800 records is
≈ 204,835 bytes of stride between consecutive time values. Reading them all is 1800
range requests or one 368 MB download. Neither is a header read.

A netCDF-4 file is the opposite case: every variable is its own HDF5 dataset, so `time`
and `time_bnds` together are tens of kilobytes sitting in one place — reachable, but only
with a file object that can fetch arbitrary ranges on demand, which `_PrefixFile`
deliberately is not (it raises rather than zero-filling, so that HDF5 can never decide
anything from bytes we did not fetch).

### The fixture is too short to test against

`tests/test-data/headers/cmip5-netcdf3-prefix.bin` is 6,364 bytes. The globals end at
6300, `time` is the first variable and declares six attributes starting at byte 6332, and
the prefix dies after the first of them (`bounds = "time_bnds"`). `units` and `calendar`
are beyond it.

So a longer re-fetch of the same file is needed before any real classic-netCDF calendar
test can pass. The short file is **kept** as well: it pins "the globals end at 6300",
which is a fact worth a test.

## The reader question, settled

The decision is not which reader is best, because none of them is. It is **which reader
for which situation**, and the constraint that forces the answer is this: **netCDF4's
Python API takes a filename or a bytes buffer — it cannot take a Python file-like
object.** `h5py` can. So lazy, range-backed reads are h5py-only, and classic-from-memory
with clean attributes is netCDF4-only. No single tool does both.

| What is wanted | classic netCDF | netCDF-4 / HDF5 |
|---|---|---|
| attributes from a **64 KiB prefix** | `netcdf3.py` only | `h5py` only |
| attributes from a **whole file** | `netCDF4` | `netCDF4` |
| tier 3, **lazily** | impossible — interleaved | `h5py` + a range-backed file object |
| tier 3, from a **whole file** | `netCDF4` | `netCDF4` |
| HDF5 bookkeeping hidden | n/a | `netCDF4` only |

Three architectures were considered. Recording all three, because the one chosen is a
cost judgement and the others stay available if the cost turns out differently:

- **A — enrichment works on whole files only.** One reader; `netcdf3.py` and `hdf5.py`
  both deleted; every tier available for both formats. Costs roughly 250–370 MB per
  simulation, so a 47-model survey is tens of gigabytes.
- **B — enrichment stays on prefixes only.** Keeps both prefix readers; ~64 KiB per
  simulation; tier 3 never available for classic netCDF and needing new machinery for
  netCDF-4.
- **C — prefixes where they suffice, a whole file where they do not.** *Chosen.*

**Option C, stated plainly: use a 64 KiB prefix for everything it can answer, and fetch
one whole file only when a question genuinely needs tier 3.** The case where tier 3
matters — sub-daily data, which needs a real calendar, or a file suspected of
contradicting its own name — is also the case where the file is likely wanted anyway. The
cheap path stays cheap and the expensive path is entered deliberately, per question,
never in bulk.

## Settled decisions

Continuing the numbering of [the first note](enrichment.md#settled-decisions).

| # | Decision |
|---|---|
| 24 | **Three readers, chosen by what we hold, and `netcdf3.py` stays.** `netcdf3.py` for classic from a prefix, `h5py` for netCDF-4 from a prefix, `netCDF4` for either from a whole file. Measured, not preferred — see the matrix above. |
| 25 | **`netCDF4` is a new optional extra**, used only where the whole file is in hand. The objection recorded against it in `fetching.py` — that it does its own HTTP inside the C library — does not apply when it is handed a buffer, and it is the only reader that reaches tier 3 and hides HDF5's bookkeeping. |
| 26 | **Four tiers, trusted in the order filename, header dates, the time variable's own values, a fix** — except that tier 1 beats tier 2, because the filename is what the archive serves by. Tier 2 is modelled and currently unpopulated for CMIP. |
| 27 | **Once a header is in hand, parse all of it and store all of it.** Fetching is the expensive part and parsing is free, so a read taken for one reason fills in every fact it can. Stored as `FileHeaderDoc`, mirroring `DatasetRawDoc`. |
| 28 | **Which stored range answers depends on the question being asked.** `get_time_range` gains the units wanted, because a question in months should be answered from a calendar-free row and a question in days needs a dated one. |
| 29 | **A fix is refused if it contradicts what we measured.** An interpreted branch point which falls outside the parent's measured range is wrong, and enrichment is what makes that checkable. |
| 30 | **Parallelism is a later PR, and it targets measuring rather than fetching.** |
| 31 | **`read_variable_attributes` returns the attributes it was asked for, and reports every attribute it found when it cannot find them.** |
| 32 | **A whole file is fetched per question, never in bulk.** Option C above: tier 3 is entered deliberately, for a question that needs it, and the prefix path is what runs by default. |

### Decision 27, which is the one that shapes the code

> Getting the header is the expensive bit; parsing it is cheap. So once we have got the
> header, we may as well do all the parsing rather than risking having to retrieve the
> header again to do more parsing in future.

The numbers back this up. Stored as JSON, against what it cost to fetch:

| | attributes | as JSON | bytes fetched |
|---|---|---|---|
| CMIP5 globals | 27 | **1,555 B** | 6,364 B — and ~64 KiB once the variable list is wanted |
| CMIP6 globals | 52 | **3,161 B** | 42,487 B |
| CMIP6 `time` variable | 6 real | **318 B** | the same read |

About 3.5 KB kept against 64 KiB fetched — and, more to the point, **kept bytes do not
need a data node to be alive.** The first note records that 3 of 47 sampled header reads
timed out, so a re-read is not merely dearer, it is a different reliability class.

The precedent is exact. `DatasetRawDoc` already stores the full raw search JSON, for the
reason its own docstring gives: *"so nothing a record carried is lost to our column
choices."* `FileHeaderDoc` is the same move for a file header.

### Decision 31, and why the error message can still be generous

`read_variable_attributes(prefix, name, *, wanted=...)` returns the attributes asked for,
which makes the h5py-object problem impossible rather than filtered. The cost looked like
diagnostics — `NoTimeUnitsError` currently lists every attribute it found, which is how
you work out why a strange file has no units — but there is no real tension: parsing is
cheap, so the reader parses everything, returns what was wanted, and **puts everything it
found into the error**. Decision 27 pays for decision 31.

## Storage

Three tables. All in `src/esmporium/db/schema.py`, which deliberately does not use
`from __future__ import annotations` (it breaks SQLModel), gives every table
`model_config = {"validate_assignment": True}`, and stores every enum as
`Field(sa_type=AutoString)` — never a SQLAlchemy `Enum`, so adding a member later needs no
migration.

### `FileHeaderDoc`

One row per **file** whose header we have read, holding everything that read found.

| Column | Why |
|---|---|
| `file_id` | unique; FK `file.id`. The read is of a file, not of a version |
| `global_attributes` | JSON, as returned, nothing renamed. `DatasetRawDoc.raw_json`'s rationale applies verbatim |
| `variable_attributes` | JSON, keyed by variable name |
| `layout` | which `HeaderLayout` the bytes announced, so a per-format bug is findable |
| `bytes_read`, `route_url`, `data_node` | what it cost and who served it |
| `retrieved_at` | as `DatasetRawDoc` has it |

Keyed on the file rather than the version **deliberately**, and it is the same honesty
`DatasetVersionParent.file_id` already practises: one header answers a whole simulation,
so the facts derived from it are written against every version of that simulation while
this row names the one file whose bytes were really fetched. That is the record that makes
the answer recoverable if the file turns out not to be the one the index describes.

Every later consumer reads this rather than the network: parent facts, the time axis, and
whatever is next.

### `DatasetTimeRange`

`UniqueConstraint("dataset_version_id", "source")`, copying `DatasetVersionParent`
exactly.

| Column | Why |
|---|---|
| `dataset_version_id` | a range is a per-version fact |
| `source` | `TimeInfoSource`: `FILENAME`, `FILE_HEADER`, `FILE_VARIABLE`, `SEARCH_METADATA`, `FIX` — the four tiers plus the index |
| `status` | `MEASURED`, `NO_TIME_AXIS`, `NEEDS_A_CALENDAR`, `UNREADABLE_FILENAMES`. A row is written for the failures too, so nothing is retried for ever and an `fx` version's header is never fetched twice. `TimeRange` refuses an empty range, so "no time axis" has nowhere else to live |
| `start`, `end_exclusive` | half-open, no extent column. Nullable: a sub-monthly filename gives dates rather than a count |
| `units` | `str(TimeUnits)` — `months` for a natural value, `days since 1850-01-01` for a rich one. One column, so no half-empty rows. Precedent: `parent_time_units` |
| `calendar` | nullable, and `NULL` *means* "we did not have the calendar when we recorded this" |
| `is_contiguous`, `n_files` | the gap flag and how many files were measured |
| `start_raw`, `end_raw` | the observation; never used in arithmetic |
| `file_id`, `recorded_at` | provenance, as every other table here carries |

`SEARCH_METADATA` is included and is **always last in precedence** — CMIP7 STAC items and
CMIP6 Solr *file* records do carry start and stop datetimes, and nothing reads them yet.

### `DatasetBranchPoint`

Hangs off the child→parent **edge**, keyed on `parent_info_id` with `unique=True`, exactly
as `DatasetParentLink` is. So it carries **no source column**: a corrected branch time is a
`FIX`-sourced `DatasetVersionParent` row whose interpretation hangs off it, and
`preferred_parent_info` already decides which row speaks.

Columns: `parent_info_id`, `child_dataset_version_id` (derivable, kept for the same
one-indexed-lookup reason `DatasetParentLink` keeps it), `in_parent`, `in_child`, `units`,
`calendar`, `recorded_at`.

### Choosing which row answers

Not a fixed precedence tuple. Decision 28 makes it a function of the question:

```text
preferred_time_range(rows, *, wanted: TimeStep | None) -> DatasetTimeRange | None

  wanted is None          ->  the cheapest row that is MEASURED
  wanted is calendar-free ->  prefer a natural row; a dated row can answer too
  wanted is finer         ->  only a dated row can answer; otherwise NEEDS_ENRICHMENT
  a FIX row               ->  always wins
  two rows disagree       ->  the more trusted tier wins, loudly where it matters
```

`Covers` already holds the information this needs: `pad` and `min_overlap` are `Span`s,
and a `Span` carries its `step`. So `CatalogueWithTimeInfo.get_time_range` gains a
`wanted` argument in PR D. That is a change to a protocol PR B shipped days ago, and it is
the right change: the project is a prototype with no users, and an interface known to be
wrong is cheaper to fix than to work around.

## The enrich step

`src/esmporium/db/enrich_to_database.py`, following the
`plan_header_reads` / `record_header_parent_facts` / `build_header_facts_provider` split
in `db/parent_headers.py` and reusing its machinery rather than restating it:
`get_or_create`, `flushed_id`, `describing_version`, the smallest-first file candidates,
and the buffered-`HeaderReadCall` observer written in a `finally` so a read which raises
still leaves behind what the data nodes did.

```text
enrich_datasets(session, inputs, …)
     │
     ├─ plan_enrichment            which versions, which simulations, which edges
     │      │
     │      ├─ filenames_to_read          tier 1 — free, no network
     │      ├─ headers_to_read            tier 2 — one per SIMULATION, 64 KiB each
     │      ├─ files_to_fetch             tier 3 — whole files, only where needed
     │      ├─ branch_points_to_interpret
     │      ├─ needs_a_fix                a raw value we cannot interpret at all
     │      └─ unreadable                 nothing fetchable was published
     │
     ├─ record_filename_ranges     FILENAME rows, every status
     ├─ store_file_header          FileHeaderDoc — everything the read found
     ├─ record_time_axis           FILE_HEADER rows, from the stored doc
     ├─ record_variable_range      FILE_VARIABLE rows, from a whole file via netCDF4
     └─ record_branch_point        DatasetBranchPoint, validated
```

- A range is recorded against the **describing version** of each dataset in
  `enrichment_inputs`, resolved through `describing_version` exactly as `parent_of` already
  resolves a parent. `EnrichmentInput.dataset_id` is a `Dataset` id and its docstring says
  which version to measure is the enricher's question; this is the answer.
- Header reads are batched **per simulation**, because a calendar belongs to the run — the
  same argument `HeaderReadPlanItem.dataset_version_ids` already makes for parents.
- A header read is never *triggered* to add a rich row to monthly or yearly data. A monthly
  question answers from the filename for nothing; a question in days against monthly data
  reports `NEEDS_ENRICHMENT` and a header is fetched then. But a header fetched for *any*
  reason is parsed in full and ingested in full (decision 27).
- `files_to_fetch` is the Option C path, and it is the only one that touches bandwidth
  beyond a prefix. It is populated **only** where a question needs tier 3 and the cheaper
  tiers cannot answer, and never for a whole archive at once (decision 32).
- Every writer takes a session and **does not commit**; the entry point owns the boundary.
- A `TIME_FACT_COLUMNS` tuple plus the "asserted wins, silence changes nothing" loop from
  `save_parent_info`, so a re-run refreshes what it learned and leaves alone what it did not.

### Interpreting a branch point

`Covers._check_the_axes` requires `branch.parent_axis.is_comparable_with(parent_axis)`, and
`TimeAxis.is_comparable_with` compares units **exactly**. So a branch point stored as
`days since 1850-01-01` against a parent whose range is natural `months` is a loud `Fail`,
not an alignment question — and the interpreted value therefore has to be stored **on the
axis the parent's own range is stored on**.

Getting there needs four things, which is why this is the one piece with a real ordering
constraint:

1. the raw `DatasetVersionParent.branch_time_in_parent`;
2. `parent_time_units`, which gives the epoch — CMIP6 publishes it
   (`days since 0701-01-01` in the sampled file), CMIP5 publishes nothing;
3. the **parent's calendar**, from a header read of the parent;
4. the parent's own `DatasetTimeRange`, to know the target axis.

So `plan_enrichment` orders the work filenames, then headers, then branch points — the same
order `Covers` asks its questions in. And **enriching a branch point needs
`esmporium[calendars]` even for monthly data**, because turning a day count into a month
ordinal needs leap rules. That is an enrich-time install requirement, not a solve-time one;
the monthly solve path stays free, and it sits beside the extras enrichment needs anyway.

## Fixes, and the check that makes them safe

`src/esmporium/fixes/time.py`, reusing `ParentFix`'s shape: a whole source payload, plus a
**mandatory** `reason` and `evidence`, with that file's own warning kept —

> **Required, and required to be honest.** The one thing that must not happen is a guess
> recorded as a certainty: a fix outranks the file itself, so an unfounded one is worse
> than no fix at all.

`schema.py` already records a sampling across 47 CMIP5 models on 2026-11-04, in which the
same `branch_time` attribute carried at least five incompatible encodings. Sorted by
whether the encoding is self-evident from the value:

| Model / case | Raw | Reading | Writeable as a fix? |
|---|---|---|---|
| IPSL-CM5A-LR | `185001.0` | `YYYYMM` | **yes** — six digits, plausible year, month 01 |
| FIO-ESM | `20060101.0` | `YYYYMMDD` | **yes** — eight digits, an rcp start date |
| bcc-csm1-1-m `abrupt4xCO2` | `240.0` | a calendar year — the child's own filename starts `024001`, i.e. year 240 | **yes, and checkable** |
| IPSL-CM5A-MR | `1850.0` | year 1850, or 1850 days into a control | no — ambiguous |
| CCSM4, bcc-csm1-1 | `2.0`, `160.0` | years, or days | no — ambiguous |
| BNU-ESM, GFDL-CM3, HadGEM2-ES, MPI-ESM-MR | `0.0` | nothing was recorded | **no — unfixable** |

The last row is the case the status enum exists for: there is no information in the file,
so the only honest report is *"this cannot be determined from what was published; a fix is
the only way through, and we do not have one."*

The third row is the interesting one, and it is why decision 29 exists. The fixture in this
repository is a `bcc-csm1-1-m` `abrupt4xCO2` file whose own range starts at year 240 and
whose `branch_time` is `240.0`. Read as a calendar year in the parent's axis that is
coherent — and it is **checkable**, because after enrichment we hold the parent
`piControl`'s measured range. So the writer refuses a fix whose interpreted branch point
falls outside the parent's measured span. Enrichment does not merely consume fixes; it
validates them.

## Deferred: parallelism, as its own PR

Once the workflow above is settled, not before.

The fetching is I/O and threads are already right for it — `read_headers` has the pool, the
per-data-node cap and the buffered observer. The part that would gain from **processes** is
the **measuring**: a sub-daily version has a great many files, and each one costs a `cftime`
call through `position_of_date` before `measure_version` ever runs.

The seam already exists and is already picklable: `PublishedRange` and `TimeRange` are
frozen dataclasses of plain values, `measure_version` is a pure function over them, and no
`Session` crosses that boundary.

PLAN step 8's warning about the download prototype applies here and is worth repeating: it
grew an AIMD congestion controller, node eviction and affinity clustering, roughly 330
lines of generic scheduler, **none of which ever fired against the real network**. Start
with a fixed pool; add adaptation only if a measurement asks for it.

## Still open

One question, and it decides whether a code path gets written at all:

**Does any CMIP file carry `time_coverage_start` / `time_coverage_end`?** Neither of the
two sampled here does. The column and the `FILE_HEADER` source value cost nothing and are
in the plan either way, so tier 2 is modelled; but a *reader* with no customer is a path
nobody exercises, and until a file turns up carrying one, none is written.

## Verification

```text
make migration MESSAGE="add file header doc, time range and branch point tables"
make migration-sql       # the schema applying every migration from scratch gives
make test                # fast suite, no network
make test-headers        # opt-in, live data nodes
make licence-check       # the new `netcdf4` extra needs listing in licence-check.txt
make checks
```

Adding the extra also means regenerating the locked requirements files, so that is part of
the PR rather than a follow-up.

Then end to end against a populated database: search a small CMIP6 requirement, solve a
`Covers` requirement against it, hand `result.enrichment_inputs` to
`enrich_datasets(session, …)` unmodified, and confirm that it writes a row for every
version named and that a second `plan_enrichment` has nothing left to do.
