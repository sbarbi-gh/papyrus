# Papyrus — PubMed Knowledge Base Manager

Papyrus is a deterministic CLI (`papyrus`) that operates on a self-contained,
filesystem-based knowledge base of PubMed articles. Every article, concept,
embedding, and event lives as a file in a single folder tree — **no database,
no daemon**.

The CLI handles only **deterministic** operations: fetching PubMed results,
creating records, converting PDFs, chunking, embedding, indexing, search, and
integrity checks. Everything requiring **judgement** — screening candidates,
distilling articles, curating concepts, writing digests — is performed by a
human (or an agent) by reading the files the CLI produces and calling the next
CLI command. The agent ↔ CLI protocol is the filesystem.

## Features

- **Filesystem as database** — records, concepts, embeddings and the event log
  are plain files (`record.json`, `concepts/*.md`, `chunks.jsonl`,
  `events.jsonl`), all `jq`-queryable.
- **PubMed E-utilities** — `esearch`/`efetch`/`elink` wrappers with rate-limit
  handling and deduplication.
- **PDF → structured markdown** — section-preserving conversion via
  `pymupdf4llm`, with chunk boundary maps (a `marker` engine is configurable
  but not yet implemented).
- **Embedding** — provider-swap (`openai`, `ollama`) with the model stamped on
  every stored embedding for coherence checks.
- **Semantic search** — in-memory cosine similarity over concepts,
  distillations, and source chunks, with cascading `auto` mode.
- **Integrity checks** — 13 checks over the tree (`papyrus check`).
- **External PDF storage** — rclone-backed push/pull/check for fulltext PDFs.
- **Append-only event log** — a JSON-lines trail of every mutation.

## Installation

Papyrus requires **Python 3.11+** and is managed with [`uv`](https://docs.astral.sh/uv/).

Add it to a Python project (a KB is one) and run it through `uv`:

```bash
uv add git+https://github.com/sbarbi-gh/papyrus.git
uv run papyrus --help
```

Or clone and run from source:

```bash
git clone https://github.com/sbarbi-gh/papyrus.git
cd papyrus
uv sync
uv run papyrus --help
```

The entry point is defined in `pyproject.toml`:

```toml
[project.scripts]
papyrus = "papyrus.cli:main"
```

## Quick start

```bash
# 1. Create a new KB tree (requires an intent.md in the directory)
papyrus init

# 2. Query PubMed → staging/<ts>/
papyrus update

# 3. Screen candidates, then for each:
papyrus ingest <pmid>          # include
papyrus discard <pmid> --reason "review only"   # exclude

# 4. Convert, embed, index
papyrus convert <pmid>
papyrus embed <pmid>
papyrus index

# 5. Verify
papyrus check
```

Every command runs from the KB root (the directory containing `method.toml`)
or any subdirectory below it; the CLI walks up to locate the root
automatically.

## Commands

| Command | Purpose |
|---|---|
| `papyrus init [--force]` | Create a new KB tree |
| `papyrus update [--query-id <id>] [--related-to <pmid>] [--dry-run]` | Query PubMed → staging |
| `papyrus ingest <pmid> [--pdf <path>] [--via seed\|manual\|query]` | Include an article (from staging or direct seed/manual) |
| `papyrus discard <pmid> --reason "..." [--confidence 0.X]` | Exclude with reason |
| `papyrus defer <pmid>` | Park a staged candidate for later review |
| `papyrus revisit <pmid>` | Return a deferred candidate to staging |
| `papyrus convert <pmid>` | PDF → structured markdown + chunk index |
| `papyrus embed [<pmid>] [--all] [--pending] [--kind chunks\|distillation\|both]` | Chunk/embed text |
| `papyrus index` | Regenerate `concepts/index.json` |
| `papyrus refresh-meta [<pmid>] [--dry-run]` | Backfill volume/issue/pages/ISSN from PubMed |
| `papyrus check [--json]` | Run 13 integrity checks |
| `papyrus search <query> [--kind auto\|concepts\|distillations\|chunks] [--top-k 10] [--cite <citekey>]` | Semantic search |
| `papyrus store push\|pull\|check` | Manage external PDF storage (rclone) |
| `papyrus inbox list\|clean` | Manage the `pdfinbox/` staging directory |
| `papyrus notify [<digest>] [--latest] [--list] [--dry-run]` | Send a digest via email (Resend) |

All commands are idempotent, synchronous, and never block on input.

## The KB tree

```
intent.md                    # human-authored premise (the project intent)
method.toml                  # queries, filters, parameters
events.jsonl                 # append-only event log (JSON-lines)
pdfinbox/                    # staging area for incoming PDFs before ingest
updates/<ts>.md              # per-run narrative digest
staging/<ts>/<pmid>/          # candidates awaiting screening
  candidate.json
deferred/<pmid>/              # candidates parked for later
db/<pmid>/                    # permanent record (PMID-based canonical path)
  record.json                # CSL-JSON + custom process state
  abstract.txt               # plain-text abstract
  fulltext.pdf               # original PDF (gitignored; cached from external storage)
  fulltext.md                # structured markdown (after convert)
  chunk_index.json           # chunk boundary map
  chunks.jsonl               # per-chunk embeddings
  distillation.md            # concept-tagged summary
  distillation.json          # distillation embedding
concepts/<id>.md             # long-form concept article
  index.json                 # bidirectional PMID↔citekey↔concept map
```

## Core workflows

1. **Bootstrap** — write `intent.md`, run `papyrus init`, seed articles with
   `papyrus ingest <pmid> --via seed --pdf <path>`.
2. **Scheduled update** — `papyrus update` → screen candidates → `ingest` /
   `discard` / `defer` → `convert` → `embed` → `index` → `check` → write
   digest to `updates/<ts>.md`.
3. **Distillation** — read `fulltext.md`, write `distillation.md` with
   `[[concept-id]]` tags, then `papyrus embed <pmid> --kind distillation` and
   `papyrus index`.
4. **Concept curation** — create/split/merge/archive `concepts/*.md`, then
   `papyrus index` and `papyrus check`.

## Configuration

All configuration lives in `method.toml` at the KB root: PubMed queries and
relevance inclusion criteria, the PDF conversion engine, the embedding model/
provider/dimensions, and external fulltext storage (rclone remotes). Secrets
(API keys) come from environment variables or a `.env` file:

- `OPENAI_API_KEY` — OpenAI embedding provider
- `RESEND_API_KEY` — `papyrus notify` email sending

## Tech stack

- **Python 3.11+** with `uv` for packaging and dependency management
- **Typer** + **Rich** for the CLI
- **httpx** (sync) for PubMed E-utilities and provider APIs
- **NumPy** for in-memory cosine-similarity search
- **pymupdf4llm** for PDF conversion
- **mistune** for markdown handling, **tomlkit** for `method.toml`, **python-dotenv** for `.env`
- **pytest** for testing

## Project layout

```
papyrus/
  pyproject.toml
  src/papyrus/
    cli.py                     # typer app, command registration
    commands/                  # one module per CLI command
    core/                      # shared utilities (config, record, citekey,
                               #   pubmed, chunking, embedding, searchlib, …)
  docs/                        # design & stack notes
  .agents/skills/papyrus/      # agent skill + reference docs
```

## License

Not yet licensed.
