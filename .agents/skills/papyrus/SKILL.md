---
name: papyrus
description: |
  PubMed Knowledge Base Manager. Use when working with the KB: searching,
  screening candidates, distilling papers, curating concepts, or running
  integrity checks. Triggers: "update the KB", "search the knowledge base",
  "screen candidates", "distill this paper", "what does the KB know about X",
  "papyrus", "PubMed KB", "run the pipeline".
compatibility: opencode,claude-code,pi,mistral-vibe
---

# Papyrus — PubMed Knowledge Base Manager

## What papyrus is

Papyrus is a deterministic CLI (`papyrus`) that operates on a self-contained,
filesystem-based knowledge base. Every article, concept, embedding, and event
lives as a file in a single folder tree — no database, no daemon.

The CLI handles only **deterministic** operations: fetching PubMed results,
creating records, converting PDFs, chunking, embedding, indexing, search, and
integrity checks. Everything requiring **judgement** — screening candidates,
distilling articles, curating concepts, writing digests — is performed by you
(the agent) by reading files the CLI produces and calling the next CLI command.

The agent ↔ CLI protocol is the filesystem. No command blocks on input; every
command runs to completion.

## Setup

The CLI lives in a git repo. If `papyrus` is not on PATH, install it in the
current KB (or any Python project) with:

```bash
uv add git+https://git.sbarbi.it/sbarbit/papyrus.git
```

Then run commands as:

```bash
uv run papyrus <cmd>
```

The repo URL for cloning or adding is:

```
https://git.sbarbi.it/sbarbit/papyrus.git
```

## Orientation

Always start by locating the KB root. It's the directory containing
`method.toml`. Run all papyrus commands from the KB root or a subdirectory
below it. The CLI walks up to find `method.toml` automatically.

```bash
papyrus init    # if creating a new KB
papyrus check   # verify an existing KB is healthy
```

## Command quick reference

| Command | Purpose | Key flags |
|---|---|---|---|
| `papyrus init` | Create a new KB tree | `--force` |
| `papyrus update` | Query PubMed → staging | `--query-id`, `--related-to`, `--dry-run` |
| `papyrus ingest <pmid>` | Ingest from staging or seed | `--pdf`, `--via` |
| `papyrus discard <pmid>` | Discard with reason | `--reason`, `--confidence` |
| `papyrus convert <pmid>` | PDF → structured markdown | — |
| `papyrus embed <pmid>` | Embed chunks or distillation | `--all`, `--pending`, `--kind` |
| `papyrus index` | Regenerate concepts/index.json | — |
| `papyrus check` | Run 13 integrity checks | `--json` |
| `papyrus refresh-meta [<pmid>]` | Backfill volume/issue/pages/ISSN for records | `--dry-run` |
| `papyrus search <query>` | Semantic search | `--kind`, `--top-k`, `--cite` |
| `papyrus store push \| pull \| check` | Manage external PDF storage | — |
| `papyrus inbox list \| clean` | Manage pdfinbox/ staging dir | `--delete`, `--dry-run` |
| `papyrus notify <digest>` | Send digest via email (Resend) | `--latest`, `--list`, `--dry-run` |

Full signatures in `references/cli-reference.md`.

## The KB tree at a glance

```
intent.md                    # human-authored premise (you never write this)
method.toml                  # queries, filters, parameters (co-managed)
events.jsonl                 # append-only event log (JSON-lines)
pdfinbox/                    # staging area for incoming PDFs before ingest
updates/<ts>.md              # per-run narrative digest (you write)
staging/<ts>/<pmid>/          # candidates awaiting screening
  candidate.json
db/<pmid>/                    # permanent record (PMID-based canonical path)
  record.json                # CSL-JSON + custom process state
  abstract.txt               # plain-text abstract
  fulltext.pdf               # original PDF (gitignored; cached from external storage)
  fulltext.md                # structured markdown (after convert)
  chunk_index.json           # chunk boundary map
  chunks.jsonl               # per-chunk embeddings
  distillation.md            # concept-tagged summary (you write)
  distillation.json          # distillation embedding
concepts/<id>.md             # long-form concept article (you write)
  index.json                 # bidirectional PMID↔citekey↔concept map
```

Full schema in `references/file-scheme.md`.

## Core workflows

### Bootstrap a new KB

1. Human writes `intent.md` — the project premise.
2. `papyrus init` — creates the tree, stamps schema versions, writes
   skeleton `method.toml`. Fails if `intent.md` is missing.
3. Human seeds articles: `papyrus ingest <pmid> --via seed --pdf <path>`.
4. **Your job**: read `intent.md` + seed articles → derive initial
   `method.toml` queries, propose on a branch for human merge.
5. First update: `papyrus update`, then screen candidates, ingest relevant
   ones, convert, embed, index.

### Scheduled update cycle

1. `papyrus update` — queries PubMed, deduplicates against known PMIDs,
   writes enriched candidates to `staging/<ts>/`.
2. **Your job — screening**: read each candidate from staging. Apply the
   inclusion criteria from `method.toml` `[relevance].inclusion` and the
   orientation from `intent.md`. For each candidate, decide:
   - **Ingest**: `papyrus ingest <pmid>`
   - **Discard**: `papyrus discard <pmid> --reason "..." --confidence 0.X`
 3. **Your job — full-text**: for each new ingest, try to find the PDF.
    - Place PDFs in `pdfinbox/` before ingest.
    - `papyrus ingest <pmid> --pdf pdfinbox/<file>.pdf` — uploads to
      external storage (rclone, per `[fulltext].storages`), records
      `fulltext_location` in `record.json`, and cleans `pdfinbox/`.
    - If PDF is not in `pdfinbox/`, `papyrus ingest <pmid> --pdf <path>`
      works from any path — uploads to external storage.
    - If you can fetch it (Unpaywall, OA): download → place in `pdfinbox/`
      → `papyrus ingest --pdf`.
    - If unavailable: leave as `fulltext: absent` (not blocking).
 4. `papyrus convert <pmid>` — PDF → structured markdown + chunk index.
    If `fulltext.pdf` is missing locally, pulls from external storage
    on demand. Skips if already converted and source hash matches.
5. `papyrus embed <pmid>` — chunk text and embed, or embed distillation
   (skip if model on disk matches method.toml).
6. `papyrus index` — regenerate `concepts/index.json`.
 7. `papyrus check` — verify 13 integrity checks pass.
8. **Your job — digest**: write `updates/<ts>.md` summarizing new articles,
   common threads, and suggestions for query/filter revision.

### Screening rubric

When reading a `candidate.json` from staging, evaluate:

- **Title + abstract against `intent.md` scope**. Does it match the premise?
  Check `method.toml` `[relevance].inclusion` for operational criteria.
- **Study type**: prefer primary studies over reviews (unless intent says
  otherwise). Exclude case reports, commentaries, editorials.
- **Population/model**: match the scope declared in `intent.md` and
  `method.toml` `[relevance].inclusion`. Check confidence carefully.
- **Language**: English only by default.
- **Date**: check against `method.toml` query date bounds.
- **When uncertain**: set a moderate confidence (0.5–0.7) and include.
  False positives are cheaper than missed signals.

Always write a concrete, searchable `--reason` on discard (e.g. "review
only", "case report n=1", "out of scope: toxicology").

### Search workflows

**Concept-level queries** ("What does the KB know about gene X in renal carcinoma?"):
1. `papyrus search "gene X renal carcinoma" --kind concepts --top-k 5`
2. If concepts don't answer: `papyrus search "gene X renal carcinoma" --kind distillations --top-k 10`
3. Read the top-ranked concept `.md` files or distillation `.md` files for detail.

**Evidence-level queries** ("Did Smith et al. really show that gene X knockdown reduces proliferation by 72%?"):
1. `papyrus search "gene X reduces proliferation by 72%" --cite smith2024a --kind chunks --top-k 5`
2. Read the surrounding section from `fulltext.md` using the chunk offsets.
3. Judge: supported / partially supported / contradicted / not found.

**Exploratory** ("Show me everything related to immunotherapy in RCC"):
1. `papyrus search "immunotherapy renal carcinoma" --kind auto --top-k 20`
   (auto cascades: concepts → distillations → chunks at threshold 0.5)
2. For depth: find relevant concepts, then read their `.md` files for linked PMIDs.

### Distillation workflow

When the KB has a converted fulltext (`fulltext.md` exists):

1. Read `db/<pmid>/fulltext.md`.
2. Write `db/<pmid>/distillation.md` following `method.toml`
   `[distillation].required_sections` (typically: Claim, Methods, Findings,
   Concepts, Relation-to-corpus).
3. Tag concepts with `[[concept-id]]` wikilinks. Use existing concepts from
   `concepts/index.json` when applicable; propose new ones as needed.
4. Optionally update `concepts/<id>.md` if the article adds substantial
   new information to an existing concept.
5. `papyrus embed <pmid> --kind distillation`
6. `papyrus index` (picks up new tags)

### Concept curation

- **Creation**: when a new `[[concept]]` tag appears in a distillation,
  `papyrus check` flags it as a proposal. Create `concepts/<id>.md` with:
  definition, key genes/terms, linked articles, related concepts.
- **Splitting**: if a concept file grows too broad, propose a split.
- **Merging**: if two concepts overlap, propose one as the canonical name.
- **Archiving**: a concept with zero articles is flagged by `papyrus check`
  as a warning. Archive it to `concepts/.archived/` or add articles.

After concept changes, always run `papyrus index` and `papyrus check`.

### Maintenance

- `papyrus check` — run after any pipeline step to catch inconsistencies.
  Fix errors immediately; investigate warnings. Check #13 verifies external
  fulltext existence on configured rclone storages.
- `papyrus index` — regenerate after any concept or record change.
- `papyrus store push` — push local PDFs to external storage (rclone).
- `papyrus store pull` — restore missing PDFs from external storage.
- `papyrus store check` — verify all external fulltext PDFs are reachable.
- `papyrus inbox list` — list PDFs staged in `pdfinbox/`.
- `papyrus inbox clean` — remove ingested PDFs from `pdfinbox/`.
- If `method.toml` `[embeddings].model` changes, re-embed all articles:
  `papyrus embed --all`, then `papyrus check`.
- If schema version in `method.toml` is bumped, run migration scripts
  (see `migrations/`), then `papyrus check`.

## Important invariants

- **Cite keys are frozen at birth.** Never rename a cite key in
  `record.json` `id` without declaring it in `citekey_aliases`.
- **`record.json` separates bibliographic fields from process state.**
  Everything papyrus-specific lives under `custom.*`. CSL-JSON fields at
  the top level must remain standard.
- **The concept index is always regenerable.** `concepts/index.json` is a
  cache — delete it and run `papyrus index` to rebuild.
- **Embeddings are records, not database rows.** They live next to their
  source text inside `db/<pmid>/` or `concepts/<id>.json`. Moving the
  folder moves the entire search index.
- **Staging is ephemeral.** Once all candidates in a `staging/<ts>/` run
  are ingested or discarded, the run folder is deleted automatically.
- **Full-text is decoupled from inclusion.** A paper can be `status:
  included` with `fulltext: absent` indefinitely. It's searchable by
  metadata and abstract.
- **PDFs are not version-controlled.** `db/*/fulltext.pdf` is gitignored.
  The authoritative copy lives in external storage (rclone remote per
  `[fulltext].storages`). Use `papyrus store pull` to restore after clone.
- **PMID-based paths form a global namespace.** `db/<pmid>/` is the
  canonical path for a given article across all KBs. The same PMID can be
  referenced independently of which KB it was first ingested into.

## When to read reference files

- Need the exact record schema or event log format → `references/file-scheme.md`
- Need a command signature or exit code → `references/cli-reference.md`
