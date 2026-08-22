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
   This is the Prefect property you value, without Prefect's caching model.
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
  Therefore `regrid` runs first. No edge is written anywhere.
- **Materialisers.** `RegriddedDataset` knows it is netCDF and how to
  save/load/state itself.
- **Paths.** Computed from the artifact type plus the dataset identity, by
  convention. No path is written by hand, ever.
- **Cache keys.** Version from the decorator, input state from the artifact
  types, params from the frozen config object.

Registration enforces **one producer per artifact type**, raising at import time
if two steps return the same type. That is the discipline that makes type-based
inference safe, and it turns a whole class of wiring mistakes into an immediate,
obvious error rather than a silent one.

This is static inference rather than Prefect's runtime inference, which is
better here: the entire graph can be validated before a single byte is read, and
it can be generated from a database query.

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

So `StepSpec` is a compilation artifact, like generated protobuf code. It
survives your objection because it was never a source of truth.

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
  fit than per-job submission for long, heterogeneous, failure-prone jobs. See
  [When to switch to snakemake](#when-to-switch-to-snakemake).
- **Less battle-tested at TB scale** than snakemake.

### snakemake

**The alternative, and the fallback.** Better engineered for scale than its
`make` heritage suggests.

In its favour:

- **Rerun triggers are not just mtime.** Defaults are `mtime`, `params`, `input`,
  `software-env` and `code` — a change to the rule's code, parameters, input set,
  or conda/container environment all force a re-run.
- **`params` gives you the checksum trick.** Put the ESGF checksum in a rule's
  `params` and invalidation becomes a database lookup that snakemake enforces.
- **Content hashing when you want it.** Above `--max-checksum-file-size` it
  checksums content; `--rerun-triggers mtime` gives the cheap mode. Your
  "cheap by default, strict opt-in" answer as flags.
- **The best cluster story in the survey.** Executor plugins for SLURM (actively
  maintained, 2.7.1 as of June 2026), Kubernetes, Google Batch, Azure and AWS
  ParallelCluster. Critically, it submits **one job per rule**, so each step gets
  right-sized resources and failures are isolated.
- **Field standard** in computational and climate science.

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
- **Nextflow** — outstanding resume, genuinely good at this scale, but Groovy.
- **Ploomber** — archived (May 2025).
- **Covalent** — designed for exactly this dispatch pattern, but last release
  May 2025. Dormant.
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

You asked whether snakemake requires a CLI or script interface per step, and
whether long-lived non-serialisable config makes that painful. The first concern
is unfounded; the second is real and is the strongest technical argument for
pytask.

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

So the accurate cost is not "a script per step". It is that **every rule still
needs a DSL stanza** declaring inputs, outputs and params, alongside the function
that already declares them.

Two honest points about that duplication, in opposite directions:

- **It is not a drift risk if the Snakefile is generated.** With the decorator as
  the single source of truth and the Snakefile emitted from it, the stanza is a
  compilation artifact. Keeping them in sync is the generator's job, not a
  human's, so they cannot diverge through forgetfulness.
- **But you still debug in the generated artifact.** When a rule misbehaves you
  read a Snakefile you did not write, reason about wildcards you did not choose,
  and map it back to the function you did write. That tax is paid on every
  confusing failure, forever, and no amount of generation removes it.

Combined with the DSL itself, this is a fair and sufficient reason to prefer
pytask, where the function *is* the declaration and there is nothing generated to
read.

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

## When to switch to snakemake

A concrete trigger rather than a vague caveat. Switch if **you end up needing
per-job cluster submission with right-sized resources per step.**

The distinction is the execution model. `dask-jobqueue` submits *worker* jobs
that hold SLURM allocations while tasks are dispatched to them. snakemake submits
*one job per rule*. For your workload — long, heterogeneous, failure-prone steps
where a download takes twenty minutes and a regrid takes four hours with very
different memory needs — per-job submission is materially more robust. Workers
holding allocations across hours of heterogeneous work is a known source of pain:
allocations expire, workers die, the scheduler loses track.

If everything runs on one large machine, which "the CMIP6 server" in `PLAN.md`
suggests is the near-term reality, this never arises and pytask is comfortably
the better choice.

Be aware that switching means accepting the double-declaration pattern you
dislike — the generated Snakefile restates what the decorators already say. That
is a real cost, and it should be paid only for a real reason, which is why the
trigger above is specific rather than a general hedge.

Two things make the switch cheap if it comes:

1. **Step bodies are plain functions**, so the science does not move.
2. **The materialisers are untouched.** You replace one binding class with a
   snakemake dispatch function: `load`/`save` become its deserialise/serialise,
   and `state()` becomes a `params` entry. Mechanical, not a rewrite.

The DAG generator that reads your database and emits the task graph is reusable
either way, which is why it is worth writing early.

## Portability: what to adapt, and what not to

Adapters are worth building here, but only at two specific seams, and both have
limits worth knowing before you rely on them.

The single biggest portability lever is not an adapter at all: **step bodies are
plain Python functions with no framework imports.** That is where the thousands
of lines will be, and it is already portable by construction. Everything below is
second-order.

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
*are* `PNode`s, pytask has leaked into the layer you were trying to protect.

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
does not make the **execution model** port cheap. If you hit the heterogeneous
resource problem, you are changing tools, not swapping an executor, and the
`StepSpec.resources` field is what carries you across.

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

Your suspicion is half right, and the mechanism is worth knowing.

**OSS can reach non-Prefect infrastructure.** A self-hosted server supports work
pool types for Process, Docker, Kubernetes, AWS ECS, Azure Container Instances,
Google Cloud Run and Google Vertex AI. Dispatching to your own cloud with only
open-source components is genuinely supported.

**But the paid path is deliberately much easier.** Cloud-only is precisely what
removes the operational pain: **push work pools** (Cloud Run, ECS, ACI, Modal —
serverless dispatch with *no worker process to run*), **managed execution**, plus
events, automations, SSO and RBAC. On OSS you run a Postgres-backed API server
and a long-lived worker inside every target environment. A convenience gradient
rather than a wall, but your instinct that you were being nudged was accurate.

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

- Within a single execution, the providers do their own optimisation —
  ESMValTool's preprocessor caches intermediate output inside a recipe run. What
  is lost is sharing *across* execution groups, not all sharing.
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
Your instinct is correct.

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
