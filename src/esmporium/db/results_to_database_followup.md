# Results → database: status, model, and remaining work

**Short answer to "do we just need to build tests?":** essentially yes. The schema and
the ingestion code are built and verified against live CMIP5/6/7 searches. To finalise
this PR the remaining work is **test coverage** for the ingestion/parse code and the new
tables' constraints, an optional hand-run script, and a changelog fragment at merge time.
The "load the data back out" *flow* (the disambiguation popup, etc.) is a later PR — but
the schema is already proven loadable by the round-trip test.

---

## Where we are (2026-09-07)

Done and green (`pytest -m "not slow"`: all pass, 27 skipped; ruff clean):

- **Schema** — five result tables (below), with the `Dataset` identity index, a real
  one-to-many from `Dataset` to its editions, and deduplicated data nodes. Version is now
  decoupled from the bundle (migration `20260907_b35e5c5503c9`).
- **Migrations** — the result tables land across `20260903_f12406`,
  `20260904_2ecbccb2bbc1` (identity index, per-bundle rename) and `20260907_b35e5c5503c9`
  (decouple version, dedup nodes). The `uq_dataset_identity` expression index is
  hand-written because Alembic can't autogenerate expression indexes on SQLite. Covered by
  `tests/integration/test_migrations.py`.
- **Parsing lives in the search layer now.** `results_to_database.py` no longer parses raw
  JSON; the facade turns Solr/STAC responses into `ParsedDocument`s (see
  `search/result_parsing.py`, `search/result_readers.py`, `search/search_api_facade/`), and
  this module only writes rows.
- **Ingestion** — `results_to_database.py`: `ParsedDocument` → rows. Verified live (a CMIP5
  bundle → one edition per per-variable dataset, all sharing one raw doc; re-ingest is
  idempotent; CMIP6 Solr and CMIP7 STAC ingest correctly).
- **Disambiguation** — `dataset_uniqueness`: `all_facet_differences` (every differing
  facet, the bottom layer) and `facet_differences` (the higher layer, only the id-linked
  facet).

---

## The database model (current)

Six tables in `schema.py`. `SearchAPICallRecord` is the pre-existing search-health log;
the other five hold results. Version is decoupled from the bundle, and data nodes are
shared across editions.

```
        Dataset                          one row per (bundle, variable)
          id (int, surrogate PK)
          id_project_specific            the ESGF native id (grouping value, NOT a FK)
          project, model, …, grid_label, processing_id
            │  identity = UNIQUE index over every column except id (grid_label via coalesce)
            │ 1
            │ *   one-to-many  (dataset_id FK)
        DatasetVersion           one edition per (dataset_id, version)
          id (int, surrogate PK)
          dataset_id FK → Dataset.id
          version, is_latest, retracted
            │                                     ▲
            │ *:* via DatasetVersionNodeLink      │ *:* via RawDocVersionLink
            ▼                                     │
        DatasetNodeInformation              DatasetRawDoc
          id (PK)                             id (PK)
          data_node (UNIQUE)                  esgf_doc_id (UNIQUE), raw_json, retrieved_at
        one row per distinct node           the exact JSON a search returned, stored once
```

**`Dataset`** — one row per (ESGF bundle × variable). Unchanged this cycle.
- `id`: surrogate integer PK, meaningless.
- **Identity** is a unique *expression index* `uq_dataset_identity` over **every column
  except `id`** — `id_project_specific` + the nine facets, with `grid_label` wrapped in
  `coalesce(grid_label, '')` so two otherwise-identical CMIP5 rows (grid_label NULL)
  still collide (a plain UNIQUE won't fire on NULLs in SQLite).
- `id_project_specific`: the ESGF native id (Solr `master_id`, or the version-free STAC
  feature id). Indexed, **not unique** — a CMIP5 bundle's many variables share it.
- Facets: `project, model, institution, experiment, variant_label, variable,
  reporting_interval, grid_label` (nullable, NULL for CMIP5), `processing_id`.

**`DatasetVersion`** — one **edition**, now a real child of `Dataset`.
- `id`: plain integer surrogate PK (was `version_id = f"{ips}.v{version}"`; that coupling
  is gone, along with any `id_project_specific` on this table).
- `dataset_id`: FK to `Dataset.id` — a genuine one-to-many. Each per-variable CMIP5
  dataset gets its **own** edition row rather than sharing one; CMIP6/7 are per-variable
  already, so one edition per `Dataset` there.
- Unique on `(dataset_id, version)`. `version`, `is_latest`, `retracted` are search-time
  snapshots.

**`DatasetNodeInformation`** — one row per **distinct** data node.
- `data_node` is UNIQUE (there are only a handful across ESGF). Many editions point at one
  node row. No download URLs — file access is a later PR.

**`DatasetVersionNodeLink`** — the many-to-many join between editions and nodes.
- `(dataset_version_id, node_id)`, unique pair. A node hosts many editions; an edition can
  live on many nodes. This is the new link that lets node rows be shared.

**`DatasetRawDoc`** — the exact JSON a search returned, stored once. `esgf_doc_id`
(unique; Solr `<instance_id>|<data_node>`, STAC feature id), `raw_json`, `retrieved_at`.
**No `source_api` / `search_host`** — the generation is derivable from the JSON shape, and
the exact host lives in the search-health log (`SearchAPICallRecord`).

**`RawDocVersionLink`** — `(raw_id, dataset_version_id)` junction, unique pair. One CMIP5
document describes many per-variable editions (one `raw_id`, many links); one edition can
be described by several documents (Solr returns one per node).

### Identity behaviour (three cases, all real and verified live)

1. Same `id_project_specific`, differ on one of our columns (e.g. variable, or grid) →
   two distinct datasets, **allowed**.
2. Same all our columns, different `id_project_specific` → **allowed**; the distinguishing
   facet lives only in the native id / raw JSON. It differs per project: CMIP5 `product`,
   CMIP6 `activity_id`, CMIP7 `activity_id`/`region`/labels — so it is **never hardcoded**.
   Reading it out is `dataset_uniqueness`: `all_facet_differences(raw_a, raw_b)` lists
   *every* facet that differs (the bottom layer), and `facet_differences(…, ips_a, ips_b)`
   keeps only facets whose value sits inside each native id (the id-linked one).
3. Identical across every column incl. `id_project_specific` → **loud**
   `UnhandledDatasetClashError` (via `save_dataset`), meaning the data differs in a facet
   we don't model.

---

## How it fits together (search → rows → diagnosis)

Parsing now lives in the **search facade**, which turns each Solr/STAC response into a
`ParsedDocument` (one common shape). `results_to_database.py` consumes those and only
writes rows — it never touches raw JSON shape itself.

```
search()                          (esmporium.search)
  → facade parses each response into ParsedDocument   (Solr & STAC → one shape)
  → processor(host, parsed) = build_result_processor(session)
        → ingest_parsed_documents(session, parsed)
              → _ingest_document(parsed)              once per document
                    parsed.dataset_facets()           one facet-set per variable (CMIP5)
                    _get_or_create_dataset            match on ALL facets → idempotent
                    _upsert_version                   on (dataset_id, version)
                    _get_or_create_node               dedup by data_node
                    _get_or_create_version_node_link  edition ↔ node
                    _get_or_create_raw_doc            dedup by esgf_doc_id
                    _get_or_create_link               raw doc ↔ edition
        → session.commit()                            once per host
  ↓  (later, off the write path, when a clash needs explaining)
dataset_uniqueness.facet_differences / all_facet_differences
        re-read DatasetRawDoc.raw_json for the two clashing rows and name what differs
```

`ParsedDocument` (`search/result_parsing.py`) is the contract between the two layers:
`db` may import `search`, never the reverse. Ingestion is idempotent because every write
is a get-or-create keyed on the relevant unique constraint (so re-running a search reuses
rows rather than duplicating them). A genuine clash — everything equal incl.
`id_project_specific` — surfaces as `UnhandledDatasetClashError` from `save_dataset`.

---

## Remaining to finish this PR

**Essentially just test coverage** — the schema and ingestion are built and verified live;
what's left is pinning them down.

Done this cycle:
- **Schema-constraint tests** for the five result tables (`tests/unit/test_schema.py`):
  `(dataset_id, version)`, `data_node`, `(dataset_version_id, node_id)`, `esgf_doc_id`,
  `(raw_id, dataset_version_id)` uniqueness, plus the two many-to-many shapes. (FKs are
  *not* asserted: SQLite doesn't enforce them without `PRAGMA foreign_keys=ON`, which we
  don't set.)
- **Round-trip / clash tests** (`tests/unit/db/test_results_round_trip.py`) and
  **disambiguation tests** (`tests/unit/db/test_dataset_uniqueness.py`, now covering the
  new `all_facet_differences` bottom layer).
- **Ingestion tests** (`tests/unit/db/test_ingest_parsed_documents.py`): one edition per
  dataset, idempotent re-ingest, per-host commit, CMIP7 STAC.

Still to add:
- **Ingestion clash test**: a genuine clash (all columns incl. `id_project_specific`
  equal) raises `UnhandledDatasetClashError` through `ingest_parsed_documents`.
- **Parser tests per generation**, driven from the recorded fixtures in
  `tests/test-data/search/`, asserting the exact rows — especially CMIP5 emitting **all**
  variables under one raw doc. (Parsing lives in `search/` now, so these are search-layer
  tests.)
- One **integration** test: recorded (or opt-in live) search → ingest → assert
  `Dataset`/edition/node/rawdoc/link rows.
- Optional **hand-run script** `scripts/search_results_to_database.py` (visual, in the
  style of `scripts/cmip5_results_to_dataset.py`): search → ingest → print the rows.
- **Changelog** fragment (`changelog/<MR>.feature.md`) at merge time.

## Deferred to later PRs (out of scope here)

- File download URLs / access.
- Pagination (currently only the first page is ingested; PR2.5).
- `dataset_addition_source` column (dataset found via query vs. via local files).
- Content-diff tracking when a raw doc changes under the same `esgf_doc_id` (today
  get-or-create keeps the first).
- Precise `source_api` generation (the generation is derivable from the JSON shape; the
  exact host lives in `SearchAPICallRecord`).
- The **load / clash-resolution flow** (the "which product?" popup) built on
  `facet_differences` / `all_facet_differences` — a later PR; the schema is already proven
  loadable by the round-trip test.

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
### Updates to schema / database model

> **IMPLEMENTED (2026-09-07, migration `20260907_b35e5c5503c9`).** The spec below is the
> brief for the version/node decoupling; it is now done and reflected in "The database
> model (current)" above. Kept verbatim for the rationale. The one open question it raises
> — DatasetNodeInformation as shared nodes via a many-to-many — was taken: nodes are unique
> and joined through `DatasetVersionNodeLink`.

We have some updates to our database model that we need to implement. This only handles the dataset/version/node/raw_docs linking, with SearchAPICallRecord we consider separate to this current 'save results to database' step. Below is the mermaid-style flowchart for visualisation, and I will talk through the changes to make on the current schema (although the mermaid flowchart should give you indiciation of column names and what columns must be removed relative to the current schema).

Firstly, we keep Dataset as is. We have already made updates and changes to this table and you can look at db/test_dataset_uniqueness.py to see the test cases we want Dataset to handle (especially by comparing "our" columns and the id_project_specific column, and when errors should raise).

More importantly, we have DatasetVersion, where we need to remove the coupling between id_project_specific and version (currenlty coupled as version_id). From Dataset to DatasetVersion, we should have a one-to-many, for all CMIP projects. This includes CMIP5, where we save all variables as dataset rows (regardless of what the user searched for), thus every CMIP5 dataset has a row per variable which would become many rows per version for a variable of a dataset. It is for this reason that we define uniqueness in this table as dataset.id + version. (should this column be a str? or combine dataset.id and datasetversion.id somehow?). DatasetVersion should have a primary key 'id', which is a plain int, instead of version_id. id_project_specific should not be included or learnt from in this table at all.

The link from DatasetVersion to DatasetRawDocs should be relatively straightforward. This is where we have many-to-many for CMIP5, where we only want one raw_doc for many dataset rows (because we separate out variables). The change compared to the current schema is the raw_link, where we now are linking the tables with the dataset_version column, instead of 'version_id'. Again, no id_project_specific knowledge.

Finally, we have DatasetNodeInformation. Previously, this table has been handled between version to node as one-to-many. However, we have an alternative option to explore. Given there are a limited number of data nodes (e.g. 15 possible data nodes that could host files), we are hoping to populate DatasetNodeInformation with as many unique data nodes as there are available following the search (e.g. <15 rows, if there are only 15 possible data nodes that host files). This would mean that many DatasetVersion rows would point to one DatasetNodeInformation row, AND that a single DatasetVersion row could point to multiple DatasetNodeInformation rows. We need help with this implementation, and to know whether this is possible.

Please repeat the database changes back to me, so I can verify you understand (and in your plan include the columns you will be deleting/renaming etc). Please also identify any pros/cons to our changes and provide alternatives if you think there are better ways to handle the workflow we want to reproduce.

erDiagram
    DATASET ||--o{ DATASET_VERSION : "has versions"
    DATASET_VERSION ||--o{ DATASET_NODE : "downloadable from (probably needs to become a many-to-many link with associated linking table?)"
    DATASET_VERSION ||--o{ RAW_LINK : "described by"
    RAW_RECORD ||--o{ RAW_LINK : "describes"

    DATASET {
        int id PK
        string id_project_specific "provided by the project, no assumptions or constraints of uniqueness or anything else"
        string project
        string model
        string institution
        string experiment
        string variant_label
        string variable
        string reporting_interval
        string grid_label "NULLABLE for CMIP5"
        string processing_id
    }
    DATASET_VERSION {
        int id PK "Maybe unnecssary given version and dataset_id is already unique and can act as the primary key?"
        string version
        int dataset_id FK "Combination of dataset_id and version must be unique"
        bool is_latest "Could rename to just 'latest' ?"
        bool retracted
    }
    DATASET_NODE {
        int id PK
        string data_node

    }
    RAW_RECORD {
        int id PK
        string esgf_doc_id "UNIQUE (use whatever ID that you get from ESGF that should mean these docs are unique)"
        json raw_json
        datetime retrieved_at
    }
    RAW_LINK {
        int id PK
        int raw_id FK
        int dataset_version_id FK
    }


We are in the process of implementing our search results into the database. We have made some changes to our schema (database model) and feel confident in these changes. The ingestion of the results we are still working on. To handle the various projects across the various esgf-"generations" with Solr and STAC, we have created the search_api_facades to handle all the translations and differences between Solr and STAC (and the 1.5bridge) in the search step. To actually ingest (save) results to the database, we have moved all the ingestion and results parsing into the seearch_api_facade space, however not all the ingestion/loading is in the facade space yet. In dataset_uniquess.py, we have the _normalise() function which does not assume the Solr or STAC of the raw_doc results. This should be moved into the serach_api_facade layer. We do not want this coupling between facades, and it is ok to copy and paste things as much as you need so that we keep solr and stac uncoupled and in the facade layer. The dataset_uniqueness clash itself should live in the db layer, but the normalisation should live in facade. This means that the data being handed to dataset_uniqueness.py should be a in a consistent format and shouldn't care which generation it came from.

Additionally, dataset_uniqueness.py is primarily to raise a note for users if there is a clash upon loading of data. Given out uniqueness constraint across all columns in dataset, it is only when we load data and find that all "our" columns are the same but id_project_specific is different, that we investigate the source of the clash. The clash should identify the facet names (that clash), the facet values, both keyed by dataset.id. See here for an example. Please make a plan for this build adn then I will go over additional things to update in this PR
# Please build this API.
# We want to be able to pass in one or more pieces of normalised information
# related to datasets
# (we could get clashes over more than just two rows)
# and to get back something which shows all the facets that differ,
# with clear links back to the datasets we started with.
#
# The flow I'm expecting is:
# - ingest datasets, including saving their normalised facets
# - load data
# - discover a clash
# - load normalised facets for each dataset in the clash
# - pass into this function
# - get the differences
# - higher-level wrapper then does something with these differences
#   to make a nice error for the user
#
# I don't mind if you keep or delete the functions above.
def facet_differences(
    normalised_info: tuple[tuple[int, dict[str, Any]], ...],
) -> dict[str, dict[int, Any]]:
    """
    Find the facets that explain why datasets differ

    Parameters
    ----------
    raw_info
        Raw information

        Each element is a tuple with two elements.
        The first is the ID of the dataset
        (or dataset version, Anna please think and decide)
        to which these facets are linked.
        The second is the normalised facets.

    Returns
    -------
    :
        `{facet_name: {id_a: value_in_a, id_b: value_in_b}}`
        for each distinguishing facet.
        Empty if nothing in the raw documents explains the id difference.

    Examples
    --------
    >>> facet_differences(
    ...     ((2015, {"product": ["output1"]}), (1031, {"product": ["output2"]}))
    ... )
    {'product': {2015: 'output1', 1031: 'output2'}}
    """
    # Check that no ID is repeated in normalised_info
    raise NotImplementedError


We are in the process of implementing the results to database step. We have done a lot of work with getting the schema (database model) to a good place and it is doing exactly what we want. We have also made sure that result parsing and ingestion (and loading to find clashes for uniqueness) are all in the search api facade layer. We do not want any coupling between facades, we should not be questioning in the result saving or loading step "what solr or stac format is this data?". This should all happen in the api facade layer and hand results in a consistent format to the db/ layer for saving. You may have a look through the repository and verify to see if we have any lingering coupling between facades in this search step.

However, beyond this, I have some additional updates to make.
1. Rename DatasetVersionSpecific to DatasetVersion (please rename everywehre in doc strings and tests and functions)
2. For class NodeInfo, why are we saving Index host informatin? Can we remove it (and replica?) and rename Node Info to DataNodeInfo?
3. in results_to_databse.py     def __init__(self, dataset: Dataset) -> None:
        self.dataset = dataset
        super().__init__(
            "Two datasets are identical across every column our model records "
            f"(id_project_specific={dataset.id_project_specific!r}, "
            f"variable={dataset.variable!r}), so our dataset model cannot tell them "
            "apart. This clash is not handled: the data differs in a facet we do not "
            "model. Flatten the raw documents with "
            "esmporium.search.normalise_stored_document and compare them with "
            "esmporium.db.dataset_uniqueness.facet_differences to find the difference."
        )
  We still have this conecpt of data being unique per variable and id_project_specific. Note that this is ONLY relevant for CMIP5 use cases. We want to remove this concept of uniqueness, and note that we have built our concept of the main Dataset table to be unique across all columnns (noting our potential test cases in tests). Please update this concept of uniquess here and elsewhere (is keying by variable a uniqueness constraint anywhere else that is left over from when that was the constraint in the db?).
4. in schema.py we define DATASET_IDENTITY_INDEX. Aren't these meant to be autogenerated somewhere? Why is this hardcoded? Where is this relevant?
5. In test_dataset_uniqueness.py, are we still testing uniqueness based on id_project_specific facets? Shound't this be tests by parsing the raw docs to reflect out updated workflow?
6. In test_schema.py we have named some of the tests as test1/2/3. This is irrelevant to future developers (and even to our future selves). Could we please rename them:
test_case1_same_native_id_differ_on_our_column_is_allowed - test_same_id_project_specific_differ_on_our_column_is_allowed
test_case2_same_our_columns_differ_on_native_id_is_allowed - test_same_our_columns_differ_on_id_project_specific_is_allowed
test_case3_identical_everything_raises_clash - test_identiical_all_columns_raises_clash
Anywhere else in the repo are there numbered test/use cases that should have a more specific name?
7. apis/esgfng.py lines 187-190 def _version_from_id(native_id: str) -> str:
    """Read the version out of a `.vYYYYMMDD` token, if present."""
    parts = native_id.split(".")
    if parts and _VERSION_TOKEN.match(parts[-1]):
  Please remove the regexp and just do this inline (ie. 'anything after .v'). regexp is super easy to make a mess and this task doesn't require it.
8. doc string cross reference. search/result_parsing.py read_result_facets() dont' jsut have `dataset`, cross reference bakc to schema here.
9. Finally, search/result_reader.py. Is this necessary to have separated from the facade parameters? Could we incoroprate that into the facade parameters rather than having a modelu that is global/hardcoded values? Are there benefits to keeping it there? Please investigate and let me know.
