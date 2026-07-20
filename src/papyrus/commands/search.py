import json
from pathlib import Path
from typing import Annotated, Optional

import numpy as np
import typer
from rich.console import Console
from rich.table import Table

from papyrus.core.config import db_path, load_config, locate_kb_root

app = typer.Typer()
console = Console()


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

    from papyrus.core.embedding import get_provider, load_embeddings_from_tree

    # If citing, resolve cite key to PMID
    restrict_pmid: str | None = None
    if cite:
        index_path = concepts_root / "index.json"
        if index_path.exists():
            with open(index_path) as f:
                idx = json.load(f)
            restrict_pmid = idx.get("citekey_index", {}).get(cite)
            if not restrict_pmid:
                console.print(f"[red]Cite key '{cite}' not found[/red]")
                raise typer.Exit(code=2)
        else:
            console.print("[red]No concepts/index.json — run papyrus index first[/red]")
            raise typer.Exit(code=2)

    provider = get_provider(config)
    query_vec = provider.embed([query])[0]
    q = np.array(query_vec, dtype=np.float32)

    vectors, meta = load_embeddings_from_tree(db_root, concepts_root)
    if len(vectors) == 0:
        console.print("[yellow]No embeddings found. Run papyrus embed first.[/yellow]")
        raise typer.Exit(code=0)

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
        raise typer.Exit(code=0)

    norms = np.linalg.norm(vectors, axis=1)
    q_norm = np.linalg.norm(q)
    scores = np.dot(vectors, q) / (norms * q_norm + 1e-8)
    top_idx = np.argsort(scores)[-top_k:][::-1]

    table = Table(title=f"Search: {query[:60]}")
    table.add_column("Score", style="green")
    table.add_column("Kind")
    table.add_column("PMID/Cite", style="cyan")
    table.add_column("Text")
    for idx in top_idx:
        score = float(scores[idx])
        m = meta[idx]
        pmid = m.get("pmid", "")
        cid = m.get("concept_id", "")
        ref = pmid or cid
        text_preview = m.get("text", "")[:120].replace("\n", " ")
        table.add_row(f"{score:.3f}", m.get("kind", ""), ref, text_preview)
    console.print(table)
