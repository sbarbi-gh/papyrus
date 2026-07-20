# PubMed Knowledge Base Manager — Design

**Schema version:** `1.0.0`

A single-folder, git-versioned knowledge base manager that ingests PubMed
sources, screens them for relevance, converts full-text to structured markdown,
distills each article into a concept-aware summary, curates a concept map, and
serves semantic queries over chunked sources and distilled articles — all from a
self-contained directory with no global registry.

### Scope and layers

Three layers stack; this document describes all three.

- **`intent.md`** — premise. The purpose of the wiki project, human-authored,
  above everything. Read by both the feeder and the KB manager.
- **This standard** — the portable substrate: how sources are found, screened,
  acquired, converted, distilled, indexed, and searched. Wiki-agnostic, survives
  a change of KB backend.
- **The KB manager** — a field-expert agent that executes the procedures defined
  here. It can be backed by a local LLM, a remote API, or a human; the standard
  only specifies inputs, outputs, and invariants.

The seam is that everything lives in a single folder tree, git-tracked, with no
external database. Move the folder and the entire KB — including its search
index — moves with it.

The manager is not a daemon. It is invoked with a task (§9) and runs to
completion. An optional server process (§20) can be started from the folder for
interactive semantic search.

---

## 1. Founding principles

Three axes structure the design:

- **Deterministic vs judgement** — fetch, dedup, format, chunk, embed are
  deterministic shell + scripts. Relevance screening, distillation, and concept
  curation require judgement (LLM or human).
- **Premise vs derivative** — `intent.md` is the human-declared axiom.
  `method.toml` is derived from it + corpus, co-managed.
- **Stable vs temporal** — stable facts live in records and concept files;
  temporal facts live in `events.jsonl`. Never the reverse.

### Two keys, two roles

| Key          | Role                       | Mutability                              |
|--------------|----------------------------|-----------------------------------------|
| **PMID**     | Internal primary key       | Immutable by nature. Every join/dedup/lookup anchors here. |
| **Cite key** | External primary key — the contract with LaTeX documents | Generated **once** at record birth, then **frozen**. |

### The portable-folder invariant

The KB tree is self-contained. There is no global database, no `/var/lib`, no
service registry. Embeddings, the concept index, the event log — everything is a
file within the tree. If you `cp -r` the folder, you copy the entire KB.

When a server process is needed for search performance (§20), it is started from
within the folder, reads its state from the tree on startup, and writes nothing
outside it.

---

## 2. Folder structure

```
intent.md                # premise: the scope of the base, human-authored prose
method.toml              # derivative: queries, filters, parameters — co-managed
events.jsonl             # append-only structured event log (JSON-lines)
updates/                 # one timestamped digest per update run (narrative)
  2024-03-15T0900Z.md
staging/                 # pending screening decisions, decoupled from db/
  2024-07-20T0900Z/       # one folder per update run, deletable once triaged
    38471234/
      candidate.json       # {pmid, title, abstract, authors, doi, year, ...}
db/
  38471234/
    record.json           # CSL-JSON bibliographic metadata + process state
    abstract.txt          # raw abstract, kept out of the record
    fulltext.pdf          # preferred source for human consumption
    fulltext.md           # structured markdown, converted from PDF/source
    chunk_index.json      # {chunk_id, start_byte, end_byte} mapping into fulltext.md
    chunks.jsonl          # one line per chunk: {chunk_id, text, embedding, model, dimensions}
    distillation.md       # distilled summary, concept-tagged, human+LLM authored
    distillation.json     # {text, embedding, model, dimensions} — unchunked
    supp/
concepts/
  renal-carcinoma.md          # long-form concept article (org-mode or markdown)
  gene-expression-networks.md
  index.json                  # bidirectional map: concepts ↔ PMIDs, cite_keys → PMID
embeddings/                   # optional consolidated search indexes (regenerated)
  sources.lance               # consolidated chunk embeddings for fast scan
  distillations.lance
  concepts.lance
server/                       # optional search server
  server.py                   # reads tree, serves /search, /verify, /concept
```

Every unit (article, concept) is a folder or file at a predictable path. There
is no central database; views are derived by scanning the tree or by reading an
optional consolidated index. The consolidated index is always a cache, never
authoritative — it can be deleted and regenerated from the source files.

---

## 3. The record

Format is **CSL-JSON** with a `custom` block for process state. The
bibliographic part is standard CSL; process state is namespaced so it never
collides with export.

```json
{
  "schema_version": "1.0.0",
  "id": "smith2024a",
  "type": "article-journal",
  "title": "…",
  "author": [{ "family": "Smith", "given": "J." }],
  "issued": { "date-parts": [[2024, 3, 15]] },
  "DOI": "10.xxxx",
  "custom": {
    "pmid": "38471234",
    "status": "included",
    "references": ["38559001", "37990022"],
    "citekey_aliases": [],
    "materials": {
      "fulltext": "present",
      "fulltext_md": "present",
      "supp": false,
      "distillation": "present",
      "chunks": 18,
      "embeddings_model": "text-embedding-3-small",
      "embeddings_dim": 1536
    },
    "concepts": ["renal-carcinoma", "gene-expression-networks"]
  }
}
```

### What the record holds

Everything **temporal** lives in the event log (§7), not on the record. A query
matching a record is an *event*, not a property of the record. The rule:

> A record is never mutated by query activity. Query activity is logged. The
> only things that mutate a record are a **state transition** (screening
> decision, full-text acquisition, ingestion) or a **metadata correction**.

What the record holds is either stable (bibliographic data, frozen cite key,
outgoing references) or a small current state. Two independent axes make up that
state:

- **`status`** — the inclusion verdict: `included` / `discarded` / `excluded`.
  This alone determines KB membership.
- **`materials.fulltext`** — the acquisition state: `absent` / `present` /
  `unavailable`. Never gates inclusion (§9).

`status` is a denormalization of the last inclusion event — kept on the record
because it drives the pipeline and is read constantly — with the event log as its
audit trail.

The `materials` block tracks the state of derived artifacts (`fulltext_md`,
`distillation`, `chunks`, `embeddings_model`, `embeddings_dim`) alongside the
PDF. The `concepts` field is a denormalized cache of concept IDs this article is
tagged with; the authoritative source is `concepts/index.json`.

### Rationale for what stays vs what leaves

- **State-as-derived-view.** The "state lists" (included, discarded, full-text
  outstanding) are recomputed on the fly over ~200 records with a
  `jq | select`. Only the scalars in `materials` are materialized.
- **Payload outside the record.** Abstracts and PDFs are heavy data, not
  bibliographic metadata. Keeping them out keeps `record.json` light, diffable,
  and free of escaping problems.
- **Temporal facts in the log, not the record.** Query and ingestion history
  lives in the event log, keeping the "store the immutable, derive the mutable"
  principle (§5) uniform: the record holds identity and current state, the log
  holds history.
- **BibTeX is output, not storage.** The `.bib` is regenerable on demand from
  records filtered on `included`. Pandoc **reads** the cite key from `id`; it
  never recomputes it.

---

## 4. Cite key: birth and immutability

Generated **exactly once**, when a PMID first enters the KB, then treated as
persistent data on par with the PMID.

- **Base**: first-author family name + year.
- **Disambiguation**: suffix `a/b/c`, resolved against the set of `id` values
  already present in the DB (not against a regenerated `.bib`).
- **Freeze**: written to `id`, never touched again.

Edge cases fixed up front:

- **Missing author** → fall back to container-title token, else `pmidNNN`.
- **Missing year** → `nd` (no date).
- **Collisions past `z`** → continue with `aa`, `ab`, …

**Invariant:** a cite key, once emitted, is never silently changed. If a merge
must change it, it declares so (§9).

---

## 5. The graph: store the immutable, derive the mutable

- **Outgoing references** — a published article's bibliography is fixed.
  Persisted in `custom.references`.
- **Cited-by** — grows over time, never immutable. **Never persisted.**
  - *Internal* cited-by is derived: "who among the known PMIDs lists X in their
    references".
  - *External* cited-by is pulled from NCBI (`elink`) at each run.

The internal graph is always recomputed from the intersection of references with
known PMIDs (the folder names): dynamic by construction, never stale.
Data-driven expansion — counting external cited-by frequencies (relevance
expansion) or mining corpus terms (query generation) — is transient and lives in
`scratch/`, where mutability is confined and disposable.

Pending screening decisions live in `staging/` — timestamped run folders each
containing one folder per candidate. Staging is payload-heavy (full abstracts)
and ephemeral (deletable once triaged), keeping the event log clean of transient
payload. Each candidate is a self-contained folder, so `ingest` and `discard`
remove candidates atomically without editing a shared file.

### The concept graph

Concepts link to PMIDs (through `concepts/index.json`) and may link to each
other (`related` field in concept files). This graph is small (order of 50–100
nodes) and is represented explicitly, not derived, because it is curated
human+LLM judgement.

---

## 6. Intent and method

Two first-class configuration artifacts, on the premise/derivative axis (§1).

### `intent.md` — the premise

Descriptive, authorial prose stating the purpose of the **whole wiki project**,
not merely the update policy. Example:

> This knowledge base tracks the history and developments of *problem X*, in
> particular in relation to *aspect Y*. It is not a general survey; it favors
> primary studies over reviews. As a rough orientation, animal models are
> usually out of scope.

Properties:

- **Human-authored, human-only.** The agent never writes it.
- **Above both consumers.** A premise has no upstream, so it sits above both the
  wiki and this feeder. The wiki manager asks it "what is this base about"; the
  feeder asks it "what should I look for". One axiom, two questions — they cannot
  conflict because neither owns it.
- **Nearly immutable** — changes only when the *purpose* of the project changes.
- **Not read by the deterministic steps.** The mechanical pipeline does not
  parse `intent.md`; the judging agent does. Within the feeder its concrete
  roles are:
  1. **Bootstrap** — the seed from which the first `method.toml` is derived.
  2. **Organizing the non-periodic revision** of queries and filters — the
     conversational or event-triggered interventions (§16).
- **May, but need not, carry discard orientation** — high-level prose guidance
  ("animal models usually out of scope"), which informs the human and the agent
  when deriving filters. It is direction, not an operational rule.

### `method.toml` — the derivative

Queries, filters, screening parameters, and KB manager settings. Deduced from
`intent.md` + corpus, co-managed by human and agent. Format is **TOML**: PubMed
query strings carry boolean operators, quotes, brackets and field tags — TOML's
literal multi-line strings (`'''…'''`) hold them without the quoting traps YAML
sets on exactly this content.

```toml
schema_version = "1.0.0"

[settings]
db_path         = "./db"
primary_db      = "./db"
chunk_size      = 60          # abstracts per LLM screening call
unpaywall_email = "…"

[fulltext]
max_attempts    = 5
backoff         = "exponential"
keep_on_wishlist = true

[relevance]
inclusion = '''
Include studies on X in adult populations.
Exclude animal models, case reports, non-English.
'''
min_confidence = 0.6

[source_conversion]
engine          = "marker"    # PDF → MD conversion tool
fallback        = "pymupdf"   # if marker fails
max_pages       = 100         # skip overly long PDFs
image_handling  = "reference" # extract images, reference in MD

[chunking]
method          = "section"   # section-aware, fallback to fixed-size
target_bytes    = 2048        # ~500 tokens
overlap_bytes   = 256         # ~64 tokens
max_chunks_per_article = 80

[embeddings]
model           = "text-embedding-3-small"
dimensions      = 1536
provider        = "openai"    # or "local" for self-hosted
batch_size      = 50          # embeddings per API call

[distillation]
style           = "concept-tagged"
max_length      = 2000        # words
required_sections = ["claim", "methods", "findings", "concepts", "relation-to-corpus"]

[concepts]
max_concepts    = 150
min_articles_per_concept = 2
allow_orphan    = false       # auto-archive concepts with zero linked articles

[search]
consolidated_index = true     # regenerate embeddings/*.lance after batch updates
index_format    = "lance"

[[queries]]
id      = "q1"
enabled = true
term    = '''("topic A"[Title/Abstract] OR "synonym"[Title/Abstract]) AND 2020:2026[dp]'''

[[queries]]
id      = "q2"
enabled = false               # disable without deleting (saturated query)
term    = '''"topic B"[MeSH] AND humans[Filter]'''
```

- **Stable explicit `id` per query** — the key linking a query to its events in
  the log. Never derive it from the term: if the term changes, the id persists
  and history stays coherent.
- **`enabled`** instead of deletion — disabling a saturated query preserves its
  historical link to the events it produced.

### Three levels of "discard reason"

The word "reason" maps to three distinct things at three layers:

| Level | Where | Nature |
|-------|-------|--------|
| Orientation | `intent.md` prose | high-level direction, optional, guides humans + derivation |
| Rule | `method.toml` `inclusion` | operational criterion the KB manager applies at screening |
| Fact | `events.jsonl` discard event | the factual `reason` + `confidence` of one specific discard |

---

## 7. Event log

`events.jsonl` is the single home of everything temporal: append-only,
one JSON object per line, `jq`-friendly, git-tracked. Every mode of entry writes
an event, including manual ones.

```
{"schema_version":"1.0.0","ts":"…","kind":"query-run","query_id":"q1","n_found":42,"n_new":7}
{"schema_version":"1.0.0","ts":"…","kind":"query-match","query_id":"q1","pmid":"38471234"}
{"schema_version":"1.0.0","ts":"…","kind":"ingest","pmid":"38471234","via":"query"}
{"schema_version":"1.0.0","ts":"…","kind":"ingest","pmid":"37990022","via":"seed"}
{"schema_version":"1.0.0","ts":"…","kind":"discard","pmid":"38550010","reason":"animal model","confidence":0.94}
{"schema_version":"1.0.0","ts":"…","kind":"fulltext-attempt","pmid":"38471234","result":"fail","attempt":2}
{"schema_version":"1.0.0","ts":"…","kind":"fulltext-acquired","pmid":"38471234","via":"human"}
{"schema_version":"1.0.0","ts":"…","kind":"fulltext-unavailable","pmid":"38550777","attempts":5}
# Source conversion
{"schema_version":"1.0.0","ts":"…","kind":"source-converted","pmid":"38471234","engine":"marker","pages":12,"n_chunks":14}
# Distillation
{"schema_version":"1.0.0","ts":"…","kind":"distilled","pmid":"38471234","model":"gpt-4o","n_concepts_tagged":3}
# Embedding
{"schema_version":"1.0.0","ts":"…","kind":"embedded","pmid":"38471234","model":"text-embedding-3-small","kind":"source_chunks","n_vectors":14}
{"schema_version":"1.0.0","ts":"…","kind":"embedded","pmid":"38471234","model":"text-embedding-3-small","kind":"distillation","n_vectors":1}
# Concepts
{"schema_version":"1.0.0","ts":"…","kind":"concept-created","concept_id":"renal-carcinoma"}
{"schema_version":"1.0.0","ts":"…","kind":"concept-linked","concept_id":"renal-carcinoma","pmid":"38471234"}
{"schema_version":"1.0.0","ts":"…","kind":"concept-merged","winner":"renal-carcinoma","loser":"kidney-cancer"}
{"schema_version":"1.0.0","ts":"…","kind":"concept-archived","concept_id":"obsolete-topic"}
# Maintenance
{"schema_version":"1.0.0","ts":"…","kind":"re-index","reason":"model-change","old_model":"text-embedding-ada-002","new_model":"text-embedding-3-small","n_articles":180}
{"schema_version":"1.0.0","ts":"…","kind":"integrity-check","result":"pass","checks":12}
```

Consequences:

- **Provenance is a log projection.** "Give me all seeds", "what did q1
  produce", "when did this paper enter" are all `jq` queries over the log. The
  `via` attribute on `ingest` events (`seed`, `manual`, `query`, `snowball_*`)
  records how a paper entered; its main consumer is trust-weighting for query
  generation (a human seed is ground truth), which reads `via` directly.
- **Last-run date is derived**:
  ```bash
  jq -r --arg q q1 'select(.kind=="query-run" and .query_id==$q) | .ts' events.jsonl | tail -1
  ```
- **A query matching a known record is informative, not mutating.** It writes a
  `query-match` event and touches no record. A match on an `included` paper is a
  recall signal validating the query; a match on a `discarded` paper is a
  re-screening candidate (§16). `query-match` events carry only `pmid +
  query_id + ts` — payload (title, abstract, authors) lives in the staging
  candidate folder (§9), not the event log.
- **The discard event carries `reason` + `confidence`.** Required by the open
  re-screening problem (§16): without it, selective re-screening is blind.
- **Full-text acquisition is event-driven, never blocking.** `fulltext-attempt`,
  `fulltext-acquired` (with `via: auto | human`), and `fulltext-unavailable`
  record the async lifecycle. The current full-text state is read from
  `materials.fulltext`; the log holds how and when it got there, including the
  attempt count that drives retry backoff.
- **Source conversion, distillation, embedding, and concept curation** each
  produce their own event kinds, preserving a complete audit trail of every
  derived artifact.

### Updates digest — companion to the log

`events.jsonl` records *what happened*; `updates/<timestamp>.md` records *what it
means*. One narrative file per update run, authored by the KB manager,
human-facing. It is not an operation log — it is the interpretation a run
surfaces:

- new articles worth attention and why;
- common threads across the batch (recurring methods, converging findings, an
  emerging sub-topic);
- suggestions for criteria revision — where the screening results hint that a
  query or filter is too broad, too narrow, or missing a term.

The digest is the surface where the non-periodic revision of §16 is proposed in
prose: the human reads it and decides whether to open a `method.toml` branch.
Properties that keep it from becoming a second source of truth:

- **Derived, never authoritative.** Nothing reads a digest to drive behavior; it
  informs a human. It could be deleted without affecting a single downstream
  computation. Every fact it references is independently in the log or the
  records.
- **Immutable once written.** One file per run, timestamped, never edited — a
  digest is a snapshot of what the base looked like at that moment. Revisions
  happen by writing the next run's digest, not by rewriting a past one.
- **Prose, not schema.** No `schema_version`, no fields to migrate. It is read by
  people, so its only contract is legibility.

---

## 8. Git as history, audit, and promotion

The whole tree is version-controlled. Git provides three things no other layer
does, and deliberately does **not** provide three others.

**Git gives:**

- Full history, revert, and rationale-in-prose via commit messages.
- Auditability of *everything*, including manual ingestions — each is a commit
  (`git log --follow db/38471234/`).
- The promotion mechanism for `method.toml` (below).

**Git does not give (and these stay separately provided):**

- **Structured, queryable provenance** — a prose commit message is not
  `jq`-queryable. That is the event log's job; the two are complementary
  (audit-in-prose vs data-as-facts), not redundant.
- **Runtime concurrency control** — git is not a lock. If scheduler and manual
  run can overlap, a lock file (`flock`) is still required.
- **Effect gating by itself** — see the promotion model.

### Promotion model (gating)

The gating question splits along the premise/derivative axis:

- **`intent.md`** — human-only. No gating needed; the agent never writes it.
- **`method.toml`** — co-managed, so gating applies, and the choice is
  **branch + human merge**. The agent proposes commits on a branch with the
  rationale in the message; the pipeline reads `method.toml` from the human-
  controlled ref (`main`); the human merge *is* the promotion.
- **Concept files** — co-managed; changes proposed on a branch, merged by human.
- **`index.json`** — regenerated by deterministic code; never manually edited.
- **Embeddings** — regenerated by deterministic code; never manually edited.

Rationale for choosing ref-over-working-tree: because the premise is isolated and
protected in `intent.md`, all residual risk lives in the reversible derivative. A
light gate (merge) on the derivative — rather than auto-live edits — reproduces
the same "LLM proposes, human decides" semantics used for query generation,
unified under git.

---

## 9. Deterministic commands and the agent workflow

The KB manager is a CLI tool (`papyrus`) that exposes **deterministic commands
only**. Each command is atomic, idempotent, and communicates through the
filesystem tree. Judgement steps — screening, distillation, concept curation —
are performed by an **agent** (LLM or human) that reads files the CLI produces
and invokes the next command.

The full set of commands and their signatures is specified in
[`cli-scaffold.md`](cli-scaffold.md). This section describes the architectural
concepts that shape them.

The communication protocol between CLI and agent is the filesystem. No command
blocks on input; every command runs to completion.

### Staging: the handoff from queries to screening

`staging/` holds candidate articles that have been fetched and enriched but not
yet screened. Each update run produces a timestamped run folder with one
sub-folder per candidate, mirroring the `db/` structure:

```
staging/
  2024-07-20T0900Z/
    38471234/
      candidate.json    # {pmid, title, abstract, authors, doi, year, ...}
    38559001/
      candidate.json
```

Each candidate is a self-contained folder, so ingestion and discard remove
candidates atomically (delete one folder) without editing a shared file. Staging
folders are **payload-heavy** (full enrichment — everything the agent needs to
screen) and **ephemeral** — a run folder is deleted once all its candidates have
been triaged. The directory listing of `staging/` is the **single source of truth
for "pending decision"**: `find staging -name candidate.json | wc -l` gives the
pending count instantly, decoupled from the event log and permanent records.

The event log records `query-run` and `query-match` events as before, but
`query-match` carries only `pmid + query_id + ts` — just enough to answer "what
did this query produce" without duplicating payload.

### Deterministic vs judgement split

The deterministic layer handles everything that can be automated without
reasoning:

- Querying PubMed and enriching candidates (→ staging).
- Creating records with frozen cite keys (→ `db/`).
- Converting PDFs to structured markdown (→ `fulltext.md`).
- Chunking and embedding text (→ `chunks.jsonl`, `distillation.json`).
- Regenerating derived indexes (→ `concepts/index.json`).
- Running integrity checks.
- Semantic search over the tree.

Each deterministic command is idempotent — it checks whether its output already
exists and is up-to-date (by comparing `schema_version` stamps or content hashes)
and skips if so.

The judgement layer — screening candidates, distilling articles, curating
concepts, writing digests — is performed by an agent. The agent reads staging
folders to decide what to ingest or discard; it reads `fulltext.md` to write
`distillation.md` and update concept tags; it writes `updates/<ts>.md` digests.
The CLI provides the deterministic scaffolding; the agent provides the reasoning.

### Full text is decoupled from inclusion

Inclusion (the relevance verdict, `status`) and full-text availability
(`materials.fulltext`) are independent axes. A relevant paper is handed off
immediately at abstract level (`status: included`, `fulltext: absent`); full text
is enrichment acquired asynchronously, and its absence never holds up the handoff
or the next run.

Acquisition has two non-blocking channels:

- **Automatic** — attempted at ingestion and **re-attempted at each run's
  intake**, since availability changes over time (embargo lifts, an OA copy
  appears). Governed by a retry cap + backoff in `method.toml`; after exhaustion
  the record is marked `fulltext: unavailable` (soft-terminal — still included,
  auto-retry stops, a human drop can still override it).
- **Human drop** — the human places a PDF in the record's folder. Never a
  pipeline step; surfaced only as a **standing recommendation** and absorbed by
  the next intake.

Source conversion, chunking, embedding, and distillation all wait on
`materials.fulltext: present`.

### Screening gate and its bypass

The screening gate is bypassed **only by human-chosen ingestion** (`via: seed`
or `via: manual`): if a human put it in by hand, it is ground truth, not a
candidate for judgement. Everything arising from an automatic mechanism (query,
snowballing) passes through screening — via the staging → ingest/discard flow.
Thus `included` is reached by two paths: through screening, or directly by human
choice. `status` says *whether* it is in; the log's `ingest.via` says *how* it
got there.

### Related-by expansion

The update command supports an alternative mode: instead of query-based search,
it uses PubMed's ELink neighbor expansion (`cmd=neighbor` / pmra "related
articles", plus `citedin` and `refs`). It seeds from a given PMID, unions
neighbor PMIDs, dedups against known and staging PMIDs, enriches, and writes
staging folders — same output format as a query-based update. The agent screens
the staging folder as usual. This is corpus-relative expansion (§16), fired when
the agent judges an article pivotal or after a query/filter revision.

### Bootstrap mode

1. Human writes `intent.md` (the project's purpose) and seeds the base (bypassing
   screening).
2. Agent reads intent + seed corpus → **derives** a first `method.toml`,
   proposes it on a branch.
3. Human ratifies (merge).
4. Periodic updates trace new papers, the agent screens candidates, and the
   corpus grows while `intent.md` stays fixed.

### Wake-up and scheduling

The CLI is stateless and called explicitly. An **external clock** (cron or agent
scheduling) invokes commands. The layers are: clock → command → artifacts. The
agent reads staging output and decides what to call next.

---

## 10. Source conversion

### Pipeline

```
fulltext.pdf  ──[marker / pymupdf]──>  fulltext.md
                                          │
                                          ▼
                                     chunk_index.json   (byte offsets)
                                          │
                                          ▼
                                     chunks.jsonl        (text + embedding)
```

### `fulltext.md` — the converted source

The preferred format is a well-structured markdown file produced from the PDF
(or other full-text source). It is the **primary artefact for LLM consumption**
(distillation, chunking, concept extraction). The PDF remains the preferred
format for human reading and graphics.

Requirements for the converter:

- Preserve section structure (Abstract, Introduction, Methods, Results,
  Discussion, References) as markdown headings.
- Extract tables as markdown tables (best-effort).
- Extract image captions and reference images by filename in `supp/`.
- Strip reference lists into a compact format or omit (references are already in
  `record.json`).
- Handle multi-column layouts and line-break hyphenation.

Configured in `method.toml` `[source_conversion]`. If the primary engine fails
(e.g., corrupted PDF), the fallback is tried. If both fail, the event log
records the failure and the article sits at `fulltext_md: absent` — it can still
be included, but cannot be chunked or distilled until a human provides a
converted source.

### `chunk_index.json`

Maps each chunk to byte offsets in `fulltext.md`, enabling the verification
workflow (§14) to retrieve the surrounding context of a match.

```json
{
  "schema_version": "1.0.0",
  "source_file": "fulltext.md",
  "source_hash": "sha256:abc123…",
  "chunk_method": "section",
  "chunks": [
    {"chunk_id": "00001", "start_byte": 0, "end_byte": 2048, "section": "Introduction"},
    {"chunk_id": "00002", "start_byte": 2049, "end_byte": 4096, "section": "Introduction"}
  ]
}
```

### `chunks.jsonl`

Append-only JSON lines, one per chunk. Embeddings are stored inline.

```json
{"chunk_id":"00001","text":"The role of gene X in renal carcinoma has been…","embedding":[0.0123,-0.0456,…],"model":"text-embedding-3-small","dimensions":1536}
```

---

## 11. Distillation artifacts

Each included article with a converted fulltext source gets a **distillation** —
a structured summary authored by the KB manager (LLM) with optional human
review. It is the article's entry in the KB's own voice.

### `distillation.md`

Human-readable, concept-tagged markdown. Sections are prescribed by
`method.toml` `[distillation].required_sections`:

```markdown
# Smith et al. (2024) — Distillation

## Claim
This study demonstrates that gene X is a master regulator of renal carcinoma
progression through the Y signaling pathway.

## Methods
- Single-cell RNA-seq of 45 tumor samples (clear cell RCC)
- CRISPR knockout of gene X in 3 cell lines
- Murine xenograft model (n=24)

## Findings
- Gene X overexpression in 89% of tumor samples vs matched normal (p<0.001)
- Knockout reduced proliferation by 72% in vitro
- Xenograft tumor volume down 64% with gene X silencing
- Identified 12 downstream targets; 6 are known oncogenes

## Concepts
- [[renal-carcinoma]]
- [[gene-expression-networks]]
- [[crispr-screens]]

## Relation to corpus
This is the first study linking gene X specifically to clear cell RCC.
Contrasts with Doe et al. (2023) who found gene X in papillary RCC only.
The signaling pathway aligns with findings from the Lee group (2022–2024).
```

### `distillation.json`

The distillation text plus its unchunked embedding, used for semantic search at
the concept/distillation level.

```json
{
  "schema_version": "1.0.0",
  "pmid": "38471234",
  "text": "This study demonstrates that gene X is a master regulator…",
  "embedding": [0.0123, -0.0456, …],
  "model": "text-embedding-3-small",
  "dimensions": 1536
}
```

The distillation embedding is a **single vector** for the entire distillation
text — no chunking. This vector represents "what this article is about" and is
searched when answering concept-level queries (§14).

### Distillation is downstream of the handoff

A paper enters the KB at step 6 (handoff). Distillation happens later, after
source conversion, and is independent of screening. A paper can be `included`
and `distillation: absent` indefinitely — it is searchable by metadata and
abstract, just not yet semantically at the distillation level.

---

## 12. Concepts and the semantic index

### Concepts as first-class artifacts

A concept is a curated topic that spans multiple articles. It is represented as
a markdown (or org-mode) file in `concepts/` and is manually or agent-authored.

```markdown
# Renal carcinoma

## Definition
Renal cell carcinoma (RCC) is a kidney cancer that originates in the lining
of the proximal convoluted tubule. The most common subtype is clear cell RCC
(ccRCC, ~75% of cases).

## Key genes
- VHL (biallelic inactivation in ~90% of ccRCC)
- PBRM1, SETD2, BAP1 (chromatin remodeling, frequently co-mutated)
- [[gene-X]] (emerging; see corpus)

## In this corpus
- 23 articles link to this concept
- Dominant themes: immunotherapy response (12 articles), metabolic
  reprogramming (7), diagnostic biomarkers (4)
- Key open question: the role of gene X in ccRCC vs papillary subtypes

## Related concepts
- [[immunotherapy]]
- [[chromatin-remodeling]]
- [[cancer-metabolism]]
```

### `concepts/index.json`

A single file at `concepts/index.json` that is the authoritative bidirectional
map. Regenerated deterministically from the concept files and record tags. Never
hand-edited.

```json
{
  "schema_version": "1.0.0",
  "concepts": {
    "renal-carcinoma": {
      "pmids": ["38471234", "38559001", "37990022"],
      "related": ["immunotherapy", "chromatin-remodeling"],
      "n_articles": 23,
      "last_updated": "2024-03-15T09:00:00Z"
    },
    "gene-expression-networks": {
      "pmids": ["38471234", "38550010"],
      "related": ["renal-carcinoma"],
      "n_articles": 5,
      "last_updated": "2024-03-14T14:00:00Z"
    }
  },
  "citekey_index": {
    "smith2024a": "38471234",
    "doe2023b": "37990022"
  },
  "pmid_to_citekey": {
    "38471234": "smith2024a",
    "37990022": "doe2023b"
  }
}
```

### Concept lifecycle

1. **Creation** — the distillation step (or a human) proposes a new concept tag.
   If the concept file doesn't exist, a stub is created and flagged for review.
2. **Linking** — a PMID gains a concept tag; the index is regenerated.
3. **Splitting** — a concept grows too broad (e.g., "immunotherapy" becomes
   "immune-checkpoint-inhibitors" and "car-t-therapy"). Proposed by the agent at
   concept-review time.
4. **Merging** — two concepts are discovered to be synonyms. One wins, the other
   becomes an alias. Logged as `concept-merged` in the event log.
5. **Archiving** — a concept with zero linked articles (after dedup/merge) is
   moved to `concepts/.archived/`. Configurable via `allow_orphan`.

### The index is always derivable

`concepts/index.json` can be deleted and fully regenerated from:

1. Concept files in `concepts/` (for related links and metadata)
2. `record.json` files in `db/*/` (for `custom.concepts` → PMID mappings)
3. `db/*/record.json` `id` fields (for cite key mappings)

This means a corrupted or stale index is never a data-loss event.

---

## 13. Embeddings as records

### Design principle

Embeddings are stored alongside their source text, within the same folder as the
article or concept they describe. They are **records**, not database rows in an
external system. This preserves the portable-folder invariant: move the tree,
and all embeddings come with it.

### Storage format

Three embedding types, each stored differently:

| Type | Location | Format | Granularity |
|------|----------|--------|-------------|
| Source chunk embeddings | `db/<pmid>/chunks.jsonl` | JSONL (one per chunk) | ~500 tokens each |
| Distillation embeddings | `db/<pmid>/distillation.json` | JSON (single vector) | entire distillation |
| Concept embeddings | `concepts/<concept_id>.json` | JSON (single vector) | entire concept article |

### Concept embedding file

```
concepts/
  renal-carcinoma.md
  renal-carcinoma.json        # {text, embedding, model, dimensions}
```

The `.json` companion to each concept `.md` stores the embedding of the
concept's definition + corpus summary text. This is searched when answering
concept-level queries.

### Format specification

```json
{
  "schema_version": "1.0.0",
  "pmid": "38471234",                 // absent for concept embeddings
  "concept_id": "renal-carcinoma",    // absent for article embeddings
  "text": "full text that was embedded…",
  "embedding": [0.0123, -0.0456, …],
  "model": "text-embedding-3-small",
  "dimensions": 1536,
  "generated": "2024-03-15T09:00:00Z"
}
```

The `text` field stores the exact text that was embedded, enabling re-embedding
after a model change without re-chunking or re-distilling. It also makes the
embedding file self-contained: a human or agent can inspect what a vector
represents without consulting the source file.

### Regeneration

Embeddings are regenerated deterministically when:
- The embedding model changes (new `method.toml` `[embeddings].model`)
- The chunking parameters change
- A distillation is rewritten

Regeneration reads `text` from the existing embedding file, re-embeds it with
the new model, and overwrites the `embedding` field. It does not re-chunk or
re-distill.

### Scale

At 200 articles × 15 chunks/article = 3,000 chunk vectors + 200 distillation
vectors + 100 concept vectors ≈ 3,300 vectors total. At 1536 dimensions, this is
~20 MB of float32 data. JSON serialization bloats this to ~100 MB, which is
acceptable. If the KB grows by an order of magnitude, switching to a binary
format (Lance) in the consolidated index becomes necessary, but the
source-of-truth files remain JSONL for portability.

### Consolidated index (optional)

`embeddings/` holds regenerable binary indexes for fast search:

```
embeddings/
  sources.lance        # all chunk embeddings, source-ID-keyed
  distillations.lance  # all distillation embeddings
  concepts.lance       # all concept embeddings
```

These are caches, never authoritative. They are regenerated deterministically from
the source embedding files when the embedding model changes.

The server process (§20) reads these on startup for fast search. If they don't
exist, it builds them in-memory from the source JSONL files (slower startup,
same results).

---

## 14. Semantic search and verification

### Two search tiers

The KB exposes two search modes, corresponding to two levels of granularity:

| Tier | Scope | Use case | Method |
|------|-------|----------|--------|
| **Concept search** | Concept embeddings + distillation embeddings | "Is gene X involved in renal carcinomas?" | Cosine similarity against ~300 vectors |
| **Evidence search** | Source chunk embeddings | "Verify this citation: Smith et al. showed that…" | Cosine similarity against ~3,000 vectors, with context retrieval |

### Tier 1: Concept search ("Is gene X involved in…")

1. Embed the query with the same model.
2. Compute cosine similarity against:
   - All concept embeddings (`concepts/*.json`)
   - All distillation embeddings (`db/*/distillation.json`)
3. Return ranked results with scores, concept IDs, and PMIDs.
4. If a top-ranked result is a distillation, the agent can read
   `distillation.md` for detail. If it's a concept, the agent reads the
   concept's `.md` file and follows links to PMIDs.
5. If the answer is not clear from concepts and distillations, the agent can
   escalate to Tier 2.

This tier answers quickly (300 dot products, sub-millisecond) and requires no
chunk-level retrieval. It is the default entry point for conceptual queries.

### Tier 2: Evidence search ("Verify this citation")

1. The user provides a claim + citation (e.g., "Smith et al. 2024 showed gene X
   knockdown reduces tumor volume by 64% in xenografts").
2. Resolve the citation to a PMID via `concepts/index.json` citekey index.
3. If resolved, load the article's `chunks.jsonl`, embed the claim, compute
   cosine similarity against all chunks, and retrieve the top-k matching chunks.
4. For each matching chunk, use `chunk_index.json` to retrieve the surrounding
   section from `fulltext.md` (expand to the containing section for context).
5. Present the matched passages with scores, section context, and a verdict:
   - **Supported** — passage directly corroborates the claim.
   - **Partially supported** — passage is topically related but doesn't fully
     confirm.
   - **Contradicted** — passage suggests the opposite.
   - **Not found** — no passage reaches the similarity threshold.
6. If the PMID is resolved but the article has no chunks (no fulltext
   converted), fall back to abstract search.
7. If the citation cannot be resolved (ambiguous cite key, or article not in
   KB), search across all articles' `abstract.txt` and `record.json` titles as a
   fallback.

### Verification accuracy and failure modes

- **The chunk is the retrieval unit, but context is the presentation unit.** A
  matching chunk may capture a sentence fragment; the surrounding section
  provides the necessary context for judgement.
- **The verdict ("supported" / "not found") is a judgement call** — the agent
  reads the retrieved passages and decides. This is a Judgement step, not
  deterministic.
- **Negative results are informative.** "Not found" means the claim is absent
  from the full text, not that it's false. The user interprets accordingly.

### Cross-article search

If no specific citation is given ("What does the corpus say about gene X and
renal carcinoma?"), the query is embedded and searched against:

1. All concept embeddings (first, for a map of relevant topics)
2. All distillation embeddings (second, for articles that discuss this)
3. If depth is needed, the top-k articles' chunk embeddings (third, for
   specific passages)

The three-level cascade (concepts → distillations → chunks) mirrors the
knowledge hierarchy: broad topics → article summaries → detailed evidence.

---

## 15. Merge of two bases

A merge is a **union of folders** with reconciliation of conflicts only, in
fixed order:

1. **Identity (PMID)** — same folder in both DBs ⇒ same paper.
2. **Cite key (with mandatory report)**:
   - *Same PMID, diverging keys*: the primary DB's key survives; the loser goes
     to `custom.citekey_aliases`.
   - *Different PMIDs, same key (collision)*: the non-primary DB's record is
     renamed, old key to `citekey_aliases`. The merge **emits a report** of every
     rename.
3. **State and materials**: `status` = max on `included > discarded` (`excluded`
   is sticky — a manual blacklist in either DB wins), `materials` = best across
   all fields (e.g., `materials.fulltext` = max on `present > unavailable >
   absent`), `supp` = OR, PDF = keep the non-empty one.
4. **Concepts** — union of concept tags per PMID. If two concept files have the
   same ID but different content, flag for human resolution. The `index.json` is
   regenerated after merge.
5. **Embeddings** — the embedding files move with their PMID/concept folders. If
   models differ, the `embeddings_model` field in `record.json` records the
   discrepancy and a re-index is recommended.
6. **Event logs** — concatenated (append-only, so union is safe).
7. **Distillations** — if both DBs have a distillation for the same PMID, keep
   the primary DB's and log the conflict in the merge report.

The report is the artifact that makes the rare breaking change (collision)
*visible and correctable* instead of silent.

---

## 16. Open design points

### Re-screening of discarded records (staging)

`discarded_irrelevant` is a **staging** state, not terminal — an area of rest,
not a bin. (Contrast `excluded_manual`, which **is** terminal: human blacklist,
never revisited.) A discarded record is a dormant candidate, reactivatable. Its
defined contours:

- **Trigger** — never automatic, never every run. It is **conversational**,
  prompted by an analysis tool that, at query time, examines signals: citation-
  graph statistics, keyword clustering, semantic relevance (**not yet
  implemented**). The tool *proposes* a set of discards worth a second look; the
  human decides.
- **Output** — not a direct transition but a **staging set**: a batch of
  re-screening candidates presented for review. Re-screening produces proposals,
  not commits.
- **Dependency** — the whole mechanism sits downstream of analytic capabilities
  not yet built. Until graph/clustering/semantic signals exist, the trigger has
  no input. It is therefore a *hook*, not a function.

**Why the cascade is hard.** Re-screening is not a revaluation but a
*propagation*. The signals that trigger it (graph centrality, cluster
membership, semantic proximity) are all **relative to the corpus**. So if a
discard D1 is reactivated and ingested, the corpus changes, and with it those
signals change for D2, D3… some of which may now cross threshold → re-screening →
possible flip → the corpus changes again. It is an iterative fixpoint, kin to
label propagation on a citation graph. This is why "one revision cascades onto
other discards" is the nature of the problem, not a side effect.

**Required for it to work.** To re-screen selectively one must know *why* a
record was discarded — hence the discard event logs `reason` + `confidence` (§7).
This distinguishes a high-confidence, clear-cut discard ("animal model" — must
not flip just because a neighbor became relevant) from a just-below-threshold
one, the cascade candidate par excellence. The logging requirement is part of the
design; the propagation mechanism is not.

**Likely shape (not fixed).** The cascade, when built, will likely run as
**human-gated waves** — each wave a reviewed staging set whose commit generates
the next wave — rather than an autonomous fixpoint, consistent with the
conversational trigger.

### Autosuggest trigger (undefined)

The same non-periodic revision of queries/filters may be initiated by
conversation or by an **autosuggest** mechanism yet to be defined. Its detection
criteria and cadence are open.

### Event-triggered "related-by" screening

PubMed's ELink recommender (`cmd=neighbor` / pmra "related articles", plus
`citedin` and `refs`) emits PMIDs keyed by input PMID — the same currency the
exclusion filter and screening already consume. So it feeds the normal funnel:
seed it with `status: included` PMIDs, union the neighbor PMIDs into the
candidate pool, dedup with the same `comm -23`, and screen as usual. Neighbors
are an automatic mechanism, so they pass screening (never straight to
`included`) and their ingest events carry `via: recommend`.

This is corpus-relative expansion, not temporal monitoring — running it every
scheduled run would re-propose the same dense neighborhood and waste the
exclusion filter. It belongs on the non-periodic path instead, **fired by
particular events** rather than the clock:

- after **seeding or bootstrap** — expand outward from the initial corpus once
  it exists;
- on a **pivotal ingestion** — a newly included article deemed field-shifting is
  a strong seed whose neighborhood is worth pulling;
- after a **query/filter revision** — a changed `method.toml` may reframe what
  counts as relevant, making a fresh neighbor sweep of the current corpus
  worthwhile.

Like the other non-periodic paths, it *proposes* a candidate set for screening;
the human-gated flow decides what enters.

**Who judges "pivotal".** Deciding an article is field-shifting requires a
conceptual model of the field — which the **KB manager** holds, not this
standard. The feeder defines the *mechanism* (seed set → neighbor PMIDs → funnel)
but not the *significance judgment* that fires it. Within the feeder's own
artifacts the only proxy for significance is the **digest**
(`updates/<ts>.md`), a soft human-readable signal rather than a computed flag.

### Distillation drift

When a record's `distillation.md` is regenerated (due to a new LLM, a style
change, or new corpus context), the old one is overwritten. There is no version
history for distillations within the KB — git provides it externally. Whether
distillations should carry an explicit version or timestamp within the file is
open.

### Concept reification

Currently, concepts are explicit files with curated content. An alternative is
to treat concepts as purely derived — clusters over the embedding space with
auto-generated labels. The current design is explicit (human+LLM authored)
because the scale (50–100 concepts) permits curation and because concept-level
queries benefit from precise definitions. If the corpus grows beyond a few
hundred articles, partial automation (clustering as proposal, human labels as
ratification) will be needed.

### Embedding format at scale

JSON is acceptable at 3,300 vectors (~100 MB). Beyond ~10,000 vectors, binary
formats (Lance, Parquet) become necessary for the source-of-truth files, not
just the consolidated index. The `schema_version` on embedding files allows a
future migration to a binary format without breaking the design.

### Server process scope

The server process (§20) currently exposes search only. Whether it should also
handle ingestion (conversion, chunking, embedding) — becoming the KB manager's
runtime — is open. The current design keeps ingestion as a CLI invocation and
the server as a read-only search facade.

---

## 17. Single source of truth per fact

Organized on the premise/derivative and stable/temporal axes.

| Fact | Source | Nature |
|------|--------|--------|
| Purpose of the wiki project | `intent.md` | **premise** — human-only, no upstream, near-immutable |
| Queries, filters, parameters | `method.toml` | **derivative** — co-managed (intent + corpus), branch+merge |
| Pending screening candidates (payload: abstract, authors, DOI) | `staging/<ts>/<pmid>/candidate.json` | ephemeral — disposable once triaged; decoupled from event log |
| Pending decision (which PMIDs await screening) | `staging/` (directory listing) | derived — fast `find staging -name candidate.json`; no event-log walk |
| Record metadata | `record.json` (CSL) | stable state |
| Cite key | `record.json` `id` | frozen |
| Process state (inclusion) | `record.json` `status` | denormalized from the event log |
| Material flags (PDF, MD, chunks, distillation) | `record.json` `materials` | proxy for files on disk |
| Converted source text | `fulltext.md` | derived from PDF |
| Chunk boundaries | `chunk_index.json` | derived from fulltext.md |
| Source chunk embeddings | `chunks.jsonl` | derived, keyed to chunk |
| Distilled summary | `distillation.md` | judgement, article-level |
| Distillation embedding | `distillation.json` | derived from distillation.md |
| Concept definitions | `concepts/<id>.md` | judgement, curated |
| Concept embeddings | `concepts/<id>.json` | derived from concept text |
| Concept ↔ PMID mapping | `concepts/index.json` | derived, regenerable |
| Cite key ↔ PMID mapping | `concepts/index.json` | derived, regenerable |
| Events (run, match, ingest, discard, distill, …) | `events.jsonl` | temporal, append-only, git-tracked |
| Provenance / last-run / ingestion cause | `events.jsonl` (projection) | derived via `jq` |
| Outgoing references | `record.json` `custom.references` | stable |
| Cited-by | **not stored** | derived (internal) / pulled at run (external) |
| Payload | folder per PMID | referenced by `materials` flags |
| History / audit / rationale | git | wraps everything |
| Run interpretation (threads, suggestions) | `updates/<ts>.md` | derived digest, never authoritative |
| Bibliography for LaTeX | regenerated `.bib` | disposable |
| Consolidated search indexes | `embeddings/*.lance` | cache, regenerable |

---

## 18. Schema versioning

A single `schema_version` (semver) is stamped on every `record.json`, on
`method.toml`, on each `events.jsonl` line, on each embedding file, on
`chunk_index.json`, and on `concepts/index.json`. It is the one hook for
evolving the design without silent breakage.

- **PATCH** (`1.0.0 → 1.0.1`) — additive, backward-compatible (e.g. a new
  optional `custom` field). Old records remain valid; readers ignore what they
  don't know.
- **MINOR** (`1.0.0 → 1.1.0`) — new fields the system starts relying on, but old
  records still parse. A reader may lazily upfill defaults.
- **MAJOR** (`1.0.0 → 2.0.0`) — a breaking change to field meaning, key
  generation, or state ordering. Requires an explicit migration pass.

### Migration convention

Migrations are numbered scripts under `migrations/`, each a deterministic `jq`
rewrite, idempotent (guarded by the version stamp), that rewrites the stamped
version:

```
migrations/
  0001_1.0.0_to_1.1.0.sh
  0002_1.1.0_to_2.0.0.sh
```

The integrity check (§19) flags any record whose `schema_version` lags
`method.toml`'s, so drift after a partial migration is detectable.

---

## 19. Criticality points

- **Cite-key rename in merge.** The one place the external contract can break.
  Made *manageable* (report + `citekey_aliases`) but not *painless*: documents
  citing the renamed key must be updated by hand. Intrinsic cost of fusing two
  independent namespaces.
- **`citekey_aliases` ↔ real documents.** The system records obsolete keys but
  does not know which external documents use them; propagating a rename stays the
  user's responsibility. Aliases are a trail, not an auto-rewrite mechanism.
- **Write concurrency.** Git is not a runtime lock; `flock` is still required if
  scheduler and manual run can overlap.
- **No index.** Every view scans `db/*/record.json` and every projection scans
  `events.jsonl`. Instant at 200 records; the O(N²) graph derivation over
  references would be the first bottleneck an order of magnitude up. The design
  is explicitly tuned to the stated scale.
- **Staging ↔ db/ collusion.** If a PMID appears in both `db/` and an
  unresolved staging run folder, the update query treats it as known and won't
  re-propose it. If a staging run folder is deleted before all its candidates are
  triaged, pending decisions are silently lost. The integrity check flags any PMID
  in an active staging folder that also exists under `db/` as stale (already
  decided, should have been removed).
- **`status` ↔ event log coherence.** `status` is denormalized from the last
  inclusion event. A record whose `status` disagrees with its latest inclusion
  event is an inconsistency no schema constraint prevents.
- **Full-text may never arrive.** A record can rest in `included / fulltext:
  absent` indefinitely; this is by design, not a stuck state. The `unavailable`
  soft-terminal (retry cap in `method.toml`) stops automatic retries but keeps
  the record included and, by default, on the human wishlist. Whether an
  `unavailable` record ever leaves the wishlist is tunable
  (`keep_on_wishlist`), not settled.
- **Premise ↔ derivative drift.** If `intent.md` changes and `method.toml` is
  not revised, the derivative no longer reflects the premise. Not
  machine-verifiable (requires judgement), but a legitimate conversational flag.
- **Embedding-model drift.** If `record.json` records
  `embeddings_model: "text-embedding-ada-002"` but `method.toml` says
  `"text-embedding-3-small"`, semantic search mixes incompatible vector spaces.
  The integrity check flags this. Resolution is a re-index.
- **Chunk-index ↔ source-file coherence.** If `fulltext.md` is edited but
  `chunk_index.json` and `chunks.jsonl` are not regenerated, chunk offsets are
  stale. The integrity check verifies `source_hash` in `chunk_index.json`
  matches the current file hash.
- **Distillation ↔ concepts coherence.** A distillation tags concepts that don't
  exist in `concepts/`. This is not an error (the tag is a proposal), but the
  integrity check reports it as a warning.
- **Index regeneration is not transactional.** Regenerating
  `concepts/index.json` or `embeddings/*.lance` is a multi-file write. A crash
  during regeneration leaves the tree inconsistent. The integrity check detects
  this; the fix is to regenerate again.
- **Embedding files contain plain text.** `chunks.jsonl` includes the chunk text
  in plain form. If the fulltext contains sensitive or licensed material, the
  embedding files expose it. This is acceptable for a personal KB; a multi-user
  deployment would need access control outside this design.

### Integrity check

The integrity check verifies:

1. folder ↔ record (every folder has a `record.json`, every record a folder);
2. record ↔ materials on disk (`materials.fulltext: present` ⇔ a PDF exists;
   `materials.fulltext_md: present` ⇔ `fulltext.md` exists; same for
   `chunk_index.json`, `chunks.jsonl`, `distillation.md`, `distillation.json`);
3. query-id ↔ `method.toml` (no orphan query ids in the log);
4. record version ↔ `method.toml` version (no migration lag);
5. `status` ↔ event log (current inclusion state matches last inclusion event);
6. an `included` record has at least one `ingest` event in the log;
7. embedding model coherence — all embedding files for records with
   `embeddings_model` set use the model declared in `method.toml`;
8. chunk index ↔ source hash — `chunk_index.json`.source_hash matches the
   SHA-256 of `fulltext.md`;
9. concept ↔ index coherence — every concept ID in `index.json` has a
   corresponding file in `concepts/`, and vice versa;
10. distillation concept tags — every tag in `distillation.md` `[[…]]` links
    either has a matching concept file or is flagged as a proposal;
11. consolidated index freshness — if `embeddings/*.lance` exists, its record
    count matches the number of articles with embeddings;
12. staging coherence — no PMID in an active staging file also exists under
    `db/` (indicates a decision already made that should have been removed from
    staging). Stale staging entries are a warning.

A mismatch on items 2, 4, 8, or 11 is an error (the tree is inconsistent). Items
9, 10, 12 are warnings (flagged for human attention). Items 1, 3, 5, 6, 7 are errors.

---

## 20. Optional server process

For interactive semantic search, a lightweight server can be started from within
the KB folder. It is an **optional performance layer**, not a required component.

### Design constraints

- **Started from the folder, writes nothing outside it.**
- **Reads its state from the tree on startup.** If `embeddings/*.lance` exists,
  loads those. Otherwise scans `db/*/chunks.jsonl`, `db/*/distillation.json`,
  `concepts/*.json` and builds in-memory indexes.
- **Stateless across requests.** Any mutation to the KB (new article, re-index)
  requires a server restart to pick up changes. This is acceptable because
  ingestion is infrequent and server restarts are cheap.
- **Read-only.** The server answers queries; ingestion remains CLI-invoked.

### API surface

```
GET  /search/concepts?q=gene+X+renal+carcinoma
     → { results: [{ concept_id, score, pmids }, …] }

GET  /search/distillations?q=gene+X+renal+carcinoma
     → { results: [{ pmid, cite_key, score, snippet }, …] }

POST /verify
     Body: { claim: "gene X knockdown reduces tumor volume by 64%", citation: "smith2024a" }
     → { verdict: "supported"|"partially_supported"|"contradicted"|"not_found",
         passages: [{ chunk_id, text, score, section, start_byte, end_byte }, …] }

GET  /concept/<concept_id>
     → { concept: { id, title, definition, pmids, related_concepts, … } }

GET  /article/<pmid>
     → { record, distillation, concepts, materials }

GET  /health
     → { status: "ok", n_articles, n_concepts, n_chunks, model, kb_path }
```

### Implementation sketch

A single-file server (e.g., FastAPI or Flask) in `server/server.py` that:

1. On startup, reads `method.toml` → locates `db/`, `concepts/`, `embeddings/`.
2. Loads embeddings into numpy arrays with ID maps.
3. Accepts queries, embeds them with the same model (using the OpenAI API or a
   local model), computes cosine similarity, and returns ranked results.
4. For `/verify`, additionally reads `chunk_index.json` and `fulltext.md` to
   retrieve surrounding context.

Startup: `cd /path/to/kb && python server/server.py`

The `method.toml` `[search]` section controls whether consolidated indexes are
maintained and in what format.

### When the server is not needed

At 200 articles, a CLI tool that does the same thing (scan → compute → return)
is fast enough for interactive use (< 100ms for concept search, < 500ms for chunk
search). The server becomes useful when:

- The corpus exceeds ~500 articles (startup scan time becomes noticeable).
- Multiple clients need concurrent access.
- Latency must be sub-10ms (pre-computed indexes in memory).

Until then, the deterministic search commands suffice and keep the architecture
dependency-free.

---

## Appendix A: Bootstrapping a new KB

1. Human writes `intent.md` — the premise.
2. `papyrus init` — creates the folder tree, stamps schema versions, writes
   skeleton `method.toml` and `concepts/index.json`. Fails if `intent.md` is
   absent — the premise must exist before the tree.
3. Human seeds the base: uses the ingest command with `--pdf` for each seed
   article (bypasses screening).
4. Agent derives initial `method.toml` from `intent.md` + seed corpus, proposes
   on a branch.
5. Human ratifies (merge).
6. First run: the update command queries PubMed, the agent screens candidates,
   deterministic commands convert, embed, and index new articles.
7. Weekly wake-ups maintain the KB.

## Appendix B: Tools and dependencies

| Capability | Suggested tool | Notes |
|------------|---------------|-------|
| PubMed queries | Entrez E-utilities (`esearch`, `efetch`, `elink`) | NCBI API key recommended |
| PDF → markdown | `marker` (primary), `pymupdf4llm` (fallback) | Both produce structured MD |
| Full-text acquisition | Unpaywall API + PMC OA | Configured in `method.toml` |
| Chunking | Custom script using `fulltext.md` section structure | ~500 tokens, section-aware |
| Embedding | OpenAI `text-embedding-3-small` or local model | API key in env, not in KB tree |
| Consolidated index | Lance (`lancedb`) or FAISS | Optional, for search performance |
| CSL-JSON ↔ BibTeX | `pandoc` + `citeproc` | Cite key from `id` field |
| Screening / distillation / concepts | LLM (GPT-4o, Claude, or local) | Invoked by KB manager |
| Event log queries | `jq` | All deterministic views |
| Git | `git` | History, audit, promotion gating |
| Integrity check | Custom shell script + `jq` | Run via `housekeeping` task |
| Server | FastAPI + numpy/scipy | Optional, read-only search facade |
