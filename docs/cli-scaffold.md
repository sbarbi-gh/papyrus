# papyrus — CLI command scaffold

```
papyrus

  init      Initialize a new KB tree in the current directory
  update    Query PubMed, write enriched candidates to staging
  ingest    Ingest an article (from staging or direct seed/manual)
  discard   Create a discarded record with reason
  convert   Convert PDF to structured markdown
  embed     Chunk and embed a source or distillation
  index     Regenerate concepts/index.json
  check     Run integrity checks over the KB tree
  search    Semantic search over concepts, distillations, and chunks
```

All commands are deterministic, idempotent, and communicate through the
filesystem tree. No command blocks on input. Judgement steps (screening,
distillation, concept curation) are performed by an agent that reads CLI output
and invokes the next command (§9).

---

## `papyrus init`

```
papyrus init [--force]
```

Initialize a valid, empty KB tree in the current directory. Produces exactly the
folder structure, schema-stamped config files, and empty artifacts the other
commands expect. Fails if any file already exists (unless `--force`).

**Behaviour:**
1. Create directories: `db/`, `concepts/`, `updates/`, `staging/`, `embeddings/`.
2. Write `events.jsonl` — touch an empty file.
3. Write `concepts/index.json` — stamped with the running CLI's `schema_version`:
   ```json
   {"schema_version":"1.1.0","concepts":{},"citekey_index":{},"pmid_to_citekey":{}}
   ```
4. Write `method.toml` — minimal skeleton with `schema_version` and empty
   sections for the fields other commands read:
   ```toml
   schema_version = "1.0.0"

   [settings]
   db_path = "./db"

   [relevance]
   # TODO: derive from intent.md

   [source_conversion]
   engine = "marker"
   fallback = "pymupdf"

   [embeddings]
   model = "text-embedding-3-small"
   dimensions = 1536
   provider = "openai"

   [fulltext]
   [chunking]
   [distillation]
   [concepts]
   [search]
   ```
5. Assert that `intent.md` exists. If absent, the command exits 2 with a message
   — `intent.md` is human-authored and the CLI never writes it. `--force` skips
   this check.

**Stdout:**
```
Initialized KB tree (schema 1.1.0)
  db/ concepts/ updates/ staging/ embeddings/
  events.jsonl  concepts/index.json  method.toml
  intent.md NOT FOUND -- create it before running update
```

**Exit codes:** 0 (ok), 1 (`--force` needed, files exist), 2 (`intent.md` missing).

---

## `papyrus update`

```
papyrus update [--query-id <id>] [--related-to <pmid>] [--dry-run]
```

Fetch candidate articles from PubMed and stage them for screening. **Read-only
wrt `db/`** — writes nothing to record directories.

- `--query-id <id>` — run a single query from `method.toml` (default: all
  enabled queries).
- `--related-to <pmid>` — switch to ELink neighbor expansion from the given PMID
  instead of query-based search. Fires `cmd=neighbor` (pmra "related articles")
  plus `citedin` and `refs`. The PMID does not need to be a known PMID.
- `--dry-run` — fetch and dedup, print candidate count, do not write staging
  file or events.

**Behaviour:**
1. Run queries (or neighbor expansion) against PubMed via E-utilities.
2. Dedup against known PMIDs — both `db/*/` (all records, any status) and
   `staging/* /<pmid>/` (pending candidates from prior runs).
3. Enrich remaining candidates via EFetch (title, abstract, authors, DOI, year,
   journal, references).
4. Create `staging/<timestamp>/` with one folder per candidate:
   ```
   staging/2024-07-20T0900Z/
     38471234/
       candidate.json    # {pmid, title, abstract, authors, doi, year, journal, references, ...}
     38559001/
       candidate.json
   ```
5. Write `query-run` and `query-match` events to `events.jsonl`.
6. Output staging path and summary to stdout.

**Stdout:**
```
staging/2024-07-20T0900Z/  7 candidates (2 new, 5 matched in existing staging)
```

**Exit codes:** 0 (success, may have 0 candidates), 1 (PubMed API error).

---

## `papyrus ingest`

```
papyrus ingest <pmid> [--pdf <path>]
```

Ingest an article into the database. Two code paths, determined by whether the
PMID exists in staging:

**From staging (screening flow):**
1. Find `staging/*/<pmid>/candidate.json` and read enrichment data (title,
   abstract, authors, DOI, year, references).
2. Proceed to record creation. `via` is set to `query` (or `related` if the
   staging run was produced by `--related-to`).
3. Remove `staging/*/<pmid>/` (the candidate folder). If the staging run folder
   is now empty, remove it as well.

**Direct ingestion (seed/manual — bypasses screening):**
1. Fetch metadata from PubMed via EFetch (title, abstract, authors, DOI, year,
   references).
2. If `--pdf <path>` is given, copy the PDF into the record folder as
   `fulltext.pdf` and set `materials.fulltext: "present"`.
3. Proceed to record creation. `via` is set to `seed`.

**Record creation (common to both paths):**
1. Compute a frozen cite key (§4): first-author family name + year, suffixed
   to avoid collision with any existing `id` in `db/*/record.json`.
2. Create `db/<pmid>/` with:
   - `record.json` — CSL-JSON with `id` (cite key), bibliographic fields,
     `custom.pmid`, `custom.status: "included"`, `custom.references`,
     `custom.materials` (all `absent`, unless `--pdf` was given),
     `custom.concepts: []`, `schema_version`.
   - `abstract.txt` — plain-text abstract.
   - `fulltext.pdf` — if `--pdf` was provided.
3. Write `{"kind":"ingest","pmid":"…","via":"seed|query|related"}` event to
   `events.jsonl`.

**Stdout:**
```
db/38471234/  smith2024a  (via: seed)
```

**Exit codes:** 0 (created), 2 (already exists — idempotent skip), 3 (PMID
not found in staging and PubMed fetch failed).

---

## `papyrus discard`

```
papyrus discard <pmid> --reason "…" [--confidence <0.0–1.0>]
```

Create a permanent record for an article the agent has decided is not relevant.
The record stays in `db/` so the paper won't be re-proposed on the next update.

**Behaviour:**
1. Read the candidate from `staging/*/<pmid>/candidate.json` for enrichment data.
2. Assign a frozen cite key (same rules as `ingest` — discarded papers need
   one for re-screening and potential later inclusion).
3. Create `db/<pmid>/` with:
   - `record.json` — CSL-JSON, `custom.status: "discarded"`.
   - (No `abstract.txt` — the abstract is already in the candidate folder; if the
     paper is later re-screened and included, `abstract.txt` is written then.)
4. Remove `staging/*/<pmid>/`. If the staging run folder is now empty, remove it
   as well.
5. Write `{"kind":"discard","pmid":"…","reason":"…","confidence":0.94}` event.

**Stdout:**
```
db/38550010/  not-relevant  (reason: animal model, confidence: 0.94)
```

**Exit codes:** 0 (created), 2 (already exists).

---

## `papyrus convert`

```
papyrus convert <pmid>
```

Convert `db/<pmid>/fulltext.pdf` into structured markdown. Idempotent — skips
if `fulltext.md` already exists and `chunk_index.json` refers to a matching
source hash.

**Behaviour:**
1. Read `fulltext.pdf` from the record directory.
2. Run primary conversion engine (`marker`) configured in `method.toml`
   `[source_conversion]`; fall back to secondary (`pymupdf4llm`) on failure.
3. Write `fulltext.md` with section-structured markdown (§10).
4. Generate `chunk_index.json` mapping chunk boundaries to byte offsets in
   `fulltext.md` (§10).
5. Update `record.json` `custom.materials.fulltext_md: "present"` and
   `custom.materials.chunks` count.
6. Write `{"kind":"source-converted","pmid":"…","engine":"marker","pages":12}` event.

**Stdout:**
```
db/38471234/fulltext.md  14 chunks, 12 pages  (engine: marker)
```

**Exit codes:** 0 (converted or already current), 1 (engine failure — both
primary and fallback exhausted), 2 (no `fulltext.pdf`).

---

## `papyrus embed`

```
papyrus embed <pmid> [--kind chunks|distillation]
```

Chunk `fulltext.md` and compute embeddings, or embed the distillation text.
Idempotent — skips if embeddings are present and the model in `method.toml`
matches what's on disk.

- `--kind chunks` (default) — split `fulltext.md` by section boundaries
  according to `chunk_index.json`, embed each chunk, write or update
  `chunks.jsonl`. Also writes the unchunked `distillation.json` embedding if
  `distillation.md` exists and is newer than the embedding record.
- `--kind distillation` — embed only the distillation text from
  `distillation.md`, write `distillation.json`.

**Behaviour:**
1. Read source text (chunks from `fulltext.md` or distillation from
   `distillation.md`).
2. Call the embedding API configured in `method.toml` `[embeddings]`.
3. Write `chunks.jsonl` (one line per chunk with text + embedding) or
   `distillation.json` (single vector with text).
4. Update `record.json` `custom.materials.embeddings_model` and
   `embeddings_dim`.
5. Write `embedded` event to `events.jsonl`.

**Stdout:**
```
db/38471234/  14 chunk vectors, 1 distillation vector  (model: text-embedding-3-small, dims: 1536)
```

**Exit codes:** 0 (embedded or already current), 1 (embedding API error), 2 (no
source text to embed).

---

## `papyrus index`

```
papyrus index
```

Regenerate `concepts/index.json` from the authoritative sources: concept files
in `concepts/` and `record.json` files in `db/*/`. Fully idempotent — safe to
run any time, no-op if the index is already current.

**Behaviour:**
1. Scan `concepts/*.md` for concept IDs, related concepts, and metadata.
2. Scan `db/*/record.json` for `custom.concepts` arrays and `id` fields.
3. Build the bidirectional map: `concepts` (concept → PMIDs, related, counts),
   `citekey_index` (cite key → PMID), `pmid_to_citekey` (PMID → cite key).
4. Write `concepts/index.json`.

**Stdout:**
```
concepts/index.json  18 concepts, 142 articles
```

**Exit codes:** 0.

---

## `papyrus check`

```
papyrus check
```

Run the 12 integrity checks defined in §19. Errors exit non-zero; warnings are
reported but don't change the exit code (they go to stderr).

**Stdout:** JSON report with per-check results.

```
{ "result": "pass", "checks": 12, "errors": 0, "warnings": 2, … }
```

**Exit codes:** 0 (clean or warnings only), 1 (one or more errors).

---

## `papyrus search`

```
papyrus search <query> [--kind concepts|distillations|chunks] [--top-k 10]
papyrus search --cite <citekey> [--top-k 5]
papyrus search --pmid <pmid> [--top-k 5]
```

Semantic search over the KB. Reads embeddings from the tree on every invocation
(JSONL files under `db/` and `concepts/`) — fast enough at current scale.

- `--kind concepts` — search concept embeddings only (`concepts/*.json`).
- `--kind distillations` — search distillation embeddings (`db/*/distillation.json`).
- `--kind chunks` — search source chunk embeddings (`db/*/chunks.jsonl`).
  This is the default; if no `--kind` is given, the command follows the cascading
  strategy (§14): concepts → distillations → chunks, stopping at the level that
  yields results above threshold.
- `--cite <citekey>` — resolve the cite key to a PMID, then search only within
  that article's chunks (cite-verification mode).
- `--pmid <pmid>` — same as `--cite` but with an explicit PMID.

**Stdout:** JSON line per result.

```
{ "pmid": "38471234", "cite_key": "smith2024a", "kind": "chunk", "chunk_id": "00007", "score": 0.87, "text": "Knockout reduced proliferation by 72% …", "section": "Results" }
```

**Exit codes:** 0, 1 (embedding API error), 2 (cite key not found).

### Search engine (placeholder)

```
papyrus search --start [--port 8080]
papyrus search --stop
```

A future optimization: pre-load embeddings into a long-running process for sub-10ms
latency and multi-client access. Not needed at current scale (~200 articles,
search takes <500ms with in-memory scan from JSONL). Deferred until the corpus
grows or concurrent access warrants it.
