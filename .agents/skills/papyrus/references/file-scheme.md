# KB file scheme reference

## Directory layout

```
intent.md                    # human-authored premise (agent never writes)
method.toml                  # queries, filters, parameters (co-managed)
events.jsonl                 # append-only structured event log (JSON-lines)
updates/
  <ts>.md                    # per-run narrative digest (agent-authored)
staging/
  <ts>/                      # one folder per update run
    <pmid>/
      candidate.json         # {pmid, title, abstract, authors, doi, year, journal, references}
db/
  <pmid>/
    record.json              # CSL-JSON bibliographic metadata + process state
    abstract.txt             # plain-text abstract
    fulltext.pdf             # original PDF
    fulltext.md              # structured markdown (after convert)
    chunk_index.json         # {chunk_id, start_byte, end_byte} mapping into fulltext.md
    chunks.jsonl             # one line per chunk: {chunk_id, text, embedding, model, dimensions}
    distillation.md          # concept-tagged agent-authored summary
    distillation.json        # {text, embedding, model, dimensions}
    supp/                    # supplementary materials
concepts/
  <id>.md                    # long-form concept article (agent-authored)
  .archived/                 # archived concepts (zero linked articles)
  index.json                 # bidirectional map: concepts ↔ PMIDs, cite_keys → PMID
embeddings/                   # optional consolidated search indexes (regenerable cache)
  sources.lance
  distillations.lance
  concepts.lance
```

## record.json

Format: CSL-JSON with a `custom` block for process state. Bibliographic
fields are standard CSL; process state is namespaced so it never collides
with export.

```json
{
  "schema_version": "1.1.0",
  "id": "smith2024a",
  "type": "article-journal",
  "title": "Gene X is a master regulator of renal carcinoma",
  "author": [{"family": "Smith", "given": "J."}],
  "issued": {"date-parts": [[2024, 3, 15]]},
  "DOI": "10.xxxx",
  "container-title": "Nature",
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
      "chunks": 14,
      "embeddings_model": "text-embedding-3-small",
      "embeddings_dim": 1536
    },
    "concepts": ["renal-carcinoma", "gene-expression-networks"]
  }
}
```

### Materials flags

| Field | Values | Meaning |
|---|---|---|
| `fulltext` | `absent`, `present`, `unavailable` | PDF availability |
| `fulltext_md` | `absent`, `present` | Structured markdown exists |
| `supp` | `true`, `false` | Supplementary materials present |
| `distillation` | `absent`, `present` | Distillation written |
| `chunks` | integer | Number of chunks |
| `embeddings_model` | string | Model used for embeddings |
| `embeddings_dim` | integer | Embedding dimensions |

### Status values

| Status | Meaning | Set by |
|---|---|---|
| `included` | KB member | `papyrus ingest` |
| `discarded` | Not relevant, filtered | `papyrus discard` |
| `excluded` | Human blacklist, never revisited | manual edit |

## candidate.json

Emitted by `papyrus update` into `staging/<ts>/<pmid>/candidate.json`.

```json
{
  "pmid": "38471234",
  "title": "Transcription Factor X Drives Oncogenesis",
  "abstract": "We show that gene X ...",
  "authors": [{"family": "Smith", "given": "J."}],
  "journal": "Nature",
  "year": "2024",
  "doi": "10.xxxx",
  "references": ["38559001", "37990022"]
}
```

## events.jsonl

Append-only, one JSON object per line. Every action that mutates state
writes an event. Key event kinds:

| kind | Key fields | Written by |
|---|---|---|
| `query-run` | `query_id`, `term`, `n_found`, `n_new` | `papyrus update` |
| `query-match` | `query_id`, `pmid` | `papyrus update` |
| `ingest` | `pmid`, `via` (`seed`, `query`, `related`) | `papyrus ingest` |
| `discard` | `pmid`, `reason`, `confidence` | `papyrus discard` |
| `source-converted` | `pmid`, `engine`, `n_chunks` | `papyrus convert` |
| `embedded` | `pmid`, `model`, `subkind`, `n_vectors` | `papyrus embed` |
| `concept-created` | `concept_id` | agent (manual) |
| `concept-linked` | `concept_id`, `pmid` | `papyrus index` |
| `concept-merged` | `winner`, `loser` | agent (manual) |
| `concept-archived` | `concept_id` | agent (manual) |
| `re-index` | `old_model`, `new_model`, `n_articles` | agent (manual) |
| `integrity-check` | `result`, `checks` | `papyrus check` |

## chunks.jsonl

One JSON line per chunk. Stored in `db/<pmid>/chunks.jsonl`.

```json
{"chunk_id":"00001","text":"Full text of the chunk...","embedding":[0.0123,...],"model":"text-embedding-3-small","dimensions":1536}
```

## distillation.json

Single JSON object. Stored in `db/<pmid>/distillation.json`.

```json
{
  "schema_version": "1.0.0",
  "pmid": "38471234",
  "text": "Complete distillation text...",
  "embedding": [0.0123, ...],
  "model": "text-embedding-3-small",
  "dimensions": 1536,
  "generated": "2024-03-15T09:00:00Z"
}
```

## concept JSON embedding

Companion to `concepts/<id>.md`. Stored as `concepts/<id>.json`.

```json
{
  "schema_version": "1.0.0",
  "concept_id": "renal-carcinoma",
  "text": "Renal cell carcinoma (RCC) is a kidney cancer...",
  "embedding": [0.0123, ...],
  "model": "text-embedding-3-small",
  "dimensions": 1536,
  "generated": "2024-03-15T09:00:00Z"
}
```

## concepts/index.json

Bidirectional map, regenerated by `papyrus index`. Never hand-edited.

```json
{
  "schema_version": "1.1.0",
  "concepts": {
    "renal-carcinoma": {
      "pmids": ["38471234", "38559001"],
      "related": ["immunotherapy"],
      "n_articles": 23,
      "last_updated": "2024-03-15T09:00:00Z"
    }
  },
  "citekey_index": {
    "smith2024a": "38471234"
  },
  "pmid_to_citekey": {
    "38471234": "smith2024a"
  }
}
```

## chunk_index.json

Maps chunk boundaries to byte offsets in `fulltext.md`.

```json
{
  "schema_version": "1.1.0",
  "source_file": "fulltext.md",
  "source_hash": "sha256:abc123...",
  "chunk_method": "section",
  "chunks": [
    {"chunk_id": "00001", "start_byte": 0, "end_byte": 2048, "section": "Introduction"}
  ]
}
```

## method.toml sections

```toml
schema_version = "1.1.0"

[settings]
db_path = "./db"
primary_db = "./db"
chunk_size = 60

[fulltext]
max_attempts = 5
backoff = "exponential"
keep_on_wishlist = true

[relevance]
inclusion = '''Include studies on X in adult populations.
Exclude case reports and non-English studies.
'''
min_confidence = 0.6

[source_conversion]
engine = "marker"
fallback = "pymupdf"

[chunking]
method = "section"
target_bytes = 2048
overlap_bytes = 256
max_chunks_per_article = 80

[embeddings]
model = "text-embedding-3-small"
dimensions = 1536
provider = "openai"
batch_size = 50

[distillation]
style = "concept-tagged"
max_length = 2000
required_sections = ["claim", "methods", "findings", "concepts", "relation-to-corpus"]

[concepts]
max_concepts = 150
min_articles_per_concept = 2
allow_orphan = false

[search]
consolidated_index = true
index_format = "lance"

[[queries]]
id = "q1"
enabled = true
term = '''"topic A"[Title/Abstract] AND 2020:2026[dp]'''
```

### Query array-of-tables

Each `[[queries]]` entry has:
- `id` — stable identifier (persists across term changes)
- `enabled` — `true`/`false` (disable without deleting, preserves history)
- `term` — PubMed query string (boolean, field-tagged, MeSH)

## Design axioms

Three axes structure the data model:

1. **Deterministic vs judgement** — fetch, dedup, format, chunk, embed are
   deterministic CLI. Screening, distillation, concept curation require
   judgement (you, the agent).
2. **Premise vs derivative** — `intent.md` is the human axiom. `method.toml`
   is derived from it + corpus, co-managed.
3. **Stable vs temporal** — stable facts live in records and concept files;
   temporal facts live in `events.jsonl`. Never the reverse.
