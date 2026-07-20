import json
import time
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

from papyrus.core.config import db_path, load_config, locate_kb_root
from papyrus.core.events import write_event

app = typer.Typer()
console = Console()


@app.command()
def embed(
    pmid: Annotated[
        Optional[str],
        typer.Argument(help="PubMed ID to embed (omit with --all or --pending)"),
    ] = None,
    kind: Annotated[
        str,
        typer.Option("--kind", help="chunks (default), distillation, or both"),
    ] = "chunks",
    all_: Annotated[
        bool,
        typer.Option("--all", help="Embed all papers with fulltext"),
    ] = False,
    pending: Annotated[
        bool,
        typer.Option("--pending", help="Embed papers with unconverted or unembedded fulltext"),
    ] = False,
):
    """Chunk and embed source text or distillation."""
    kb_root = locate_kb_root()
    config = load_config(kb_root)
    db_root = db_path(config)

    if pmid is None and not all_ and not pending:
        if kind == "concepts":
            pmids_to_embed = []
        else:
            console.print("[red]Specify a PMID, --all, or --pending[/red]")
            raise typer.Exit(code=2)

    from papyrus.core.embedding import get_provider

    emb_cfg = config.get("embeddings", {})
    provider = get_provider(config)
    model = emb_cfg.get("model", "")
    dims = emb_cfg.get("dimensions", 0)

    pmids_to_embed: list[str] = []
    if all_:
        for d in sorted(db_root.iterdir()):
            if not d.is_dir() or not (d / "record.json").exists():
                continue
            with open(d / "record.json") as f:
                rec = json.load(f)
            if rec.get("custom", {}).get("status") != "included":
                continue
            if (d / "fulltext.md").exists():
                pmids_to_embed.append(d.name)
    elif pending:
        for d in sorted(db_root.iterdir()):
            if not d.is_dir() or not (d / "record.json").exists():
                continue
            with open(d / "record.json") as f:
                rec = json.load(f)
            if rec.get("custom", {}).get("status") != "included":
                continue
            mats = rec.get("custom", {}).get("materials", {})
            has_md = (d / "fulltext.md").exists()
            has_chunks = (d / "chunks.jsonl").exists() and (d / "chunks.jsonl").stat().st_size > 1000
            if has_md and not has_chunks:
                pmids_to_embed.append(d.name)
    else:
        if pmid is not None:
            pmids_to_embed = [pmid]  # type: ignore[list-item]

    n_ok = 0
    for p in pmids_to_embed:
        try:
            _embed_one(kb_root, config, db_root, provider, model, dims, p, kind)
            n_ok += 1
        except typer.Exit as e:
            if e.exit_code != 0:
                console.print(f"[yellow]  skipped {p}[/yellow]")

    console.print(f"[green]Embedded {n_ok}/{len(pmids_to_embed)} papers[/green]")

    if kind in ("concepts", "both"):
        concepts_root = kb_root / "concepts"
        concept_files = sorted(concepts_root.glob("*.md"))
        if not concept_files:
            return
        n_conc = 0
        for cf in concept_files:
            if cf.name == "index.json":
                continue
            cid = cf.stem
            text = cf.read_text()
            console.print(f"[dim]Embedding concept {cid}[/dim]")
            vecs = provider.embed([text])
            conc_rec = {
                "schema_version": "1.0.0",
                "concept_id": cid,
                "text": text,
                "embedding": vecs[0],
                "model": model,
                "dimensions": dims,
                "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            with open(concepts_root / f"{cid}.json", "w") as f:
                json.dump(conc_rec, f, indent=2, ensure_ascii=False)
                f.write("\n")
            n_conc += 1
        console.print(f"[green]Embedded {n_conc} concepts[/green]")


def _embed_one(
    kb_root: Path,
    config: dict,
    db_root: Path,
    provider,
    model: str,
    dims: int,
    pmid: str,
    kind: str,
):
    record_dir = db_root / pmid
    record_path = record_dir / "record.json"
    if not record_path.exists():
        console.print(f"[red]No record for PMID {pmid}[/red]")
        raise typer.Exit(code=2)

    with open(record_path) as f:
        rec = json.load(f)

    if kind in ("chunks", "both"):
        md_path = record_dir / "fulltext.md"
        ci_path = record_dir / "chunk_index.json"
        chunks_path = record_dir / "chunks.jsonl"

        if not md_path.exists():
            console.print(f"[red]No fulltext.md for PMID {pmid}. Run papyrus convert first.[/red]")
            if kind == "chunks":
                raise typer.Exit(code=2)
        elif not ci_path.exists():
            console.print(f"[red]No chunk_index.json for PMID {pmid}. Run papyrus convert first.[/red]")
            if kind == "chunks":
                raise typer.Exit(code=2)
        else:
            with open(md_path) as f:
                fulltext = f.read()
            with open(ci_path) as f:
                ci = json.load(f)
            chunk_defs = ci.get("chunks", [])
            texts = []
            for cd in chunk_defs:
                start = cd["start_byte"]
                end = cd["end_byte"]
                texts.append(fulltext[start:end])

            console.print(f"[dim]Embedding {len(texts)} chunks for PMID {pmid} (model: {model})[/dim]")
            vectors = provider.embed(texts)

            with open(chunks_path, "w") as f:
                for cd, vec, txt in zip(chunk_defs, vectors, texts):
                    line = {
                        "chunk_id": cd["chunk_id"],
                        "text": txt,
                        "embedding": vec,
                        "model": model,
                        "dimensions": dims,
                    }
                    f.write(json.dumps(line, ensure_ascii=False) + "\n")

            rec["custom"]["materials"]["embeddings_model"] = model
            rec["custom"]["materials"]["embeddings_dim"] = dims
            write_event(kb_root, {
                "kind": "embedded",
                "pmid": pmid,
                "model": model,
                "subkind": "source_chunks",
                "n_vectors": len(vectors),
            })
            console.print(f"[green]  {len(vectors)} chunk vectors written to chunks.jsonl[/green]")

    if kind in ("distillation", "both"):
        dist_path = record_dir / "distillation.md"
        dist_json_path = record_dir / "distillation.json"

        if not dist_path.exists():
            console.print(f"[yellow]No distillation.md for PMID {pmid}[/yellow]")
        else:
            text = dist_path.read_text()
            console.print(f"[dim]Embedding distillation for PMID {pmid}[/dim]")
            vectors = provider.embed([text])

            dist_record = {
                "schema_version": "1.0.0",
                "pmid": pmid,
                "text": text,
                "embedding": vectors[0],
                "model": model,
                "dimensions": dims,
                "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            with open(dist_json_path, "w") as f:
                json.dump(dist_record, f, indent=2, ensure_ascii=False)
                f.write("\n")

            rec["custom"]["materials"]["embeddings_model"] = model
            rec["custom"]["materials"]["embeddings_dim"] = dims
            rec["custom"]["materials"]["distillation"] = "present"
            write_event(kb_root, {
                "kind": "embedded",
                "pmid": pmid,
                "model": model,
                "subkind": "distillation",
                "n_vectors": 1,
            })
            console.print(f"[green]  distillation embedding written to distillation.json[/green]")

    with open(record_path, "w") as f:
        json.dump(rec, f, indent=2, ensure_ascii=False)
        f.write("\n")
