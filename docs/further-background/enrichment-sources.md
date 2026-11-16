# Enrichment, part two: where time information comes from, and how we read it

Design note sitting between
[the first enrichment note](enrichment.md) (the value types and `Covers`, now built)
and [the third](enrichment-storage.md) (storing it, and the enrich step).
It answers two questions the first note got partly wrong,
and that building PRs A to B2 showed to be the hard part:

1. where does each piece of time information come from,
   what does it cost to get, and how far can it be believed?
2. what reads it?

Written: 2026-11-16.
Everything marked *measured* was run against this repository's own fixtures
(`tests/test-data/headers/`, `tests/test-data/search/`)
or against a synthetic file built for the purpose,
and is reproducible from them.

## The short version

Retrieving time information is painful in one place only,
and it is not the place the first note thought.

| Source | Gives | Extra cost | Can it be wrong? |
|---|---|---|---|
| search record | first and last time *value* of a dataset version | **none**: already stored in `DatasetRawDoc` | yes |
| filename | first and last stamp of each file, so gaps between files | **none**, once the file list has been fetched | yes |
| file header (global attributes) | branch time in parent and child, parent time units | one ranged GET per file, 64 KiB | yes |
| variable attributes | time units, calendar, name of the bounds variable | **the same GET** as the header, in both samples | not checkable (see below) |
| variable values (first and last only) | exact start and end of a file, from its time bounds | two or three more small ranged GETs per file | no |
| fix | whatever we write | none | only if we got it wrong |

Nothing here is painful.
The first and last time bounds of every file
are enough to check a version's start, end and every join between files exactly,
and that is the most expensive thing enrichment ever reads.

Reading *every* time value of a file is deliberately not on this list.
It would show gaps or irregular steps *inside* a file,
but a check that thorough also has to look at the data itself (missing values, for a start),
so it is a different and much more expensive thing again,
and not part of enrichment.
For the record, reading every time value would cost roughly one request per time step
for files as CMIP5 and CMIP6 wrote them,
and one small read for a CMIP7 file (see "Variable values").

## Fast paths that guess, and the slow path that checks

One pattern recurs through every source below, so it is named once here.
The cheap sources give a value only with an assumption attached,
and the time bounds are what check it:

| Fast path | What it assumes | Checked by |
|---|---|---|
| filename end | the last step is one reporting interval wide | the last upper bound |
| search record start and end | each is a time *point* inside its own step, and the steps are as wide as the reporting interval says | the first lower and last upper bound |
| a missing `branch_time_in_child` (CMIP5) | the child branched at its own first time bound | a fix |
| the width of a reporting interval (`SPAN_BY_REPORTING_INTERVAL`) | `mon` is a month, `3hr` three hours, and so on | the bounds |

Every value that rests on an assumption is stored with the assumption named,
and every answer built on one says so,
which is what makes "if this looks wrong, check it with the bounds" an instruction rather than a hope.

## The sources, one at a time

### Search records

Every search API we query publishes a start and an end
**at the dataset (version) level**, for every project we have recorded.
*Measured* from the recorded responses in `tests/test-data/search/`:

| Recording | Field names | Example |
|---|---|---|
| ESGF 1 Solr, CMIP5 | `datetime_start`, `datetime_stop` | `1850-01-16T12:00:00Z` to `2005-12-16T12:00:00Z` |
| ESGF 1 Solr, CMIP6 | `datetime_start`, `datetime_stop` | `1850-01-16T12:00:00Z` to `2014-12-16T12:00:00Z` |
| ESGF 1.5 bridge, CMIP6 | `datetime_start`, **`datetime_end`** | `1970-01-16T00:00:00Z` to `2014-12-16T00:00:00Z` |
| ESGF-NG STAC, CMIP6 and CMIP7 | `start_datetime`, `end_datetime` (STAC common metadata) | `2022-01-16T00:00:00Z` to `2100-12-16T00:00:00Z` |

Three things to notice.

- This corrects the earlier understanding that CMIP6 publishes this per *file*
  and CMIP7 per *dataset*:
  all three projects publish it per dataset in the records we search.
  (ESGF 1's *file* records may carry it too; we do not search files that way, so it is not used.)
- These are the first and last **time coordinate values**, which are points, not bounds:
  `1850-01-16T12:00` is the middle of January 1850.
  That is annoying, because a dataset's extent is set by its bounds.
  With a guess (that each point lies inside its own step, and the steps are as wide as the
  reporting interval says) they give a natural range (January 1850 to December 2014, so
  half-open end January 2015) and a rich one, but **neither is the dataset's actual extent**:
  that comes from the bounds only.
  So this is a fast path with a guess, checked by the slow path, like the filename.
  They carry no calendar either.
- The field names differ by API (the bridge says `datetime_end`).
  That is search-API knowledge,
  so reading them belongs with the result parsers in `esmporium.search`,
  not in `esmporium.enrich`.

Nothing in `src/` reads these fields today, which is the gap in our current time handling.
They cost nothing, because `DatasetRawDoc` already holds the raw records,
so they can be back-filled for everything we have ever searched without touching the network.

They are publisher metadata, computed by whatever indexed the dataset,
so they get the same treatment as a filename:
believed until something looks wrong, then checked against the data.

### Filenames

`…_185001-201412.nc`: the first and last stamp of each file,
written to the precision of the dataset's reporting interval.

- Free, once the file list is in the database
  (the file list itself needs one file search per dataset, which is the data-access step's job).
- The only cheap source of **gaps between files**:
  two files whose stamps do not meet are a gap,
  which no dataset-level source can show.
- Inclusive, and turning the last stamp into a half-open end is an *assumption*:
  that the last step is one reporting interval wide.
  That is "the data runs to the end of the last time bound",
  which is true when the bounds are contiguous and as wide as the interval says,
  and is not even well-defined for point (`…Pt`) data,
  whose values are instants rather than intervals.
  So a filename end is recorded as derived-under-an-assumption,
  and the time bounds are what check it.

### File header (global attributes)

What a run says about itself:
for time purposes, `branch_time_in_parent`, `branch_time_in_child` and `parent_time_units`
(CMIP6 and CMIP7), or a bare `branch_time` (CMIP5).
Already read by `esmporium.headers` for parent information.

- One ranged GET of 64 KiB per file, against a data node
  which may be slow or down (3 of 47 sampled reads timed out).
- **Can be wrong**: nothing validates a published header,
  and the first note records five incompatible encodings of CMIP5 `branch_time` across 47 models.
- Carries **no start or end date** in any CMIP5 or CMIP6 file sampled
  (no `time_coverage_start` / `time_coverage_end`),
  so it is not a source of a dataset's extent and is not modelled as one.

### Variable attributes

The time variable's `units` (`days since 1850-01-01`), `calendar` and `bounds`.
Separate from the globals,
because they are a different part of the file and a different kind of fact.

- **The same retrieval as the header in practice.**
  *Measured*: in the CMIP6 sample the calendar is at byte ~24,900;
  in the CMIP5 sample the variable list starts directly after the globals at byte 6,300.
  Both are well inside the 64 KiB we already ask for.
  So reaching them is a *parsing* change, not a fetching one.
- **Not checkable rather than not wrong.**
  A file can carry a wrong calendar attribute,
  but nothing else in the file or the archive records the calendar to check it against.
  So it is treated as authoritative,
  and the only thing that overrides it is a fix.
- A missing `calendar` is an error that lists the attributes found, not a default.
  CF does say a missing calendar means `standard`,
  but that is a rule about how to read a file, not evidence about what the model ran,
  and a silent default is exactly how a day count comes out wrong unnoticed.
  A fix can supply it.

### Variable values

The time coordinate and its bounds: the truth.
Enrichment reads **only the first and last** of them:
the first lower bound is where a file's data starts, and the last upper bound is where it ends,
with no assumption about step widths.
(Reading every value is a more thorough and much more expensive check, and not part of enrichment;
see "The short version".)

What that costs:

- **Classic netCDF.** `time` is the record dimension,
  so its values are interleaved one per record across the file
  (≈ 204,835 bytes apart in the CMIP5 sample).
  But the header gives `numrecs`, the record size and each variable's `begin`,
  so the offset of record *i* is arithmetic.
  `time` and `time_bnds` sit next to each other in a record,
  so **the first and last bounds are one small ranged GET each**.
- **netCDF-4, as CMIP6 wrote it.** *Measured* on the CMIP6 sample:
  `time` is chunked `(1,)` and `time_bnds` `(1, 2)`,
  one value per chunk, as CMOR appends them,
  and their chunk indexes sit at bytes 51,815 to 56,527, just past the fixture
  but inside a 64 KiB ask.
  So the earlier note's "tens of kilobytes sitting in one place" does not hold for this file,
  but first and last are still only the chunk index plus two chunk reads.
- **netCDF-4, repacked (CMIP7).** CMIP7 requires files to be *repacked* for remote access
  ([`cmip7repack`](https://pypi.org/project/cmip7-repack/), with CMOR 3.14 chunking to suit;
  see the [CMIP7 FAQ](https://cmip7-guidance.pages.dev/CMIP7/FAQ/)).
  Repacking puts the time coordinate in **a single compressed chunk**
  and asks HDF5 to place its B-trees at the start of the file,
  so the first and last bounds are one small read near the front,
  quite possibly inside the prefix we already fetch.
  Not yet measured here: we hold no CMIP7 file fixture,
  and getting one is part of PR C1 in [part three](enrichment-storage.md#pr-c1-readers-esmporiumheaders).

The layout is a property of the file, not of the project
(a CMIP6 file could be repacked; a CMIP7 file might not be),
so the readers do not key on project.
They ask for what they need through the byte source's budget,
and a well-packed file simply costs less.

Done for every file of a version, first and last bounds check the version's start, end
*and* every join between files exactly.
This is the normal way to double-check a filename or a search record.
From a local file it is cheaper again,
though see [parallel enrichment](enrichment-storage.md#parallel-enrichment):
on some systems even local reads are slow.

Once the bounds are in hand, everything is parsed out of them
(the rich range, and the natural range where the calendar allows it),
because getting them was the expensive bit.

### Fixes

A row we write ourselves, with a mandatory reason and evidence,
which outranks every published source.
Not only for CMIP5 branch times:
anything above can be wrong and has a fix as its remedy.

## Using the source as a diagnostic

Every stored time fact records which source it came from,
and every answer `Covers` gives names the sources of the numbers it used.
That turns provenance into advice:

- an answer resting only on **search records or filenames** is the cheap answer.
  If it looks wrong, the next step is to enrich those datasets from
  **variable values (first and last)** and see whether the numbers change;
- if the values *confirm* the published numbers and the answer still looks wrong,
  the data really is like that, and the remedy is a **fix**
  (or a conversation with the modelling centre), not more enrichment;
- the same goes for branch times, which come only from headers:
  a branch point outside its parent's range is a header to distrust.

When two sources disagree, both rows are kept and the disagreement is surfaced;
the more trusted one answers (see [the third note](enrichment-storage.md#choosing-which-row-answers)).

## What reads it

### The constraint

What we need from a reader:

1. classic netCDF (`CDF\x01`, `CDF\x02`, most of CMIP5) **and** netCDF-4/HDF5;
2. lazy: read the header, variable attributes, and a handful of values
   **without** fetching the whole file,
   from a remote data node over HTTP ranges *and* from a local file;
3. the netCDF data model, not HDF5's internals;
4. HTTP that we control (timeouts, status codes, retries, per-node limits),
   which is what `esmporium.headers.fetching` already does with `httpx`.

### What was looked at

| Tool | Classic | netCDF-4 | Lazy over a Python file object | Clean netCDF attributes | Verdict |
|---|---|---|---|---|---|
| our `headers/netcdf3.py` | yes | no | yes (it parses bytes) | yes | **keep and extend** |
| `h5py` | no — `OSError: file signature not found` | yes | yes | **no** — adds `CLASS`, `NAME`, `_Netcdf4Dimid`, `REFERENCE_LIST` | replace with `h5netcdf` |
| `h5netcdf` (over `h5py`) | **no** — *measured*: `OSError: file signature not found`, on both the CMIP5 prefix and a complete classic file | yes | **yes** — *measured* over our `_PrefixFile`, reading no further than byte 42,231 | **yes** — *measured*, identical to `netCDF4`'s list | **use for netCDF-4** |
| `netCDF4` on a local path | yes | yes | lazy on disk, but takes a path or a bytes buffer, never a file object | yes | not needed (see below) |
| `netCDF4` with `memory=` | yes | yes | no: needs the complete file image | yes | no |
| netcdf-c byte-range (`url#mode=bytes`) | yes | yes | yes, but the HTTP is inside the C library | yes | no: not in our control, Unidata do not call it production-ready, and wheel support is unconfirmed |
| `scipy.io.netcdf_file` | yes | no | **no** — *measured*: with a file object it reads the whole file on open (7,870,392 of 7,870,392 bytes) | yes | no |
| `xarray` | via scipy or netCDF4 | via h5netcdf or netCDF4 | only as lazy as the engine underneath | yes | no: a wrapper, and its classic path is scipy's |
| `ncdata` | via netCDF4 | via netCDF4 | no: its file I/O *is* netCDF4-python | yes | no: an in-memory data model for moving between iris and xarray |
| `kerchunk` | yes, through scipy | yes | yes, via fsspec | yes | no: built to emit Zarr references, and its classic path is scipy's |
| `VirtualiZarr` | yes, its own parser | yes | yes, via obstore | yes | no as a dependency (see below); yes as prior art |
| `pyfive` | no | yes | yes | as `h5py` | not now; a possible future swap for `h5py` (pure Python, thread-safe) |

Measured with `h5py` 3.16.0 (HDF5 2.0.0), `h5netcdf` 1.8.3, `scipy` 1.18.1 and
`netCDF4` 1.7.5.1, in a throwaway environment.

So: **no single tool does all four**.
The one that comes closest, netcdf-c's byte-range mode,
fails the fourth requirement, which is the one we already decided not to give up
(see `esmporium.headers.hdf5`'s module docstring).
xarray is not smarter than what it wraps:
its classic engine is scipy, which reads everything.

### The decision

**Our own classic parser, and `h5netcdf` for netCDF-4,
both reading from one "byte source" that is either HTTP ranges or a local file.**

- **Classic: extend `headers/netcdf3.py`.**
  Rolling our own sounds insane and is not,
  because classic netCDF is small, fully specified and frozen,
  and we already parse its global attributes.
  The variable list sits directly after the globals and reuses the same attribute structure,
  and record offsets are arithmetic on `numrecs`, `recsize` and `begin`.
  The one trap is `CDF\x02`, where `begin` widens to eight bytes
  (and `CDF\x05` if it ever turns up).
  **Written from the format specification, not from anyone else's parser:**
  the BNF grammar in the netCDF Users Guide,
  [Appendix B, File Format Specifications](https://docs.unidata.ucar.edu/nug/current/file_format_specifications.html),
  which is also published as the community standard
  [ESDS-RFC-011](https://www.earthdata.nasa.gov/s3fs-public/imported/ESDS-RFC-011v2.00.pdf).
  Each grammar rule the parser implements cites the rule it implements,
  so a reviewer checks the code against the specification, and nothing else.
  This is also what others have concluded; see "Why not VirtualiZarr" below.
- **netCDF-4: `h5netcdf` instead of bare `h5py`.**
  Same file-object interface we already use,
  so it reads lazily through the same machinery,
  and it presents the netCDF data model,
  which removes the bookkeeping-attribute problem
  (including `REFERENCE_LIST` coming back as object references) rather than filtering it.
  It sits in the existing `headers` extra beside `h5py`, which it depends on.
- **One byte source, two implementations.**
  Today `_PrefixFile` holds one prefix and raises past it.
  It becomes a range-backed file object that fetches what is asked for, on demand,
  up to a stated budget of bytes and requests,
  and still raises (never zero-fills) past the budget.
  A local file is the same interface with `open(path, "rb")`,
  so local and remote share every parser and every test.
- **No `netCDF4` extra.**
  The previous note planned one, for whole files.
  With the byte source above, a local file is read lazily by our own readers,
  so `netCDF4` adds a compiled dependency to get what we already have.
- **Why not VirtualiZarr.**
  It is the closest thing to what we need for classic files.
  Its 2.8.0 release (14 October 2026) replaced the kerchunk/scipy route
  with its own header parser:
  *"NetCDF3Parser now reads the netCDF3 header itself instead of going through
  `kerchunk.netCDF3.NetCDF3ToZarr`, so virtualizing a netCDF3 file no longer requires
  `kerchunk` or `scipy`"*
  ([release notes, v2.8.0](https://virtualizarr.readthedocs.io/en/latest/about/releases.html);
  [the parser](https://github.com/zarr-developers/VirtualiZarr/blob/main/virtualizarr/parsers/netcdf3.py)).
  Their stated reasons are that scipy cannot read CDF-5 or the unsigned and 64-bit integer types,
  and, from the module docstring, that the format is
  *"a small, fully-specified big-endian binary grammar"*.
  (No pull request or issue discussion was found to link beyond those two.)
  We do not depend on it, for four reasons:
  1. its public interface builds a virtual Zarr store (`NetCDF3Parser(...)(url, registry) -> ManifestStore`);
     the header parser itself, `_parse_header`, is private, so using only the part we want
     means depending on an internal function;
  2. it does not read values, so first and last bounds would still be our own range reads
     at offsets we compute;
  3. its core dependencies are xarray, zarr 3, numcodecs, obstore and obspec_utils,
     a lot to install for reading a header;
  4. its I/O goes through obstore, not through `esmporium.headers.fetching`,
     so we would lose our own timeouts, retries, per-data-node limits and `HeaderReadCall` records.

  Its source is deliberately **not** used as a reference while writing ours:
  reading another implementation and then writing "our own" blurs whose code it is,
  and the format is simple enough that the specification is all that is needed.
  Nor is it used as a test oracle: the format is small enough that tests against
  our own fixtures and a synthetic file are enough.
- **`h5py` threads do not parallelise.**
  `h5py` serialises every call behind a global lock,
  so threads speed up waiting on the network but not parsing.
  That matters for [parallel enrichment](enrichment-storage.md#parallel-enrichment),
  and is why `pyfive` is worth remembering.

## Retrieve once, parse everything

> Getting the information is the expensive bit; parsing it is cheap.
> So once we have it, parse everything we can out of it.

This was decision 27 of the previous storage draft, for file headers.
It generalises to every source,
and the shape it gives the code is one `parse_*` function per source,
each pure, each returning everything that source can say:

| Function | Input | Returns |
|---|---|---|
| `parse_search_record` | one raw search record, and which API it came from | first and last time value (raw text), the natural and rich ranges guessed from them with the assumption named, and anything else time-like the record carries (e.g. CMIP7's published branch units) |
| `parse_filename` | a filename and the dataset's reporting interval | raw stamps, any suffix (e.g. `-clim`), the natural range where the step is calendar-free, and the assumption used for the end |
| `parse_file_header` | a file's global attributes | parent facts (what `db/parent_headers.py` extracts today) and the raw branch times with their units |
| `parse_variable_attributes` | a variable's attributes | time units, calendar, bounds variable name |
| `parse_variable_values` | the first and last time bounds, with the axis from `parse_variable_attributes` | rich start and end, and the natural range where the calendar allows it |

Two things follow.

- **A retrieval and a source are not the same thing.**
  One 64 KiB read yields both a file header and variable attributes,
  so the enricher dispatches every retrieved artefact to *every* `parse_*` function that applies.
  That is what stops "we fetched the header for parents" and "we need the calendar"
  from turning into two fetches.
- **`enrich_datasets` stops deciding what to record.**
  It plans what to retrieve, retrieves it, hands it to the parsers,
  and writes what they return.
  What can be learnt from a source lives in that source's parser, and its tests,
  and nowhere else.

Search records are retrieved at search time,
so by the same principle `parse_search_record` runs when results are saved,
and the enricher only back-fills rows saved before it existed.

## What this changes in the earlier notes

- [The first note](enrichment.md)'s "Where the numbers come from" is superseded by this one.
  Its claim that "a header read cannot cheaply give start and end" holds for the header,
  but not for the time bounds: first and last are a few small requests.
- [The storage note](enrichment-storage.md) as reviewed in #74 planned three readers
  (`netcdf3.py`, `h5py`, `netCDF4`) and a `netCDF4` extra (its decisions 24 and 25),
  four tiers with "filename beats header", and `time_coverage_*` as a tier.
  All of that is replaced by the above, and that note has been revised to match.
