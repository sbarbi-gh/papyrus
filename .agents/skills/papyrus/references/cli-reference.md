# Papyrus CLI reference

All commands are idempotent and communicate through the filesystem.
No command blocks on input. All exit codes are documented.

## `papyrus init`

```
papyrus init [--force]
```

Initialize a new KB tree in the current directory.

- Creates: `db/`, `concepts/`, `updates/`, `staging/`, `embeddings/`,
  `events.jsonl`, `concepts/index.json`, `method.toml`, `.env.example`
- Fails if any directory already exists (unless `--force`)
- Fails with exit 2 if `intent.md` is missing (unless `--force`)

**Exit codes:** 0 (ok), 1 (existing files, use `--force`), 2 (no `intent.md`)

---

## `papyrus update`

```
papyrus update [--query-id <id>] [--related-to <pmid>] [--dry-run]
```

Fetch candidate articles from PubMed and write enriched candidates to
`staging/<ts>/`.

- `--query-id <id>` — run a single query from `method.toml` `[[queries]]`
  (default: all enabled queries)
- `--related-to <pmid>` — ELink neighbor expansion (pmra + citedin +
  refs) from the given PMID, writes staging folders same as query mode
- `--dry-run` — fetch and dedup, print count, do not write staging or
  events

**Behaviour:**
1. Run queries (or neighbor expansion) against PubMed E-utilities
2. Dedup against `db/*/record.json` and `staging/*/<pmid>/`
3. Enrich remaining candidates via EFetch (title, abstract, authors, DOI,
   year, journal, references)
4. Write `staging/<ts>/<pmid>/candidate.json` for each candidate
5. Write `query-run` and `query-match` events to `events.jsonl`

**Exit codes:** 0 (success, may have 0 candidates), 1 (PubMed API error)

---

## `papyrus ingest`

```
papyrus ingest <pmid> [--pdf <path>] [--via seed|manual|query]
```

Ingest an article into the database. Two code paths:

**From staging (screening flow):**
1. Read `staging/*/<pmid>/candidate.json`
2. Create `db/<pmid>/record.json` with cite key, bibliographic fields,
   `status: included`, `via: query`
3. Remove candidate from staging

**Direct ingestion (seed/manual — bypasses screening):**
1. Fetch metadata from PubMed via EFetch
2. Create `db/<pmid>/record.json` with `via: seed` (or `manual`)
3. If `--pdf <path>` given, upload PDF to external storage per
   `[fulltext].storages` (rclone), set `fulltext_location` in
   `record.json`, and remove PDF from `pdfinbox/` if it resides there.
   If no external storages configured, copy locally to
   `db/<pmid>/fulltext.pdf` (gitignored).

**Exit codes:** 0 (created), 2 (already exists), 3 (not in staging and
PubMed fetch failed), 4 (rclone upload failure)

---

## `papyrus discard`

```
papyrus discard <pmid> --reason "..." [--confidence <0.0-1.0>]
```

Create a permanent `status: discarded` record so the paper isn't
re-proposed on future updates.

- Reads enrichment from `staging/*/<pmid>/candidate.json`
- Creates `db/<pmid>/record.json` with `status: discarded`, no
  `abstract.txt`
- Removes candidate from staging
- Writes `discard` event with `reason` and `confidence`

**Exit codes:** 0 (created), 2 (already exists), 3 (not in staging)

---

## `papyrus convert`

```
papyrus convert <pmid>
```

Convert `db/<pmid>/fulltext.pdf` → `fulltext.md` + `chunk_index.json`.

- If `fulltext.pdf` is missing locally, attempts to pull from external
  storage via `fulltext_location` in `record.json` before converting.
- Reads PDF, runs conversion engine from `method.toml`
  `[source_conversion].engine` (primary: marker, fallback: pymupdf4llm)
- Writes structured markdown with section-preserving headings
- Writes `chunk_index.json` mapping chunk boundaries to byte offsets
- Updates `record.json` `materials.fulltext_md` and `materials.chunks`
- **Idempotent** — skips if `fulltext.md` exists and
  `chunk_index.json.source_hash` matches current file hash

**Exit codes:** 0 (converted or already current), 1 (engine failure), 2
(no `fulltext.pdf` and PDF not found on external storage)

---

## `papyrus embed`

```
papyrus embed [<pmid>] [--all] [--pending] [--kind chunks|distillation|both]
```

Chunk and embed source text, or embed distillation text.

- `--kind chunks` (default) — split `fulltext.md` by `chunk_index.json`,
  embed each chunk, write `chunks.jsonl`
- `--kind distillation` — embed only `distillation.md`, write
  `distillation.json`
- `--kind both` — do both chunks and distillation
- `--all` — embed all included papers that have `fulltext.md`
- `--pending` — embed included papers with `fulltext.md` but no existing
  `chunks.jsonl`
- **Idempotent** — skips if on-disk embedding model matches
  `method.toml` `[embeddings].model`

**Exit codes:** 0 (embedded or already current), 1 (embedding API
error), 2 (no source text to embed)

---

## `papyrus index`

```
papyrus index
```

Regenerate `concepts/index.json` from authoritative sources.

1. Scan `concepts/*.md` for concept metadata and related concepts
2. Scan `db/*/record.json` for `custom.concepts` and `id` fields
3. Also syncs `distillation.md` `[[tags]]` into `record.json`
   `custom.concepts`
4. Write `concepts/index.json` with concepts, citekey_index,
   pmid_to_citekey

**Exit codes:** 0

---

## `papyrus check`

```
papyrus check [--json]
```

Run 13 integrity checks over the KB tree.

| # | Check | Severity |
|---|---|---|
| 1 | folder ↔ record + intent.md exists | error (warning for missing intent) |
| 2 | record ↔ materials on disk | error |
| 3 | query-id ↔ method.toml | warning |
| 4 | record schema_version ↔ method.toml schema_version | error |
| 5 | status ↔ event log | warning |
| 6 | included record has ingest event | error |
| 7 | embedding model coherence | warning |
| 8 | chunk index ↔ source hash | error |
| 9 | concept ↔ index coherence | warning |
| 10 | distillation concept tags | warning |
| 11 | consolidated index freshness | error |
| 12 | staging coherence (stale entries) | warning |
| 13 | external fulltext (verify PDF on configured rclone storages) | warning |

- `--json` — output machine-readable JSON report
- Exit 0 if no errors (warnings only OK), exit 1 if any error

---

## `papyrus search`

```
papyrus search <query> [--kind auto|concepts|distillations|chunks] [--top-k 10]
papyrus search <query> --cite <citekey> [--top-k 5]
```

Semantic search over the KB.

- `--kind auto` (default) — cascade: concepts → distillations → chunks,
  stopping at 0.5 threshold. Falls through if too few results.
- `--kind concepts` — search concept embeddings only
- `--kind distillations` — search distillation embeddings only
- `--kind chunks` — search source chunk embeddings only
- `--cite <citekey>` — restrict to chunks from the given cite key
- `--top-k` — number of results (default 10)

**Exit codes:** 0, 1 (embedding API error), 2 (cite key not
found/invalid query)

---

## `papyrus store`

```
papyrus store push <pmid>
papyrus store pull <pmid> [--output <dir>]
papyrus store check [<pmid>]
```

Manage external PDF storage via rclone per `[fulltext].storages`.

**push**
- Uploads local `db/<pmid>/fulltext.pdf` to all configured external
  rclone storages.
- Fails with exit 1 if no local PDF exists (use `store pull` to restore).
- Fails with exit 4 if rclone upload fails.

**pull**
- Downloads `fulltext.pdf` from the first reachable external storage
  into `db/<pmid>/` (default) or `--output <dir>`.
- Fails with exit 1 if PDF not found on any storage.

**check**
- With `<pmid>`: checks existence on all external storages.
- Without `<pmid>`: scans all records with `fulltext: external` and
  verifies each PDF exists. Prints a table.

**Exit codes:** 0 (ok), 1 (not found or local missing), 2 (no storages
configured), 4 (rclone error)

---

## `papyrus inbox`

```
papyrus inbox list
papyrus inbox clean [--delete] [--dry-run]
```

Manage the `pdfinbox/` staging directory per `[fulltext].pdfinbox`.

**list**
- Lists all PDF files in `pdfinbox/` with size and PMID guess
  (extracted from filename if `<pmid>.pdf` format).

**clean**
- Identifies PDFs whose filename matches a PMID that already exists in
  `db/`.
- `--dry-run` (default) — shows what would be removed.
- `--delete` — actually removes the files.

**Exit codes:** 0 (ok), 1 (pdfinbox/ doesn't exist or is empty)

---

## `papyrus notify`

```
papyrus notify [<digest>] [--latest] [--list] [--dry-run]
```

Send a digest via email using Resend API per `[notifications]`.

- `<digest>` — specific digest filename or timestamp to send
- `--latest` — send the most recent digest in `updates/`
- `--list` — list available digests
- `--dry-run` — preview email without sending

Requires `RESEND_API_KEY` in environment or `.env`.
Sender auto-derived from KB directory name if not set in `[notifications].from`.

**Exit codes:** 0 (sent), 1 (not enabled, no API key, or sending failure)
