import json
from typing import Annotated, Optional

import numpy as np
import typer
from rich.console import Console
from rich.table import Table

from papyrus.core.config import db_path, load_config, locate_kb_root
from papyrus.core.embedding import get_provider
from papyrus.core.searchlib import (
    cascade_search,
    cosine_scores,
    load_embeddings_from_tree,
    split_by_kind,
)

app = typer.Typer()
console = Console()

CASCADE_THRESHOLD = 0.5


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Search query text")],
    kind: Annotated[
        str,
        typer.Option("--kind", help="concepts, distillations, chunks, or auto (default)"),
    ] = "auto",
    top_k: Annotated[int, typer.Option("--top-k", help="Number of results")] = 10,
    cite: Annotated[
        Optional[str],
        typer.Option("--cite", help="Restrict search to chunks from this cite key"),
    ] = None,
):
    """Semantic search over the KB."""
    if not query.strip():
        console.print("[red]Query cannot be empty.[/red]")
        raise typer.Exit(code=2)

    kb_root = locate_kb_root()
    config = load_config(kb_root)
    db_root = db_path(config)
    concepts_root = kb_root / "concepts"

    restrict_pmid, cite_key = _resolve_cite(cite, concepts_root)
    if cite and not restrict_pmid:
        return

    provider = get_provider(config)
    query_vec = provider.embed([query])[0]
    q = np.array(query_vec, dtype=np.float32)

    vectors, meta = load_embeddings_from_tree(db_root, concepts_root)
    if len(vectors) == 0:
        console.print("[yellow]No embeddings found. Run papyrus embed first.[/yellow]")
        return

    if restrict_pmid:
        mask = [m.get("pmid") == restrict_pmid for m in meta]
        vectors = vectors[mask]
        meta = [m for i, m in enumerate(meta) if mask[i]]

    if kind != "auto":
        want = {"chunks": "chunk", "distillations": "distillation", "concepts": "concept"}.get(kind, kind)
        kind_mask = [m.get("kind") == want for m in meta]
        vectors = vectors[kind_mask]
        meta = [m for i, m in enumerate(meta) if kind_mask[i]]
        if len(vectors) == 0:
            console.print("[yellow]No matching embeddings.[/yellow]")
            return
        scores = cosine_scores(q, vectors)
        top_idx = np.argsort(scores)[-top_k:][::-1]
        top_scores = scores[top_idx]
    else:
        split = split_by_kind(vectors, meta)
        top_scores, top_idx = cascade_search(
            q,
            split["concept"][0], split["concept"][1],
            split["distillation"][0], split["distillation"][1],
            split["chunk"][0], split["chunk"][1],
            top_k=top_k,
            threshold=CASCADE_THRESHOLD,
        )
        meta = split["concept"][1] + split["distillation"][1] + split["chunk"][1]

    if len(top_idx) == 0:
        console.print("[yellow]No results above threshold.[/yellow]")
        return

    table = Table(title=f"Search: {query[:60]}")
    table.add_column("Score", style="green")
    table.add_column("Kind")
    table.add_column("PMID/Cite", style="cyan")
    table.add_column("Text")
    for i in range(min(len(top_idx), top_k)):
        idx = top_idx[i]
        score = float(top_scores[i])
        m = meta[idx]
        pmid = m.get("pmid", "")
        cid = m.get("concept_id", "")
        ref = pmid or cid
        text_preview = m.get("text", "")[:120].replace("\n", " ")
        table.add_row(f"{score:.3f}", m.get("kind", ""), ref, text_preview)
    console.print(table)


def _resolve_cite(cite: str | None, concepts_root) -> tuple[str | None, str | None]:
    if not cite:
        return None, None
    index_path = concepts_root / "index.json"
    if not index_path.exists():
        console.print("[red]No concepts/index.json — run papyrus index first[/red]")
        raise typer.Exit(code=2)
    with open(index_path) as f:
        idx = json.load(f)
    pm = idx.get("citekey_index", {}).get(cite)
    if not pm:
        console.print(f"[red]Cite key '{cite}' not found[/red]")
        raise typer.Exit(code=2)
    return pm, cite
