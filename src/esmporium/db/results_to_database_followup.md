# Results → database: status, model, and remaining work

**Short answer to "do we just need to build tests?":** essentially yes. The schema and
the ingestion code are built and verified against live CMIP5/6/7 searches. To finalise
this PR the remaining work is **test coverage** for the ingestion/parse code and the new
tables' constraints, an optional hand-run script, and a changelog fragment at merge time.
The "load the data back out" *flow* (the disambiguation popup, etc.) is a later PR — but
the schema is already proven loadable by the round-trip test.

---

## Where we are (2026-09-04)

Done and green (`pytest`: 385 passed / 27 skipped; ruff + mypy clean):

- **Schema** — six tables (below), with the `Dataset` identity index and per-bundle
  editions.
- **Migration** — `20260904_2ecbccb2bbc1` (single revision on committed `f12406`; the
  `uq_dataset_identity` expression index is hand-written because Alembic can't
  autogenerate expression indexes on SQLite). DDL regression fixture regenerated.
- **Ingestion** — `parse.py` + `results_to_database.py`: raw search JSON → rows.
  Verified live (CMIP5 3 docs → 51 variables sharing one bundle edition; re-ingest is
  idempotent; CMIP6 Solr and CMIP7 STAC parse correctly).
- **Disambiguation** — `dataset_uniqueness.facet_differences`.
- **Existing unit tests updated** to the new shape (`test_results_round_trip.py`,
  `test_dataset_uniqueness.py`, `test_schema.py`).

`src/esmporium/search/` was **not** touched — ingestion consumes what `search()` already
returns (`SearchOutcome.results`, host → raw JSON).

---

## The database model (current)

Six tables in `schema.py`. `SearchAPICallRecord` is the pre-existing search-health log;
the other five hold results.

```
        Dataset                      one row per (bundle, variable)
          │  id (int, surrogate PK)
          │  id_project_specific ────────────────┐  (grouping key, not a FK)
          │  project, model, …, grid_label, processing_id
          │                                       │  match on id_project_specific
          ▼                                       ▼
   identity: UNIQUE index over          DatasetVersionSpecific   one row per bundle EDITION
   every column except id                 version_id (PK) = f"{id_project_specific}.v{version}"
   (grid_label via coalesce)              id_project_specific, version, is_latest, retracted
                                                 │
                          ┌──────────────────────┼───────────────────────┐
                          ▼                       ▼                       │
              DatasetNodeInformation      RawDocVersionLink ──*:*── DatasetRawDoc
              one row per (edition,        (raw_id, version_id)      esgf_doc_id (unique)
              data_node); replica,          unique pair             search_host, raw_json
              index_node                                            retrieved_at
```

**`Dataset`** — one row per (ESGF bundle × variable).
- `id`: surrogate integer PK, meaningless.
- **Identity** is a unique *expression index* `uq_dataset_identity` over **every column
  except `id`** — `id_project_specific` + the nine facets, with `grid_label` wrapped in
  `coalesce(grid_label, '')` so two otherwise-identical CMIP5 rows (grid_label NULL)
  still collide (a plain UNIQUE won't fire on NULLs in SQLite).
- `id_project_specific`: the ESGF native id (Solr `master_id`, or the version-free STAC
  feature id). Indexed, **not unique** — a CMIP5 bundle's many variables share it.
- Facets: `project, model, institution, experiment, variant_label, variable,
  reporting_interval, grid_label` (nullable, NULL for CMIP5), `processing_id`.

**`DatasetVersionSpecific`** — one row per **bundle edition** (not per variable).
- `version_id` (PK) = `f"{id_project_specific}.v{version}"`.
- `id_project_specific` (indexed grouping key — a `Dataset` finds its editions by
  matching this), `version`, `is_latest`, `retracted` (search-time snapshots).
- CMIP5: all variables of one bundle share **one** edition row. CMIP6/7: the bundle is
  already per-variable, so it's one edition per `Dataset` anyway.

**`DatasetNodeInformation`** — one row per (edition, data node). `version_id` (FK),
`data_node`, `index_node`, `replica`. Unique `(version_id, data_node)`. **No download
URLs** — file access is a later PR.

**`DatasetRawDoc`** — the exact JSON a search returned, stored once. `esgf_doc_id`
(unique; Solr `<instance_id>|<data_node>`, STAC feature id), `search_host`, `raw_json`,
`retrieved_at`. **No `source_api`** — the exact host is `search_host`.

**`RawDocVersionLink`** — `(raw_id, version_id)` junction, unique pair. Kept as a
many-to-many for future flexibility; with per-bundle editions it is effectively
many-documents-to-one-edition (one raw doc per node).

### Identity behaviour (three cases, all real and verified live)

1. Same `id_project_specific`, differ on one of our columns (e.g. variable, or grid) →
   two distinct datasets, **allowed**.
2. Same all our columns, different `id_project_specific` → **allowed**; the distinguishing
   facet lives only in the native id / raw JSON. It differs per project: CMIP5 `product`,
   CMIP6 `activity_id`, CMIP7 `activity_id`/`region`/labels — so it is **never hardcoded**.
   `facet_differences(raw_a, raw_b, ips_a, ips_b)` reads the facet name+values from the
   raw docs (rule: value differs, appears inside each native id, isn't the whole id).
3. Identical across every column incl. `id_project_specific` → **loud**
   `UnhandledDatasetClashError` (via `save_dataset`), meaning the data differs in a facet
   we don't model.

---

## Ingestion (how raw JSON becomes rows)

`results_to_database.ingest_results(session, results)` — consumes `SearchOutcome.results`
(host → raw JSON), commits once, and is **idempotent** (a re-run reuses rows).

Per host, per document (`parse.parse_document` → `ParsedDoc`):
- Detect shape from the response: `response.docs[]` ⇒ Solr, `features[]` ⇒ STAC.
- **CMIP5**: read the whole `variable` list from the dataset-level doc and emit one
  `Dataset` per variable — **all** of them, never the file layer. `model` is the DRS
  token (`instance_id` index 3), `grid_label = None`.
- **CMIP6 (Solr)**: `source_id`/`institution_id`/…/`table_id`; one variable.
- **CMIP6/7 (STAC)**: facets from `properties` under `cmipN:` keys; `processing_id` =
  `table_id` (CMIP6) / `variable_branding_suffix` (CMIP7); `id_project_specific` = the
  feature id with the trailing `.vYYYYMMDD` stripped; `version` from `properties.version`;
  data node(s) from the asset href hostnames.
- Write: get-or-create `Dataset` (by all facets, so re-ingest is idempotent and NULL grid
  matches); upsert edition on `version_id` (refresh `is_latest`/`retracted`); upsert node
  on `(version_id, data_node)`; get-or-create raw doc by `esgf_doc_id`; link.

`parse.data_node_from_esgf_doc_id` recovers the node from a Solr id (`rsplit("|", 1)[-1]`;
STAC ids have no `|`).

---

## Remaining to finish this PR

1. **Tests (the main remaining work).** Deliberately deferred until the model settled;
   now it has. Add:
   - **Parser** unit tests, per generation, driven from the recorded fixtures in
     `tests/test-data/search/` (Solr CMIP5/CMIP6, bridge CMIP6, STAC CMIP6/CMIP7):
     assert the exact rows, especially CMIP5 emitting **all** variables under one edition.
   - **Ingestion** tests: idempotent re-ingest; per-bundle sharing (many variables → one
     edition + one raw doc); a genuine clash raises `UnhandledDatasetClashError`.
   - **Constraint** tests on the new tables: FK integrity, `esgf_doc_id` uniqueness,
     `(version_id, data_node)` uniqueness, `(raw_id, version_id)` uniqueness.
   - One **integration** test: recorded (or opt-in live) search → `ingest_results` →
     assert `Dataset`/edition/node/rawdoc/link rows.
2. **Optional hand-run script** `scripts/search_results_to_database.py` (visual, in the
   style of `scripts/cmip5_results_to_dataset.py`): search → ingest → print the rows.
3. **Changelog** fragment (`changelog/<MR>.feature.md`) at merge time.

## Deferred to later PRs (out of scope here)

- File download URLs / access.
- Pagination (currently only the first page is ingested; PR2.5).
- `dataset_addition_source` column (dataset found via query vs. via local files).
- Content-diff tracking when a raw doc changes under the same `esgf_doc_id` (today
  get-or-create keeps the first).
- Precise `source_api` generation (we keep the exact `search_host`; solr/stac is derivable
  from the shape).
- The **load / clash-resolution flow** (the "which product?" popup) built on
  `facet_differences` — a later PR; the schema is already proven loadable by the
  round-trip test.

---

## Original working notes (preserved verbatim)

These are the earlier brainstorming notes and TODOs. Many are now resolved (e.g. the
integer PK, "for cmip5 just return all variables", the CMIP5 many-to-many link, where the
JSON→Dataset conversion lives — it is `results_to_database.py`); kept here so nothing is
lost.

```
# TODO: does it need to be built from facet columns? Or can just be integer primariy keys ->
# TODO: point of keeping this is to quickly check if we've seen a dataset in a search result
# IMportant for knowing if 're-process'
# does keeping this make sense?
# Will be reprocessing results anyway even if have seen data to check if have been changes since last time

# TODO: check if CMIP5 many to many for variables!!
# Above diagram only shows use case for cmip5 single variable

# TODO: facade should expect a certain shape. assert that we get the certain shape that we expect
# TODO : grid label as integer not relevant if dataset.id is an integer rather than built on column facets
# TODO: for cmip5 just return all variables from list
# TODO id slot will change cmIp5 eg dataset.id
# TODO need to make sure this handles .v1 as well as the consistent format as above - just get everything after v

# TODO: merge pull request first
# TODO: where to do conversion from raw json to datasets.
# line 44 in search.py ?
# potentially inject another observer?
# observer result processor?
# ask claude?
# but preference likely to convert to dataset objects here (therefore edit in src)
# but saving to db optional here -> touching db could be handled elsewhere
# see above cmip5 save all variables at all times
# Search for all, have all, but only need to return what user asked for
# to do: ask claude could this blow out the dataset records with rows

# TODO: add use case for tas + rsdt for CMIP5 and make sure can handle it
# see comments above - one or multiple variables should return same thing at all times

# TODO :
testing + use case for project-specific native
just keep in json raw format -> link to raw docs
Can always get back to project specific names
Keep cost of json.
Will this scale to 1million dataset entries?
Different product -> same version -> need to distinguish
product in id_project_specific?
CMIP5 retains project_specific_id
test: ingest cmip5 output from search that gives datasets that only differ by product
note that how to get back out is deferred to getting data loading
check with claude - if need to change schema to get data back out then should do that now
plus maybe tests of loading stuff back out
General test:
- CMIP5/6/7 if we parse search results into db, can we load back out using same query? do we handle edge cases where get clashes, because two datasets match the same query, our view of datasets are the same and only distinguished by project specific facets
- Claude can check for cmip6/7, only vary by columns in dataset?

"can you merge origin/main onto this branch"

make sure there is a commit just adding that test
- then talk to zeb, get comments
- then we can role from there
```
