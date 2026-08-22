# Caching and dependency checking for large-data processing

Investigation into how `esmporium` should decide what to re-run
when processing hundreds of GB per dataset and TBs in aggregate.

Written: 2026-08-22.

## Recommendation

**Use [pytask](https://pytask-dev.readthedocs.io/) as the DAG and caching engine,
with a thin materialisation layer of your own.**

pytask is a pytest-style build system: tasks are plain Python functions,
dependencies and products are declared through annotations, and a state database
decides what needs re-running. It is a library, not a platform — nothing to
operate, nothing running between invocations.

Four things make it the right fit:

1. **The `PNode` protocol is exactly the mixed model you need.** A node
   implements `state()`, `load(is_product)` and `save(value)`. So a step can
   return an `xarray.Dataset` and have it persisted and reloaded transparently,
   while a step producing a 100 GB file takes a path, writes it itself and
   returns nothing — both in the same DAG, no adapter layer.
2. **`state()` is your invalidation hook.** The default returns an mtime, but it
   can return the ESGF-published checksum from your database instead. That gives
   correct content-based invalidation *without ever hashing a terabyte*.
3. **It is Python only, and declarations live in one place.** No DSL, no
   generated build files, no magic globals. Inputs, outputs and resources are
   declared once on the decorated function and everything else is derived — see
   [Single source of truth](#single-source-of-truth-the-decorator-is-the-only-declaration).
   This gives Prefect's best property — decorate a function, let the tool work
   out the graph — without Prefect's caching model.
4. **Remote execution is available when you need it.** `pytask-parallel` accepts
   any registered `concurrent.futures.Executor`, including a Dask client, with a
   `remote=True` flag that syncs files to workers. Dask plus `dask-jobqueue`
   reaches SLURM and PBS, and Dask is already the climate community's standard
   through Pangeo.

The main alternative is [snakemake](#snakemake), which is more battle-tested and
has a better cluster job model, at the cost of a DSL, an adapter layer, and a
permanent config-serialisation tax. See
[When to switch to snakemake](#when-to-switch-to-snakemake) for the concrete
trigger.

## The step granularity rule

This is the most important design rule in the document, and it is easy to get
wrong in a way that is expensive and annoying to undo later.

> **A step exists because its output is expensive to produce, or because it is
> shared by several consumers — not because it is a logical unit of code.**
>
> **Cheap, unshared transforms belong inside a step, not between two.**

### Why this matters so much

Every step boundary costs a full serialise and deserialise of whatever crosses
it. If a step returns a 5 GB `Dataset`, that is a 5 GB write followed by a 5 GB
read before the next step can begin. Five "logical" steps chained together
become five writes and five reads of data that never needed to touch disk.

This is not a flaw in any particular tool. Persistence *is* the cache — a result
that was not written down cannot be reused on the next run. So the cost is
unavoidable at every boundary you create, which means boundaries must be chosen
deliberately rather than falling out of how you happened to organise the code.

### The test to apply

Before making something a step, ask:

> *Would I be happy to pay a full write and a full read of this output, in order
> to avoid recomputing it **for every consumer**?*

If the answer is no, it is not a step. It is a function that another step calls.

The emphasis matters, because there are two independent reasons to say yes:

- **Expensive.** Downloading a 40 GB dataset — hours of network time avoided for
  one write. Obvious.
- **Shared.** A global mean of a parent dataset consumed by forty ensemble
  members. Cheap in isolation, but computing it once instead of forty times is a
  40× saving on that work. Also obvious, once you look at the fan-out.

So the failure mode is not only "too many steps". It is also **too few**: fusing
a shared intermediate into its consumers silently multiplies it by the fan-out.
See [the cost of no DAG](#the-cost-of-no-dag-shared-work-is-repeated) for what
that looks like at scale.

A `.mean("time")` used by exactly one downstream step: no, never — seconds of
compute for gigabytes of I/O. The same `.mean("time")` used by forty: yes,
clearly.

### The distinction being drawn

Logical structure and caching structure are different axes, and conflating them
is the mistake:

- **Logical structure** is what makes code readable and testable. It belongs in
  ordinary Python functions, and you should have lots of them, as small as you
  like.
- **Caching structure** is what makes re-runs cheap. It belongs in steps, and you
  should have as few as you can get away with.

A step body calling six well-named helper functions is the target shape. Six
steps of one function each is one failure mode — it looks tidier, tests the same,
and runs far slower. Fusing a six-way-shared intermediate into its consumers is
the opposite failure mode, and the more expensive one.

### Worked example

Loading a netCDF, subsetting to a time range, converting units, regridding, and
computing an anomaly is **one step**, not five:

```python
@step(version="1")
def anomaly(raw: xr.Dataset) -> xr.Dataset:
    ds = _subset_time(raw, START, END)
    ds = _to_kelvin(ds)
    ds = _regrid(ds, TARGET_GRID)
    return ds - ds.mean("time")
```

Each helper is independently testable. Only the expensive, reusable result
crosses a boundary. If regridding later becomes expensive enough to be worth
caching on its own, promoting it to a step is a small, local change — whereas
demoting four steps into one, after downstream code has grown to depend on the
intermediate artifacts, is not.

Concretely, promoting `_regrid` is a two-line change plus a new artifact type:

```python
class RegriddedDataset(Artifact):          # new: an identity for the intermediate
    materialiser = NetCDF()

@step(version="1")
def regrid(raw: RawDataset) -> RegriddedDataset:     # new: was a helper call
    return _regrid(_to_kelvin(_subset_time(raw, START, END)), TARGET_GRID)

@step(version="1")
def anomaly(ds: RegriddedDataset) -> AnomalyDataset:  # changed: input type only
    return ds - ds.mean("time")
```

Nothing else moves. The helpers are untouched, no path is written, and the edge
between the two steps appears purely because the types line up. Going the other
way — merging two steps — is equally small *at the point of change*, but every
consumer of `RegriddedDataset` must also be found and rewritten, which is the
asymmetry that makes starting coarse the safer default.

### The corollary for versioning

Coarser steps mean a code change invalidates more work. That is the real cost of
this rule, and it is the right trade: an unnecessary re-run of a cheap transform
costs seconds, while an unnecessary disk round-trip of a 5 GB object costs
minutes and happens on *every* run, cache hit or miss.

## Single source of truth: the decorator is the only declaration

The strongest ergonomic requirement, and the one that should drive the design:
**a step's inputs, outputs and resources are declared once, next to the
function, and everything else is derived.** Nothing is written twice, so nothing
can drift.

### The design

Give artifacts types, and let the types carry their own materialiser and path
convention. The function signature then *is* the dependency declaration:

First the artifact types, which own their format and their location:

```python
class Artifact:
    """Base: an identity, a materialiser, and a path convention."""

    materialiser: ClassVar[Materialiser]
    subdir: ClassVar[str]

    @classmethod
    def path_for(cls, key: DatasetKey, root: Path) -> Path:
        # The only place a path is ever constructed.
        return root / cls.subdir / f"{key.as_slug()}.nc"


class RawDataset(Artifact):
    materialiser = NetCDF()
    subdir = "raw"


class RegriddedDataset(Artifact):
    materialiser = NetCDF()
    subdir = "regridded"


class AnomalyDataset(Artifact):
    materialiser = NetCDF()
    subdir = "anomaly"
```

Then the steps, which mention neither format nor location:

```python
@step(version="1", resources=Resources(memory="60GB", cpus=8, walltime="4h"))
def regrid(raw: RawDataset) -> RegriddedDataset:
    return _regrid(raw, TARGET_GRID)


@step(version="1", resources=Resources(memory="8GB"))
def anomaly(regridded: RegriddedDataset) -> AnomalyDataset:
    return regridded - regridded.mean("time")
```

That is the whole declaration. From it, everything else is inferred:

- **The DAG.** `anomaly` consumes `RegriddedDataset`; `regrid` produces it.
  Therefore `regrid` runs first. No edge is ever written by hand — the graph is
  computed by matching return types against parameter types across the registry.
- **Materialisers.** `RegriddedDataset` knows it is netCDF and how to
  save/load/state itself.
- **Paths.** Computed from the artifact type plus the dataset identity, by
  convention. No path is written by hand, ever.
- **Cache keys.** Version from the decorator, input state from the artifact
  types, and params from the run configuration — a frozen dataclass such as the
  `ClientConfig` in
  [The pattern to adopt regardless](#the-pattern-to-adopt-regardless). Frozen so
  it is hashable and cannot drift mid-run; a dataclass so its fields are the
  complete, inspectable set of knobs a step can be affected by. Anything that
  changes a step's output but is not an input artifact belongs here, or it is
  invisible to the cache.

Registration enforces **one producer per artifact type**, raising at import time
if two steps return the same type. That is the discipline that makes type-based
inference safe, and it turns a whole class of wiring mistakes into an immediate,
obvious error rather than a silent one.

This is static inference rather than Prefect's runtime inference, which is
better here: the entire graph can be validated before a single byte is read, and
it can be generated from a database query.

**Could runtime inference ever be needed, and how hard would the switch be?**

Runtime inference means the graph emerges from actually calling the functions —
a step calls another step, and the framework records the edge. It becomes
necessary when the graph's *shape* depends on a computed value: recursion to
unknown depth, or branching where which step runs next is decided by data.

Phase A has exactly that shape (ancestry recursion), which is why phase A is
ordinary Python and not in the DAG at all. Phase B does not, and it is worth
being sceptical of arguments that it might, because "static" here is a weak
requirement: the graph is rebuilt from the database on every invocation, so new
datasets, new variables and new experiments all appear without any code change.
Static means "known before *this* run starts", not "fixed forever".

If it were ever needed, the migration is moderate rather than severe. Step
bodies and materialisers are untouched; what changes is that `StepSpec`
generation moves from an ahead-of-time pass to a recording wrapper, and
validations that currently run at import (one producer per type) have to become
runtime checks or be dropped. The realistic trigger would be a phase-B step whose
output determines which further steps exist — at which point the honest answer is
probably to keep phase B static and move that decision into phase A, rather than
to make the whole engine lazy.

### Large outputs fit the same model

An artifact type declares whether the step writes it directly:

```python
class DownloadedFile(Artifact):
    materialiser = None          # the step writes its own bytes
    writes_own_output = True

@step(version="2", resources=Resources(memory="2GB", walltime="1h"))
def download(record: DatasetRecord, out: DownloadedFile) -> None:
    _fetch(record.url, out.path)
```

The adapter sees `writes_own_output` and passes a path in rather than expecting
a return value. Both modes, one declaration style, and which mode is in use is
visible in the signature.

### `StepSpec` is derived, never authored

This is the correction to make explicit, because as presented earlier it looked
like a second declaration:

> **Nobody ever writes a `StepSpec`.** It is produced by reading
> `inspect.signature(fn)` plus the decorator's metadata.

The distinction that matters:

| | Declares | Written by | How many |
| --- | --- | --- | --- |
| `@step` decorator | A step *type* | You, once, on the function | One per step |
| `StepSpec` | A step *instance* | The generator, from the DB | One per step per dataset |

The decorator says "regridding takes a `RawDataset` and produces a
`RegriddedDataset`". The generator says "for dataset `CMIP7.ACCESS.tas.r1i1p1f1`,
that means reading *this* path and writing *that* one". You cannot maintain
those in sync incorrectly, because only one of them is maintained.

So `StepSpec` is a compilation artifact, like generated protobuf code. It does
not violate single-source-of-truth because it was never a source of truth.

### Resources ride along

`resources` on the decorator is the answer to wanting scheduler hints without
double declaration. It sits next to the function, travels into each `StepSpec`,
and each backend interprets it:

| Backend | What it does with `resources` |
| --- | --- |
| `ThreadPoolExecutor` / `ProcessPoolExecutor` | Ignores it |
| Dask / `dask-jobqueue` | Worker annotations and resource constraints |
| Parsl | Provider-level resource specification |
| snakemake (if you ever generate one) | Emits a `resources:` block per rule |

Declaring resources you cannot yet use costs nothing and means the information
exists the day you need it. This is the field that carries you across if you
ever hit the heterogeneous-cluster wall.

## The shape of the problem

The work splits into two phases with genuinely different characteristics, and
they should be built differently.

### Phase A — discovery (search, parse, ancestry)

Dynamic, recursive, and outside the DAG entirely. This is PR1–PR7 in `PLAN.md`:
search ESGF, parse into `Dataset` objects, walk ancestry by reading file headers
and recursing to unknown depth.

Two properties matter:

- **It must not be cached generically.** PR3 states this outright: ESGF state may
  have changed, so "I ran this two seconds ago" is not a reason to skip.
- **Its caching is semantic, not structural.** PR6's fast path — if you know the
  parent of `tas`, derive the parent of `tos` without querying — is not "have
  these inputs changed?", it is "a different computation already told me this
  answer". No workflow engine can express that.

This phase stays as ordinary Python against your database, which is how you are
already building it.

### Phase B — processing (download, compute, derive)

Static, wide, and expensive. Once phase A has populated the database you can
enumerate every dataset and therefore every step. This is where the TBs and the
compute hours live, and the only phase that needs a caching engine.

| Constraint | Value | Consequence |
| --- | --- | --- |
| Execution target | Mixed, undecided | Executor must be swappable |
| Graph shape | Static, generated from the DB | Mature static-DAG tools are in play |
| Interface stability | Internal, but injectable | Keep step bodies plain Python |
| Input verification | Cheap by default, strict opt-in | Need a pluggable "has this changed" |
| Output size | Both in-memory and 100+ GB | Need both materialisation modes |
| Dispatch | Trigger here, run elsewhere | Needs a real remote-execution story |

The boundary between phases is clean: **phase A writes the database, phase B
reads it to build a DAG.** Making that interface explicit in the code is what
keeps the choice of DAG tool reversible.

### Two output modes

Steps come in two kinds and both must work in one DAG:

- **Large outputs** — 100+ GB netCDF. The step writes the file itself; nothing
  should ever load it into memory to satisfy the cache.
- **In-memory outputs** — an `xarray.Dataset` that fits in RAM. The step returns
  it, and the caching layer persists it and hands it back, deserialised, to
  whatever consumes it next.

This is the **materialisation** problem, and it is separate from invalidation:

| Concern | Question it answers | Who owns it |
| --- | --- | --- |
| Invalidation | Should this step run? | The DAG tool |
| Scheduling / execution | Where and when does it run? | The DAG tool |
| Materialisation | How does a value become bytes and back? | You, via `PNode` |
| Provenance | What happened, durably? | Your database |

Tools differ sharply here. pytask's `PNode` and Dagster's IO managers provide
materialisation natively. snakemake does not — it is files all the way down — so
choosing it means writing that layer by hand.

## Options assessed

### pytask

**Recommended.**

Tasks are functions; dependencies and products are declared through annotations;
a state database tracks what has run. `pytask-parallel` adds execution backends.

The `PNode` protocol is the reason to pick it. A custom node implements `name`,
`signature`, `state()`, `load(is_product)` and `save(value)`:

- **`load`/`save`** are your serialisation hooks, and pytask never touches the
  bytes. A step can be a plain function of `xarray` objects:

  ```python
  def task_anomaly(
      ds: Annotated[xr.Dataset, NetCDFNode(path=raw)],
  ) -> Annotated[xr.Dataset, NetCDFNode(path=anom)]:
      return ds - ds.mean("time")
  ```

  while a large-output step in the same DAG takes a `PathNode`, writes its own
  file and returns nothing.
- **`state()`** is your invalidation hook. Default is mtime; returning your
  recorded ESGF checksum instead is about a dozen lines and gives you
  content-correct invalidation at the cost of a database lookup.

Other things in its favour: zero operational weight, no config-serialisation tax
for local runs (see [Config and the process boundary](#config-and-the-process-boundary)),
pytest-style ergonomics your team already knows, and a `remote=True` mode that
syncs files to workers.

**Risks**, stated plainly:

- **Small project.** ~145 stars, one primary maintainer, from the economics
  research community. Actively developed and cleanly designed, but this is a real
  bus-factor bet for infrastructure meant to run for years. Mitigated by the fact
  that your step bodies stay plain Python — the port cost is one binding class,
  not the science.
- **Execution model on clusters.** Dask via `dask-jobqueue` holds long-lived
  SLURM allocations with workers that tasks are dispatched to. That is a worse
  fit than per-job submission for long, heterogeneous, failure-prone jobs —
  though this is solved by using `submitit` instead of Dask, see
  [Per-job submission without snakemake](#per-job-submission-without-snakemake).
- **Less battle-tested at TB scale** than snakemake.

### snakemake

**The alternative, and the fallback.** Better engineered for scale than its
`make` heritage suggests.

In its favour:

- **Rerun triggers are not just mtime.** Defaults are `mtime`, `params`, `input`,
  `software-env` and `code` — a change to the rule's code, parameters, input set,
  or conda/container environment all force a re-run. Note that the trigger set is
  a **fixed enum you can subtract from but not add to**: `--rerun-triggers`
  selects a subset, and there is no plugin interface for a custom trigger. The
  `params` field below is therefore the only extension point, which is why it
  carries so much weight in this design.
- **`params` gives you the checksum trick.** Put the ESGF checksum in a rule's
  `params` and invalidation becomes a database lookup that snakemake enforces.
- **Content hashing when you want it.** Above `--max-checksum-file-size` it
  checksums content; `--rerun-triggers mtime` gives the cheap mode. Your
  "cheap by default, strict opt-in" answer as flags.
- **The best cluster story in the survey.** Executor plugins for SLURM (actively
  maintained, 2.7.1 as of June 2026), Kubernetes, Google Batch, Azure and AWS
  ParallelCluster. Critically, it submits **one job per rule**, so each step gets
  right-sized resources and failures are isolated.
- **Field standard** in computational and climate science. It is worth
  understanding why the DSL is not felt as a heavy price by its main users:
  snakemake grew up in bioinformatics, where a pipeline is a chain of *external
  command-line tools* — aligners, variant callers, format converters — joined by
  files. There, "everything is paths and shell commands" is not a compromise, it
  is an accurate description of the problem, and the DSL is doing genuine work
  that Python would do more verbosely. The cost only bites when steps are rich
  in-process Python objects that must be serialised to cross a rule boundary,
  which is precisely esmporium's situation and precisely not the median
  snakemake user's.

Against:

- **It is a DSL**, which is the thing you want to avoid.
- **No materialisation layer.** Rules receive and produce paths; a function
  returning an `xarray.Dataset` is not something snakemake can persist. You write
  the adapter.
- **Permanent config-serialisation tax.** Every rule runs in a fresh process that
  re-parses the Snakefile, so non-serialisable config must be reconstructed on
  every invocation, even locally. See below.
- **Two metadata stores.** `.snakemake/` decides what is done while your database
  separately records dataset state — a real tension for a package whose stated
  purpose is that everything goes in the database (PR3).

### Dagster

The richest data model of the candidates. Assets are the right abstraction —
declare the thing that should exist, let the framework decide whether to compute
it. **IO managers** handle materialisation natively. **`code_version` plus
`data_version`** gives real staleness detection. **`@observable_source_asset`**
returns a `DataVersion` for external data, so your ESGF checksum becomes the
version and downstream assets go stale correctly.

Rejected on cost, not capability: the operational weight is the highest here — a
code location server, a daemon, and a web UI — which is disproportionate for a
`pip install`-able scientific library. Code lock-in is also the highest, since
assets, resources and IO managers are pervasive idioms that do not port.

Worth reconsidering only if `esmporium` grows into a platform with a UI
requirement.

### DVC

The cleanest pure invalidation model in the survey: declared deps and outs,
content hashes in `dvc.lock`, `dvc repro` walking the graph, a content-addressed
cache, first-class remotes.

Rejected on specifics: **hashing is full-content MD5 with no cheap mode**, so at
TB scale `dvc status` becomes an I/O event rather than a query. Pipelines are
static YAML, the Python API is second-class next to the CLI, and it assumes Git
as the coordination mechanism.

### Prefect

Excellent at execution — retries, concurrency, observability, and the best
decorator ergonomics in the field. Rejected as a caching layer, for structural
reasons rather than misconfiguration:

- **Cache = memoisation.** [The results docs](https://docs.prefect.io/v3/develop/results)
  are explicit: return values are serialised to storage (default
  `~/.prefect/storage`, default pickle) and a cache hit *loads the result back
  into memory*. There is no notion of "the output is a file over there".
- **No input-file content hashing.** In
  [`cache_policies.py`](https://github.com/PrefectHQ/prefect/blob/main/src/prefect/cache_policies.py)
  the built-ins are `Inputs`, `TaskSource`, `RunId`, `FlowParameters` and
  `CompoundCachePolicy`. `Inputs` hashes argument *values*, so a `Path` hashes
  the string, not the bytes. Fixable with a custom `CachePolicy` (~15 lines), but
  that fixes only half the problem.
- **No output validation.** Delete a cached output file and Prefect still reports
  a cache hit, because the key never referred to the file. Not fixable within the
  model.
- **Requires an API** for orchestrated runs — a server, or ephemeral mode with
  SQLite. `.fn()` bypasses the API and also bypasses caching entirely.

### redun

The most elegant design surveyed. Content-addressed caching over a call graph in
SQLAlchemy with Alembic; code-change detection by hashing task functions; `File`
values hashed cheaply as `(path, size, mtime)`; reverse validity checking of
*outputs*, which is the thing Prefect lacks; and a `Value` protocol
(`get_hash` / `serialize` / `deserialize` / `is_valid`) that is a genuinely good
materialisation answer.

Rejected on: hard, non-optional dependencies on `aiobotocore[awscli,boto3]`,
`boto3`, `s3fs`, `aiohttp` and `textual` — an AWS SDK and a TUI framework in a
climate library's dependency tree; a second SQLAlchemy database alongside yours;
~600 stars under single-company stewardship; and plain helper functions not being
hashed, so editing a helper invalidates nothing.

Its lazy-expression model solves dynamic graph shape, which phase B does not have.

**If the dependency weight were not a concern, would it be the pick?** No, but it
would be a genuine second place ahead of snakemake. Setting aside `boto3` and
`textual`, two objections remain and neither is about dependencies:

- **The second database.** redun keeps its own SQLAlchemy call graph. For a
  package whose stated purpose is that everything lands in *your* database, that
  is an architectural conflict rather than an inconvenience, and it does not go
  away with a lighter install.
- **Helper functions are not hashed.** Only task functions are. Under the
  [granularity rule](#the-step-granularity-rule), steps are deliberately coarse
  and call many helpers, so most real code changes would live in unhashed
  helpers and silently fail to invalidate. The two designs work against each
  other.

What redun does better than pytask is output validity checking and provenance
depth. If the project were starting from nothing, with no existing database and
no preference against AWS, it would be a close call. Given an existing SQLModel
schema that is the whole point of the package, it is not.

### pydoit

`file_dep`/`targets` hashing with a state database is the right model, and
load-time task creation is not a problem for a static DAG. Rejected on the
ergonomics you already found: dict-based task specs resist typing and IDE
support. pytask is what `doit` would look like if designed today.

### Luigi

Dynamic dependencies work and `Target` is a good serialisation abstraction, but
**invalidation is existence-only** — `complete()` defaults to "do my outputs
exist". Change the code or an input and Luigi skips happily. Overriding
`complete()` on every task means writing the invalidation layer yourself inside a
framework that also imposes class-per-task and a central scheduler.

### make

mtime-only invalidation is actively wrong for files restored from backup or
copied between filesystems, the syntax objection stands, and it needs targets
known before it starts. snakemake is strictly better on every axis.

### Parsl

Not a caching solution, but the strongest pure executor surveyed: a library with
no server or daemon, whose `Config` combines executors with providers for SLURM,
PBS, Torque, HTCondor, Cobalt, LSF, Kubernetes, AWS and GCP. NSF-funded academic
infrastructure, actively released (2026.8.10), no commercial entity.

Relevant as an alternative to Dask behind `pytask-parallel` if the Dask worker
model proves awkward — it can be registered as a custom executor the same way.

### Others considered and rejected quickly

- **Apache Hamilton** — good caching (SQLite metadata store, separate result
  store, `data_version` propagated instead of data), but the graph is defined by
  function and parameter names and the model is memoisation.
- **Airflow** — no meaningful caching model.
- **Flyte** — explicit `cache_version`, strongly typed, good at remote dispatch,
  but Kubernetes is mandatory.
- **Metaflow** — content-addressed artifacts and `resume`, but no "skip if
  unchanged across runs" semantics.
- **Kedro** — `AbstractDataset` is a good materialisation answer; no caching.
- **Nextflow** — outstanding resume, genuinely good at this scale, but its
  workflows are written in Groovy, a JVM language. That means a Java runtime in
  the stack and pipeline logic your team cannot read or test with Python tooling;
  Python appears only inside individual process bodies.
- **Ploomber** — archived (May 2025).
- **Covalent** — designed for exactly this dispatch pattern (write locally,
  dispatch to heterogeneous backends). Built by Agnostiq, a Toronto quantum/HPC
  startup, and **acquired by DataRobot in February 2025** to fold its compute
  orchestration into their agentic-AI platform. Development did not stop but did
  change direction: `0.240.0` (May 2025) is the last stable release, with a
  single `0.241.0rc0` in April 2026 and no stable follow-up. The last stable is
  usable in the sense that it installs and runs, but a year without a stable
  release under an acquirer with different priorities makes it a poor foundation
  for infrastructure meant to last.
- **joblib.Memory / diskcache** — memoisation at 1% of Prefect's features.
- **Climate-REF** — worth a conversation with Jared given PR9, but it is an
  evaluation-diagnostics orchestrator, not a caching layer.

### Rolling your own

Not recommended, and the reason is worth stating because it was a live option.

The strongest argument for building was that trusting recorded ESGF checksums —
invalidating on a database lookup rather than hashing terabytes — was not
available off the shelf. It is: pytask's `state()`, snakemake's `params` and
Dagster's `@observable_source_asset` all accept your own notion of "has this
changed". Once the cheapest correct invalidation strategy is available by
configuration, building an engine to get it is hard to justify.

What you *do* build is the materialisation layer — see below — which is the
small, stateless part. The scheduler, the state database, the parallel execution and the
dry-run/visualisation machinery are not worth reimplementing.

## The materialisation layer

The one component to write yourself: a materialiser per data format, plus a
single generic binding to whatever DAG tool you use. This is not a caching
system — it never decides whether to run anything. It only answers "how does this
value become bytes, how does it come back, and what is its state?".

### Shape

Define **your** protocol first, and have pytask nodes delegate to it. The
dependency direction matters: if your materialisers *are* `PNode`s, pytask has
leaked into the layer whose whole purpose is to be portable.

```python
class Materialiser(Protocol):
    """How a value becomes bytes, comes back, and reports its state."""

    def save(self, value: Any, path: Path) -> None: ...
    def load(self, path: Path) -> Any: ...
    def state(self, path: Path) -> str | None: ...


class NetCDF(Materialiser):
    def save(self, value: xr.Dataset, path: Path) -> None:
        tmp = path.with_suffix(".tmp")
        value.to_netcdf(tmp)
        tmp.rename(path)  # atomic

    def load(self, path: Path) -> xr.Dataset:
        return xr.open_dataset(path)  # lazy — see below

    def state(self, path: Path) -> str | None:
        return str(path.stat().st_mtime) if path.exists() else None
```

An `ESGFFile` materialiser overrides `state()` to return the recorded checksum
from your database instead of an mtime — no I/O, correct, and it works for files
not yet downloaded.

The pytask binding is then a single generic adapter, written once for all
formats:

```python
@dataclass
class MaterialiserNode(PNode):
    name: str
    path: Path
    materialiser: Materialiser

    def state(self) -> str | None:
        return self.materialiser.state(self.path)

    def load(self, is_product: bool) -> Any:
        return self if is_product else self.materialiser.load(self.path)

    def save(self, value: Any) -> None:
        self.materialiser.save(value, self.path)
```

For the large-output case, a step takes a plain `PathNode`, writes its own file
and returns nothing — no materialiser involved.

### What to be careful about

- **Atomic writes.** A killed step must not leave a half-written file that looks
  present. Write to a temporary path and rename on success, in a base class, so
  no individual materialiser can get it wrong.
- **Lazy loading.** `xr.open_dataset` is lazy; `xr.load_dataset` is not. Decide
  deliberately, because getting it wrong turns "fits in memory" into "does not".
- **Do not put invalidation logic in `load`/`save`.** The temptation to add "skip
  if the file already exists" will be strong. That is pytask's job, and
  duplicating it produces two caches that disagree.

### Why this layer is worth owning

It is the seam that makes the DAG tool replaceable. Because the materialisers
know nothing about pytask, porting means writing one new binding — a snakemake
dispatch function or a Dagster IO manager — while every format implementation
stays untouched. Steps written as Dagster assets port nowhere. It is also the
natural user-extension point, matching your preference for injection where it
makes sense: users add a node for a format you did not anticipate and touch
nothing else.

## Config and the process boundary

Two questions arise for snakemake in particular: whether it requires a CLI or
script interface per step, and whether long-lived non-serialisable config makes
that painful. The first turns out not to be a problem; the second is real, and is
the strongest technical argument for pytask.

### snakemake needs neither a CLI nor a script per step

You can call plain Python functions directly, via the `run:` directive:

```python
rule anomaly:
    input:  "raw/{ds}.nc"
    output: "anom/{ds}.nc"
    params: step="anomaly"
    run:
        run_step(REGISTRY[params.step], input, output)
```

`run:` has access to everything in the Snakefile, including imported modules, so
your step functions stay plain functions and there is no argument parsing
anywhere.

The alternative is `script:`, which points at a Python file and injects a
`snakemake` object carrying `input`, `output`, `params`, `wildcards`, `config`,
`threads` and `log`. Even then it is **one** generic dispatch script for the
whole project, not one per step:

```python
# scripts/_run_step.py — the only script in the repo
from esmporium.steps import REGISTRY

step = REGISTRY[snakemake.params.step_name]
step.run_from_paths(dict(snakemake.input), dict(snakemake.output))
```

Two real limitations of `run:` are worth knowing, because they push you towards
`script:` in practice:

- **`conda:` is disallowed with `run:`.** A run block has access to Snakefile
  globals and therefore must execute in Snakemake's own process, so it shares
  Snakemake's environment. snakemake rejects the combination outright rather than
  letting it silently mislead you.
- **`container:` only affects shell calls made from inside the block**, not the
  Python code itself. So a run block cannot be containerised.

snakemake's own docs also advise keeping `run:` to a few lines and using
`script:` beyond that.

So the cost is not "a script per step". It is that **every rule still needs a
DSL stanza** declaring inputs, outputs and params, alongside the function that
already declares them.

Two honest points about that duplication, in opposite directions:

- **It is not a drift risk if the Snakefile is generated.** With the decorator as
  the single source of truth and the Snakefile emitted from it, the stanza is a
  compilation artifact. Keeping them in sync is the generator's job, not a
  human's, so they cannot diverge through forgetfulness.
- **But you still debug in the generated artifact.** When a rule misbehaves you
  read a Snakefile you did not write, reason about wildcards you did not choose,
  and map it back to the function you did write. That tax is paid on every
  confusing failure, forever, and no amount of generation removes it.

Combined with the DSL itself, this is a sufficient reason to prefer pytask,
where the function *is* the declaration and there is nothing generated to read.

### The config concern is real

Every snakemake rule runs in a **fresh process that re-parses the Snakefile**.
Anything a step needs must therefore be either YAML-serialisable through
`config`/`params`, or reconstructable from module-level code in the Snakefile. A
live database session, an authenticated HTTP client, or a registry of fix
functions with closures cannot be shipped — you pass a *description* and rebuild
the object on the other side.

### But this is a distributed-execution tax, not a snakemake tax

Any tool that submits work to another node hits this. It is why Dagster has
resources and Prefect has blocks: both ship serialisable descriptions and
reconstruct live objects where the work runs.

The difference is **when you pay**:

| | Local runs | Distributed runs |
| --- | --- | --- |
| snakemake | Always — every rule is a fresh process | Always |
| pytask (threads / sequential) | Never — one process, objects passed directly | n/a |
| pytask (processes) | Only what crosses the boundary | Only what crosses the boundary |
| pytask (Dask, `remote=True`) | n/a | Always |

That is the honest crux of your question. **snakemake makes you pay the
serialisation tax unconditionally; pytask makes you pay it only for what actually
crosses a process boundary, and not at all when running in-process.** Given that
threads are perfectly adequate for `xarray` and `numpy` work — both release the
GIL for the operations that matter — a large fraction of your pipeline can run
with rich config passed directly.

### The pattern to adopt regardless

Even choosing pytask, make config a **serialisable description plus a cached
factory**:

```python
@lru_cache(maxsize=None)
def get_client(cfg: ClientConfig) -> AuthenticatedClient:
    return AuthenticatedClient(**asdict(cfg))
```

Steps take the frozen dataclass, not the client. This costs nothing in-process,
makes steps trivially testable, and means moving to processes or a cluster later
is a configuration change rather than a refactor. It is the discipline that keeps
the door open.

`ClientConfig` is the frozen dataclass here — the small, hashable description —
while `AuthenticatedClient` is the live object that must never appear in a step
signature. The `@lru_cache` means each process builds the client at most once, so
the indirection costs one dictionary lookup per call.

**Enforcing it** is worth doing mechanically, because the failure is silent until
the day work moves off-process. Three checks, in descending order of value and
ascending order of cost — the first is the one that matters:

**1. Reject bad signatures at registration.** Free, runs at import, needs no CI.

```python
def _validate_signature(fn: Callable[..., Any]) -> None:
    for name, param in inspect.signature(fn).parameters.items():
        ann = param.annotation
        if _is_artifact(ann) or _is_frozen_dataclass(ann):
            continue
        raise StepDefinitionError(
            f"{fn.__module__}.{fn.__qualname__} parameter {name!r} is annotated "
            f"{ann!r}. Step parameters must be an Artifact subclass or a frozen "
            f"dataclass. Live objects (clients, sessions, connections) cannot "
            f"cross a process boundary — pass a frozen config and build the "
            f"object inside the step with an lru_cache'd factory."
        )
```

This is genuine enforcement in code, not documentation. It fires the moment
someone writes a bad step, names the offending parameter, and states the fix.
**An error message at the point of the mistake teaches better than any
document**, so it is worth investing in the wording rather than treating it as a
guard clause.

**2. Pickle the registry in a unit test.** Milliseconds, executes nothing:

```python
def test_all_steps_are_picklable():
    for spec in REGISTRY.all_steps():
        pickle.dumps(spec.fn)
        for cfg_type in spec.config_types:
            pickle.dumps(cfg_type)
```

This catches unpicklable closures and module-level state that signature
inspection cannot see, without running a pipeline.

**3. One two-step integration test under a `ProcessPoolExecutor`.** Seconds, on
tiny synthetic data. It proves the wiring — that a step really does survive a
process boundary — not the pipeline.

**Running the full pipeline in CI is not required and is not suggested.** Checks
1 and 2 do nearly all the work at nearly no cost, and check 1 in particular means
the discipline is enforced by the code rather than resting on people having read
the docs. Documentation should still explain *why* — the reasoning about process
boundaries does not fit in an exception message — but it is the backstop, not the
mechanism.

## Per-job submission without snakemake

The main execution argument for snakemake is that it submits **one cluster job
per rule**, so each step gets right-sized resources and failures are isolated,
whereas `dask-jobqueue` holds long-lived allocations with workers that tasks are
dispatched to. That distinction matters for long, heterogeneous, failure-prone
work — a download needing 2 GB and a regrid needing 60 GB should not share a
worker sized for the larger.

**This does not require leaving pytask.** `submitit` is a
`concurrent.futures`-compatible executor in which *each submitted task becomes
its own SLURM job*, batched into job arrays because schedulers handle those
better than many individual submissions. Since `pytask-parallel` accepts any
registered `Executor`, it plugs directly into
[seam 2](#seam-2--concurrentfuturesexecutor-for-swapping-dask-jobqueue):

```python
import submitit
from pytask_parallel import ParallelBackend, WorkerType, registry

def build_slurm_executor(n_workers: int) -> Executor:
    ex = submitit.AutoExecutor(folder="logs/%j")
    ex.update_parameters(slurm_partition="compute", timeout_min=240, mem_gb=64)
    return ex

registry.register_parallel_backend(
    ParallelBackend.CUSTOM, build_slurm_executor,
    worker_type=WorkerType.PROCESSES, remote=False,
)
```

It is maintained by Meta, ~1,600 stars, last release December 2025, last commit
January 2026 — smaller than snakemake's ecosystem but healthy, and doing one
narrow thing.

### Per-task resources are achievable too

A single `submitit` executor does apply one resource profile to everything
submitted through it. But nothing requires you to use a single executor.

Reading `pytask_parallel/execute.py`, tasks are submitted as:

```python
session.config["_parallel_executor"].submit(
    wrap_task_in_process,
    task=task,                    # <- the PTask itself, as a keyword argument
    ...
)
```

**The task object is therefore visible to any custom executor**, which makes a
dispatching façade straightforward:

```python
class ResourceAwareExecutor(Executor):
    """Route each task to a submitit executor matching its resource profile."""

    def __init__(self, defaults: Resources) -> None:
        self._defaults = defaults
        self._by_profile: dict[Resources, submitit.AutoExecutor] = {}

    def submit(self, fn, /, *args, **kwargs):
        task = kwargs.get("task")
        resources = REGISTRY.resources_for(task) if task else self._defaults
        return self._executor_for(resources).submit(fn, *args, **kwargs)

    def _executor_for(self, resources: Resources) -> submitit.AutoExecutor:
        if resources not in self._by_profile:
            ex = submitit.AutoExecutor(folder=f"logs/{resources.slug}/%j")
            ex.update_parameters(**resources.as_submitit_kwargs())
            self._by_profile[resources] = ex
        return self._by_profile[resources]
```

Roughly thirty lines. Each distinct profile gets its own SLURM job array, which
is the arrangement schedulers prefer anyway. Because the `@step` decorator
already attaches `Resources` and generates the task, looking the profile up by
task name in your own registry needs nothing from pytask.

Three caveats worth knowing:

- **`task=` is an internal detail.** Custom executors are a documented extension
  point, but the keyword name is not a stable public contract. Read it
  defensively (`kwargs.get("task")`) and fall back to defaults rather than
  raising, so a pytask-parallel upgrade degrades to "everything runs with default
  resources" instead of breaking the run.
- **`n_workers` semantics blur.** pytask throttles submission by `n_workers`,
  which now spans several underlying executors. Set it high and let SLURM do the
  actual scheduling, otherwise pytask's own limit becomes the bottleneck.
- **Long queue waits mean many pending futures** held by pytask's submission
  loop. Fine in practice, but it is the thing to watch first if the scheduler is
  busy.

So per-step resource specification does **not** force a move to snakemake. What
snakemake still gives is the same behaviour with no bespoke code and a
battle-tested implementation — a maintenance argument rather than a capability
one.

## When to switch to snakemake

With `submitit` and a resource-aware executor façade, there is no longer a
*capability* that forces the move. The remaining triggers are about maintenance
and risk:

1. **The bespoke executor becomes a burden.** If the façade above grows past a
   page, starts special-casing scheduler quirks, or breaks on a pytask-parallel
   upgrade, you are maintaining cluster-submission infrastructure — which is
   exactly what snakemake gives away for free and does better.
2. **pytask's bus factor bites.** One primary maintainer. If development stalls
   or a Python release breaks it, the migration should be a planned move rather
   than an emergency.
3. **Collaborators need to run the pipeline.** snakemake is the field standard;
   a workflow expressed in it is legible to a much larger pool of climate and
   computational scientists.

If everything runs on one large machine — which "the CMIP6 server" in `PLAN.md`
suggests is the near-term reality — none of this arises.

Switching means accepting a double-declaration pattern: the generated Snakefile
restates what the decorators already say. That is a real cost, and it should be
paid only for a real reason, which is why the triggers above are specific rather
than a general hedge.

Two things make the switch cheap if it comes:

1. **Step bodies are plain functions**, so the science does not move.
2. **The materialisers are untouched.** You replace one binding class with a
   snakemake dispatch function: `load`/`save` become its deserialise/serialise,
   and `state()` becomes a `params` entry. Mechanical, not a rewrite.

The DAG generator that reads your database and emits `StepSpec`s is reusable
either way, which is why it is worth writing early.

## Portability: what to adapt, and what not to

Adapters are worth building here, but only at two specific seams, and both have
limits worth knowing before you rely on them.

The single biggest portability lever is not an adapter at all: **step bodies are
plain Python functions with no framework imports.** That is where the thousands
of lines will be, and it is already portable by construction. Everything below is
second-order.

By "framework imports" is meant anything from the DAG tool appearing inside a
step body or signature: no `from pytask import ...`, no `snakemake.input`, no
Dagster `context` parameter, no decorator from any of them beyond your own
`@step`. A step should import `xarray`, `numpy`, your own helpers, and nothing
else. The test is whether the function can be called directly from a test or a
notebook with ordinary arguments and no runner present — if it can, it ports
anywhere; if it cannot, it is welded to whichever tool it mentions.

### Seam 1 — a step IR, for swapping pytask, snakemake or Dagster

You need a DAG generator regardless: something that reads the database and
produces the task graph. Make its output an intermediate representation rather
than pytask objects directly. The extra cost is close to zero, and it is what
makes the backend swappable.

**Nobody writes a `StepSpec` by hand** — see
[Single source of truth](#single-source-of-truth-the-decorator-is-the-only-declaration).
It is derived from the decorated function plus a dataset identity:

```python
@dataclass(frozen=True)
class StepSpec:
    name: str
    version: str
    fn: Callable[..., Any]
    inputs: dict[str, ArtifactRef]   # resolved from the signature's types
    outputs: dict[str, ArtifactRef]  # resolved from the return type
    params: dict[str, str]           # serialisable; the checksum lives here
    resources: Resources             # from the decorator
```

The generator emits `list[StepSpec]`. A backend turns that into pytask tasks, or
into a Snakefile, or into Dagster assets. Four layers, each independently
portable:

| Layer | Portable? | Why |
| --- | --- | --- |
| Step functions | Fully | No framework imports |
| Artifact types + materialisers | Fully | Your protocol; pytask nodes delegate *to* it, not the reverse |
| `@step` decorator + registry | Fully | Plain metadata on plain functions |
| `StepSpec` IR | Fully | A dumb derived dataclass |
| Backend | Not at all | Deliberately — this is the throwaway part |

The direction of the dependency matters. Define your own materialiser protocol
and have thin `PNode` subclasses call into it. If instead your materialisers
*are* `PNode`s, pytask has leaked into the layer whose purpose is to be portable.

**The limit:** do not try to abstract execution semantics. Retries, resource
requests, per-job submission and partial-failure handling differ fundamentally
between backends, and a unified model over them is a workflow engine's
abstraction layer that you now maintain without having a workflow engine. Keep
`Resources` a small declarative record that each backend interprets its own way,
and accept that some backends will ignore parts of it.

### Seam 2 — `concurrent.futures.Executor`, for swapping dask-jobqueue

This one is nearly free, because `Executor` is a stdlib interface that
`pytask-parallel`, Dask and Parsl all already target. `pytask-parallel`'s
registration takes exactly an `(n_workers) -> Executor` builder, so a factory
plugs straight in:

```python
def make_executor(cfg: ExecutorConfig) -> Executor:
    match cfg.kind:
        case "threads":   return ThreadPoolExecutor(cfg.n_workers)
        case "processes": return ProcessPoolExecutor(cfg.n_workers)
        case "dask":      return Client(SLURMCluster(**cfg.cluster)).get_executor()
```

Swapping `dask-jobqueue` for Parsl, or for plain processes, is then a config
change.

**The limit, and it is a sharp one:** `Executor` has no notion of per-task
memory, cores or walltime. It is `submit(fn, *args)` and nothing more. So the
adapter holds perfectly for homogeneous work and breaks down at exactly the
heterogeneous-cluster scenario described in
[When to switch to snakemake](#when-to-switch-to-snakemake) — where a download
needs 2 GB and a regrid needs 60 GB, and you want the scheduler to know that.

Be clear-eyed about what this buys: the adapter makes the **code** port cheap. It
does not make the **execution model** port cheap.

"Changing tools" here means replacing the whole DAG engine — moving from pytask
to snakemake — not swapping an executor behind pytask. The distinction matters
because the two have very different costs. Swapping an executor is a config
change. Changing the engine means writing a new backend for `StepSpec` and
accepting the generated-Snakefile tax, while step bodies and materialisers stay
put. `StepSpec.resources` is what carries the per-step resource information
across that boundary, which is why it is worth populating even while the
executor in use ignores it.

Note that the heterogeneous-resource problem does not force this: `submitit`
plus a resource-aware executor façade gives per-job submission *and* per-step
resources without leaving pytask — see
[Per-job submission without snakemake](#per-job-submission-without-snakemake).

### What I would not build

- **A unified retry/failure abstraction.** Every tool does this differently and
  well. Use the backend's.
- **A backend-agnostic logging or progress layer.** Same reason.
- **An adapter for a third backend you have not needed yet.** Two is enough to
  prove the seam is real; three is speculative. Write the pytask backend, and
  write the snakemake one only if and when step 3 or 4 of the plan goes badly.

## Remote execution and lock-in

### Three kinds of lock-in, worth separating

1. **Code lock-in** — how much of your source is written in someone's idioms.
   The expensive kind; leaving means rewriting.
2. **Operational lock-in** — what must be running for anything to work. The kind
   that makes users hate installing your package.
3. **Commercial lock-in** — what is only available if you pay. Usually least
   severe, because it is most visible.

| Tool | Code | Operational | Commercial |
| --- | --- | --- | --- |
| pytask | Low — plain functions plus annotations | Nothing | **None** |
| Parsl | Low — decorate a function, config separate | Nothing | None |
| snakemake | Moderate — a DSL, step bodies stay plain | Nothing persistent | None |
| redun | Moderate — `@task` | A database | None, but AWS SDK is a hard dep |
| Prefect | High — everything is `@task`/`@flow` | API server + Postgres + a worker per environment | Moderate |
| Dagster | High — assets, IO managers, resources | Code server + daemon + UI | Moderate |
| Flyte | High — typed tasks | Kubernetes, mandatory | Low |

### On Prefect specifically

Prefect is often assumed to make non-Prefect infrastructure difficult. That is
half right, and the mechanism is worth knowing.

**OSS can reach non-Prefect infrastructure.** A self-hosted server supports work
pool types for Process, Docker, Kubernetes, AWS ECS, Azure Container Instances,
Google Cloud Run and Google Vertex AI. Dispatching to your own cloud with only
open-source components is genuinely supported.

**But the paid path is deliberately much easier.** Cloud-only is precisely what
removes the operational pain: **push work pools** (Cloud Run, ECS, ACI, Modal —
serverless dispatch with *no worker process to run*), **managed execution**, plus
events, automations, SSO and RBAC. On OSS you run a Postgres-backed API server
and a long-lived worker inside every target environment. A convenience gradient
rather than a wall, but a real one: the paid path is meaningfully less work.

One hard gap matters more than the licensing question: **there is no SLURM or PBS
work pool.** Prefect's infrastructure types are all container-shaped. snakemake,
Parsl and Dask all reach SLURM directly.

### Keep the database authoritative

Whatever you adopt keeps its own completion metadata. For a package whose thesis
is that everything goes in the database, that is a genuine tension.

The mitigation is a convention worth adopting from the first step: **every step
writes its own provenance to your database as part of running.** The tool's state
then becomes a scheduling cache — disposable, rebuildable, never consulted for
truth — and your database remains the record. Delete it and you lose scheduling
state and nothing else.

This also keeps the tool choice reversible, which for a project at this stage is
worth more than picking optimally.

## Comparison with Climate-REF

Jared's team solved an adjacent problem, and it is worth knowing where the
designs agree, where they differ, and what to take.

### How Climate-REF works

Read from `climate_ref.solver`, `climate_ref.models.execution` and
`climate_ref_core.datasets`:

1. **Ingest** datasets into a catalog held in a SQLAlchemy database.
2. **Solve.** Each `Diagnostic` declares `data_requirements` — filters and
   group-by fields over the catalog. The solver applies them and produces
   `ExecutionGroup`s, each holding an `ExecutionDatasetCollection`.
3. **Decide.** `ExecutionGroup.should_run(dataset_hash, ...)` returns true if
   there is no previous execution, the dataset hash differs from the last run, or
   the group is marked `dirty`. It returns false if an execution with the same
   hash is already in progress, or the last execution failed and the group is not
   dirty.
4. **Execute** through an `Executor` protocol, chosen by fully-qualified name in
   config via `import_executor_cls`. Implementations: synchronous, local
   multiprocessing, HPC (PBS and SLURM), and Celery.
5. **Record.** Results are CMEC output bundles on disk, ingested back into the
   database as `ExecutionOutput` and `MetricValue` rows.

The invalidation signals are `dataset_hash`, `diagnostic_version` (read from the
`Diagnostic.version` class attribute at solve time), and the `dirty` flag.

### Where the designs agree

The convergence is reassuring, because it was arrived at independently:

| Principle | Climate-REF | This plan |
| --- | --- | --- |
| Explicit version on the unit of work | `Diagnostic.version` class attribute | `@step(version=...)` |
| Never hash file contents | `stable_hash` over sorted dataset *slugs* | `state()` returns the recorded ESGF checksum |
| Database is the source of truth | Execution state lives in the DB | Steps write provenance to your DB |
| Pluggable executor behind a narrow protocol | `import_executor_cls(fqn)` | `make_executor(cfg) -> Executor` |
| Work set derived, not enumerated | `DataRequirement` filters + group-by | DAG generator over artifact types |
| Coarse units of work | One diagnostic run is the atom | The step granularity rule |

The hashing agreement is the most striking. Climate-REF hashes **dataset
identifiers**, not bytes — a SHA1 over sorted slug values. That is the same
conclusion this plan reaches from the ESGF-immutability argument: at this data
volume, identity from the database is the only affordable fingerprint.

### Where the problems differ

**Climate-REF has no DAG.** This is the structural difference, and it matters
more than anything else. Diagnostics are independent leaves: datasets in, output
bundle out. Nothing consumes another diagnostic's output, so there are no edges,
no cascade invalidation, and no intermediate artifacts.

That explains several absences which are *not* oversights on their part:

- **No materialisation layer**, because outputs are CMEC bundles read by humans
  and websites, never fed to a downstream computation. The in-memory `xarray`
  return problem simply does not arise.
- **No staleness propagation**, because there is no upstream to go stale.
- **Diagnostics mostly shell out** to ESMValTool, PMP and ILAMB via
  `CommandLineDiagnostic`, so custom Python serialisation is a non-problem.

#### The cost of no DAG: shared work is repeated

This is the most important practical consequence, and it is worth stating
plainly because it is the clearest justification for building a DAG at all.

**In Climate-REF, nothing consumes another diagnostic's output.** Every execution
starts from raw datasets and computes everything it needs from scratch. So any
intermediate result shared between executions is recomputed once per execution,
however expensive it was.

The concrete case: computing a Gregory regression across multiple ensemble
members, all of which share a parent dataset. The expensive part is the
global mean of that parent. With independent executions you compute it **once per
ensemble member**. At 40 members, that is 40× more compute than the problem
requires, and the waste scales linearly with ensemble size — precisely the
direction CMIP is moving.

The general form: **where a DAG deduplicates shared sub-results, independent
executions multiply them by the fan-out.** The larger the shared expensive
prefix, and the wider the fan-out over it, the worse the ratio.

Two caveats, in fairness:

- Within a single execution, providers do some of their own optimisation.
  ESMValTool writes preprocessor output to disk between the preprocessing and
  diagnostic phases (required anyway, since diagnostics may be written in other
  languages), so within one recipe run, two diagnostics needing the same
  preprocessed variable share it. Across runs there is `--resume_from`, which
  points at a previous run's output directory and reuses preprocessing tasks that
  completed successfully, identified by a `metadata.yml` in their output
  directory.

  Two things to note about that, because it is weaker than it sounds. It is
  **explicit and opt-in**, not content-addressed: nothing checks that the inputs
  or the code are unchanged, so `--resume_from` is a human assertion that reuse
  is safe rather than a verified fact. And it is scoped to a *recipe run*. In the
  Gregory case, if each ensemble member is solved into its own execution group,
  each becomes its own recipe run, so the shared parent global mean is
  preprocessed once per member regardless. Sharing within a run does not rescue
  the fan-out; only a graph across runs would.
- Whether this is inherent to the REF or merely how it is built today is a
  question for Jared. **If independent diagnostic execution is a hard
  architectural requirement** — and there are respectable reasons it might be,
  such as running third-party providers as black boxes, or reproducibility of a
  single execution in isolation — **then this cost can never be addressed and
  will always be paid.**

For esmporium this is not a hypothetical: PR6's fast path and PR9's Gregory
calculations are the same shape. It is the reason phase B needs a dependency
graph rather than a list of independent jobs, and it is what the artifact-type
design buys you almost for free:

```python
@step(version="1", resources=Resources(memory="60GB"))
def parent_global_mean(parent: ParentDataset) -> ParentGlobalMean:
    ...

@step(version="1")
def gregory(member: MemberDataset, parent_gm: ParentGlobalMean) -> GregoryFit:
    ...
```

`ParentGlobalMean` has one producer and forty consumers. The graph computes it
once, caches it, and every member reads it. No special-casing, no manual
memoisation — deduplication is what a DAG *is*, and it falls out of declaring the
shared intermediate as an artifact type with its own identity.

This is also the strongest argument for the
[step granularity rule](#the-step-granularity-rule) cutting the other way than
you might expect: `parent_global_mean` is a small computation, but it earns its
place as a step because it is *shared*, not because it is expensive in
isolation. **Shared-and-cheap can beat unshared-and-expensive.** The test is
total work saved across all consumers, not cost per invocation.

esmporium's phase B is a genuine chain — download, process, derive — so the
hardest parts of this plan are precisely the parts Climate-REF did not need to
solve. **Their design cannot be copied wholesale**, and the fact that they rolled
their own solver is not evidence that you should: what they built is dataset
*selection*, which no DAG tool provides, while your problem is dependency
*resolution*, which is exactly what DAG tools do provide. These are different
things, and you will need both.

### How complicated is the selection logic, really?

Not very. The mechanism is about fifty lines; the cost is entirely in domain
knowledge.

`extract_covered_datasets` in `solver.py` is, stripped of logging and
finalisation:

```python
subset = requirement.apply_filters(catalog_df)          # pandas filtering
groups = subset.groupby(list(requirement.group_by))     # pandas groupby

for name, group in groups:
    for constraint in requirement.constraints or []:
        group = constraint.apply(group, catalog_df)
        if group.empty:
            break                                        # drop this group
    else:
        results[tuple(zip(requirement.group_by, name))] = group
```

That is the whole solver. A constraint is a one-method protocol:

```python
class GroupConstraint(Protocol):
    def apply(self, group: pd.DataFrame, data_catalog: pd.DataFrame) -> pd.DataFrame: ...
```

and returning an empty frame means "this group is not viable, drop it". There is
no solving in the constraint-satisfaction sense — no search, no backtracking, no
optimisation. It is a filter, a `groupby`, and a fold over a list of validators.

**Where the 693 lines of `constraints.py` actually go** is eight concrete
implementations, each individually simple:

| Constraint | What it does |
| --- | --- |
| `RequireFacets` | Group must contain given facet values |
| `IgnoreFacets` | Drop matching rows |
| `RequireTimerange` | Group must cover a period |
| `RequireContiguousTimerange` | No gaps between files |
| `RequireOverlappingTimerange` | Multiple variables must overlap |
| `AddSupplementaryDataset` | Pull in extra rows from the full catalog (cell measures) |
| `AddParentDataset` | Pull in the parent experiment |
| `SelectFirstMember` | Pick one variant, with a sort key for `r1i1p1f1` ordering |

That is climate-data knowledge, not software complexity. You will write your own
versions of several of these no matter which tool you pick — note that
`AddParentDataset` is PR6's ancestry problem and `AddSupplementaryDataset` is the
`areacella` problem.

**The genuinely tricky part is none of the above.** It is `finalise`: the catalog
has columns that can only be filled by reading netCDF headers — `calendar`, time
ranges — so they are lazy. The solver finalises after filtering, and then
*again* after any constraint that adds rows from the wider catalog. Their own
comment records why:

> Without this, columns like `calendar` remain NaN and downstream constraints
> such as `RequireContiguousTimerange` silently skip checks.

So constraint ordering matters, constraints that add rows invalidate earlier
finalisation, and getting it wrong **fails silently** — checks pass because the
data they need is `NaN`. That is the part worth reading their code for, and the
part to be careful about in your own version.

**Estimate for esmporium:** the framework is a day. A first useful set of
constraints is a week. The lazy-metadata interaction is where the bugs will be,
and it is worth designing so that a constraint which needs an unfinalised column
raises rather than silently passing — Climate-REF added
`_validate_requirement_columns` for exactly the `group_by` case, and the same
principle should extend to constraints.

**None of this depends on the DAG tool.** Selection lives in phase A and in the
DAG generator, and is identical whether you use pytask, snakemake or anything
else. That is worth noticing, because it means this — the part with real domain
content — is not at risk from the tool decision, which lowers the stakes of that
decision further.

### What to take from it

Four things Climate-REF handles that this plan currently does not, all worth
adopting:

1. **An explicit `dirty` flag with a manual override.** Climate-REF exposes
   `flag-dirty` as a CLI command, so a human can force a re-run without faking a
   version bump. Version bumps are for code changes; `dirty` is for "I have a
   reason you cannot express". Add a `dirty` column and a way to set it.
2. **Failure as a recorded state, not an absence.** If the last execution failed
   and the group is not dirty, Climate-REF does *not* retry — you pass
   `rerun_failed=True`. This plan implicitly re-runs anything lacking a valid
   output, which means a deterministically-failing step retries forever, burning
   cluster time on every invocation. Record failures and require an explicit
   opt-in to retry them.
3. **In-progress and stale execution handling.** Climate-REF records executions
   as in-progress with their hash, skips a group if one is already running with
   the same hash, and reaps abandoned ones older than a `stale_cutoff` via
   `fail_stale_in_progress_executions`. This is a concrete answer to the
   concurrency risk this document flags but does not otherwise solve — two
   processes deciding to produce the same 80 GB output simultaneously.
4. **Recorded resource usage.** They added an `execution_resource_usage` table.
   This pairs directly with the `Resources` decorator: declare what you think a
   step needs, measure what it actually used, and refine. Without measurement,
   declared resources drift into folklore.

Items 2 and 3 are the important ones. They are the failure modes that only show
up in production, which is exactly the kind of thing worth taking from a project
that has already been there.

### Worth a conversation

PR9 already flags REF integration. Two questions worth putting to Jared:

- Whether the `dirty` flag has proved sufficient in practice, or whether they
  wish they had finer-grained invalidation.
- How the HPC executor has held up, given this plan's open question about
  per-job submission versus long-lived workers. They have `pbs_scheduler.py`
  and `slurm.py`, so they have direct experience of the model that
  `dask-jobqueue` would give you.

## What I would do next

1. **Write the artifact types and materialisers first.** The `Materialiser`
   protocol, a `NetCDF` implementation, an `ESGFFile` whose `state()` reads your
   database, and atomic writes in a shared base. No DAG involved. This is the
   piece you keep whichever tool wins.
2. **Write the `@step` decorator and registry.** Signature introspection to
   resolve artifact types, one-producer-per-type enforcement at import, and
   `Resources` metadata. This is the single source of truth everything else
   derives from, so it is worth getting the shape right before there are many
   steps depending on it.
3. **Write the DAG generator**: read the database, emit `list[StepSpec]`, then a
   pytask backend that turns those into tasks. Test the generator by asserting on
   the specs — fast, no execution, no I/O.
4. **Prototype one large-output step** on PR8's downloads, using the
   `writes_own_output` path and the `ESGFFile` materialiser. Obvious artifacts, a
   recorded checksum, a clear "already have it" question.
5. **Confirm the `state()` trick works**: mutate a recorded checksum in the
   database and check that exactly one task re-runs.
6. **Prototype one in-memory-return step**, so both materialisation modes are
   exercised before you commit.
7. **Apply the granularity rule deliberately** when you get to PR9's Gregory and
   pattern-scaling work — that is where the temptation to make every logical
   operation a step will be strongest, and where the cost of giving in is
   highest.
8. **Add the Climate-REF safeguards** before running anything at scale: a
   `dirty` flag with a manual override, failures recorded as a state that is not
   silently retried, and in-progress records with a stale cutoff so two processes
   cannot race to produce the same output. See
   [Comparison with Climate-REF](#comparison-with-climate-ref).
9. **Only then** think about executors beyond processes.

Steps 1–6 are perhaps a week and de-risk the whole decision. Steps 1–3 are
backend-independent, so even in the unlikely event that pytask has to be
abandoned, none of that work is lost.

## Sources

- [pytask — writing custom nodes](https://pytask-dev.readthedocs.io/en/stable/how_to_guides/writing_custom_nodes.html)
- [pytask-parallel — custom executors](https://pytask-parallel.readthedocs.io/en/stable/custom_executors.html)
- [pytask-parallel — Dask backend](https://pytask-parallel.readthedocs.io/en/stable/dask.html)
- [dask-jobqueue](https://jobqueue.dask.org/)
- [submitit](https://github.com/facebookincubator/submitit)
- [ESMValCore — running and `--resume_from`](https://docs.esmvaltool.org/projects/ESMValCore/en/latest/quickstart/run.html)
- [Snakemake FAQ — rerun triggers and checksums](https://snakemake.readthedocs.io/en/stable/project_info/faq.html)
- [Snakemake — using executor plugins](https://snakemake.readthedocs.io/en/latest/executing/executors.html)
- [Snakemake plugin catalog](https://snakemake.github.io/snakemake-plugin-catalog/)
- [snakemake-executor-plugin-slurm](https://github.com/snakemake/snakemake-executor-plugin-slurm)
- [Dagster — asset versioning and caching](https://docs.dagster.io/guides/build/assets/asset-versioning-and-caching)
- [DVC — `dvc repro`](https://doc.dvc.org/command-reference/repro)
- [Prefect — manage results](https://docs.prefect.io/v3/develop/results)
- [Prefect — `cache_policies.py`](https://github.com/PrefectHQ/prefect/blob/main/src/prefect/cache_policies.py)
- [Prefect — work pools and workers](https://docs.prefect.io/v3/concepts/work-pools)
- [Prefect — Cloud vs OSS comparison](https://www.prefect.io/compare/prefect-oss)
- [redun — design overview](https://insitro.github.io/redun/design.html)
- [redun — `value.py`](https://github.com/insitro/redun/blob/main/redun/value.py)
- [Parsl](https://parsl.readthedocs.io/)
- [pydoit](https://pydoit.org/index.html)
- [Luigi](https://github.com/spotify/luigi)
- [Climate-REF](https://github.com/Climate-REF/climate-ref)
- [Climate-REF `solver.py`](https://github.com/Climate-REF/climate-ref/blob/main/packages/climate-ref/src/climate_ref/solver.py)
- [Climate-REF `models/execution.py`](https://github.com/Climate-REF/climate-ref/blob/main/packages/climate-ref/src/climate_ref/models/execution.py)
- [Climate-REF `core/datasets.py`](https://github.com/Climate-REF/climate-ref/blob/main/packages/climate-ref-core/src/climate_ref_core/datasets.py)
- [Climate-REF `core/executor.py`](https://github.com/Climate-REF/climate-ref/blob/main/packages/climate-ref-core/src/climate_ref_core/executor.py)
- [Rapid Evaluation Framework for the CMIP7 Assessment Fast Track (GMD 2026)](https://gmd.copernicus.org/articles/19/7415/2026/)
