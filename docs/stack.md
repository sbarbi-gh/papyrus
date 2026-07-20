# papyrus — Software stack & implementation directions

## Language & platform

**Python 3.11+** Floor is 3.11 for `tomllib` in stdlib, `match` statements, and
async ergonomics. All key dependencies (marker, pymupdf4llm, numpy) are
Python-native.

## Packaging & dependency management

**`uv`** — `uv init`, `uv add`, `uv run papyrus`. Entry point in
`pyproject.toml`:

```toml
[project.scripts]
papyrus = "papyrus.cli:main"
```

## CLI framework

**`typer` + `rich`** over `click`/`argparse`. Typer gives type-hint-driven
subcommands and argument parsing. Rich gives colorized output, JSON
pretty-printing, progress bars for batch embedding operations, and table
rendering for check reports.

All commands are **synchronous**. Network operations (PubMed queries, embedding
API calls) use `httpx` in sync mode. If a long task needs to be backgrounded,
the agent spawns it in a new shell. No asyncio in the CLI.

## HTTP client

**`httpx`** — sync mode. One client session for all external calls: PubMed
E-utilities, Unpaywall API, OpenAI/Ollama embedding APIs. Connection pooling,
timeout handling, retry with exponential backoff.

## PubMed API

A lightweight `httpx` wrapper — not Biopython. The three endpoints needed:

| Operation | Endpoint                                                        |
| --------- | --------------------------------------------------------------- |
| Search    | `GET esearch.fcgi?db=pubmed&term=...&retmode=json`              |
| Fetch     | `GET efetch.fcgi?db=pubmed&id=...&rettype=abstract&retmode=xml` |
| Link      | `GET elink.fcgi?dbfrom=pubmed&cmd=neighbor&id=...&retmode=json` |

NCBI rate limiting (10 req/s with API key, 3 req/s without) is handled with a
semaphore + retry decorator (~50 lines). Does not warrant Biopython's weight.

## PDF conversion

**Strategy pattern**, not a plugin system. Config-keyed dispatch from
`method.toml` `[source_conversion]`:

```python
converters = {
    "marker": MarkerConverter,
    "pymupdf4llm": PyMuPDFConverter,
}
engine = converters[config["source_conversion"]["engine"]]()
```

Primary: `marker`. Fallback: `pymupdf4llm`. Both produce structured markdown
(§10).

## Embedding

**Provider-swap via strategy pattern.** The model name and dimensions are
project identity — stamped on every stored embedding record (`model`,
`dimensions` fields) and checked by integrity check #7. Switching models
requires a re-embed of all articles: change `method.toml`, run `papyrus embed`
on all records, pass `papyrus check`.

The provider is just the transport for a given model:

```python
class EmbeddingProvider(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...

providers = {
    "openai": lambda cfg: OpenAIProvider(model=cfg["model"]),
    "ollama": lambda cfg: OllamaProvider(model=cfg["model"], base_url=cfg.get("base_url")),
}
```

| Provider | Config                                | Auth                     |
| -------- | ------------------------------------- | ------------------------ |
| `openai` | model + dimensions from `method.toml` | `OPENAI_API_KEY` env var |
| `ollama` | model + optional `base_url`           | none (local)             |

`OpenAIProvider` uses the `openai` SDK (batching, retry, error codes already
handled). `OllamaProvider` is pure `httpx` calls to the local API.

The `embeddings` section of `method.toml` controls model, dimensions, provider,
and batch size. A `provider` field selects the implementation.

## Search

**In-memory numpy, read from JSONL on every invocation.** At 200 articles × ~15
chunks = ~3100 vectors at 1536 dimensions, that's ~19 MB of float32. Loading +
cosine similarity takes <100ms. No Lance, no FAISS, no persistent server at
bootstrap.

```python
# search.py pseudo
vectors = np.vstack([load_embeddings_from_tree()])
query_vec = provider.embed([query_text])[0]
scores = np.dot(vectors, query_vec) / (np.linalg.norm(vectors, axis=1) * np.linalg.norm(query_vec))
top_k = np.argsort(scores)[-k:]
```

Cascading search strategy (concepts → distillations → chunks) is implemented in
the CLI, following §14.

## jq awareness

`jq` is **not a runtime dependency** of the CLI. The deterministic views it
previously expressed (`"show me all seeds"`, `"last run for q1"`) are now
consumed through Python code scanning the tree directly.

However, the storage model is designed to be **jq-queryable**: `events.jsonl` is
JSON-lines, `record.json` is plain JSON, `method.toml` is structured. An agent
can `jq` over these files to improvise workflows not covered by the CLI —
deriving provenance, inspecting event history, or answering ad-hoc questions —
without touching Python. This is by design, not by accident.

## Project layout

```
papyrus/
  pyproject.toml
  src/papyrus/
    __init__.py
    cli.py                     # typer app, command registration
    commands/
      update.py                # esearch + dedup + staging folder creation
      ingest.py                # unstaging + direct seed/manual
      discard.py
      convert.py               # PDF → MD via strategy dispatch
      embed.py                 # chunk + embed via provider dispatch
      index.py                 # regenerate concepts/index.json
      check.py                 # 12 integrity checks
      search.py                # in-memory semantic search
    core/
      config.py                # method.toml parsing
      record.py                # record.json creation + cite key assignment
      citekey.py               # cite key logic (§4)
      pubmed.py                # E-utilities httpx wrapper
      chunking.py              # section-aware chunker
      embedding.py             # EmbeddingProvider protocol + dispatch
      providers/
        openai.py
        ollama.py
      converters/
        marker.py
        pymupdf.py
      searchlib.py             # cosine similarity, cascade strategy
      checklib.py              # integrity check implementations
```

Flat under `commands/` — no sub-packages. Core modules are shared utilities
consumed by multiple commands.

## Testing

**`pytest`.** Deterministic commands are testable with:

- **Fixture KB tree** — pre-built `db/`, `concepts/`, `events.jsonl`, `staging/`
  under `tests/data/`. Full integration tests: `papyrus update` against a mock
  PubMed, `papyrus check` against a known tree.
- **Mock httpx transport** — `responses` library or `httpx.MockTransport` for
  PubMed E-utilities, OpenAI, Ollama endpoints.
- **Golden files** — expected `record.json`, `staging/` layout, `events.jsonl`
  lines, stdout output.

Judgement-step outputs (distillation, screening) are not tested by the
deterministic CLI — they are created by the agent and consumed as input to
`embed` and `index`.

## What is not in this stack

- **Lance / FAISS** — in-memory numpy is sufficient at 200-article scale.
  Deferred until the corpus grows or search latency demands pre-computed
  indexes.
- **Plugin system** — three providers, two converters. A registry dict per
  module is the lightest abstraction that works. If either category grows beyond
  4–5 implementations, revisit.
- **Async CLI** — all commands are synchronous. Network calls use sync `httpx`.
  The agent handles concurrency by spawning subprocesses when needed.
- **jq as a dep** — awareness is designed into the data model; the CLI does not
  shell out to jq.
- **Database** — the filesystem is the database. No SQLite, no ORM.
