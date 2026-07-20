import json
import time
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

from papyrus.core.config import db_path, load_config, locate_kb_root
from papyrus.core.events import write_event
from papyrus.core.record import load_record, save_record

app = typer.Typer()
console = Console()


def _chunks_up_to_date(chunks_path: Path, expected_model: str) -> bool:
    if not chunks_path.exists() or chunks_path.stat().st_size <= 1000:
        return False
    with open(chunks_path) as f:
        try:
            first = json.loads(f.readline())
        except Exception:
            return False
    return first.get("model") == expected_model


def _distillation_up_to_date(dist_json: Path, expected_model: str) -> bool:
    if not dist_json.exists():
        return False
    with open(dist_json) as f:
        try:
            dist = json.load(f)
        except Exception:
            return False
    return dist.get("model") == expected_model


def _collect_pmids(db_root: Path, mode: str, pmid: str | None) -> list[str]:
    if mode == "all":
        return [d.name for d in sorted(db_root.iterdir())
                if d.is_dir() and (d / "record.json").exists()
                and (d / "fulltext.md").exists()]
    if mode == "pending":
        pmids: list[str] = []
        for d in sorted(db_root.iterdir()):
            if not d.is_dir() or not (d / "record.json").exists():
                continue
            rec = load_record(d)
            if rec.get("custom", {}).get("status") != "included":
                continue
            has_md = (d / "fulltext.md").exists()
            has_chunks = (d / "chunks.jsonl").exists() and (d / "chunks.jsonl").stat().st_size > 1000
            if has_md and not has_chunks:
                pmids.append(d.name)
        return pmids
    return [pmid] if pmid else []


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

    from papyrus.core.embedding import get_provider

    emb_cfg = config.get("embeddings", {})
    provider = get_provider(config)
    model = emb_cfg.get("model", "")
    dims = emb_cfg.get("dimensions", 0)

    if pmid is not None:
        mode = "single"
    elif all_:
        mode = "all"
    elif pending:
        mode = "pending"
    elif kind == "concepts":
        mode = "concepts"
    else:
        console.print("[red]Specify a PMID, --all, or --pending[/red]")
        raise typer.Exit(code=2)

    if kind in ("concepts", "both"):
        concepts_root = kb_root / "concepts"
        concept_files = sorted(concepts_root.glob("*.md"))
        if concept_files:
            n_conc = 0
            for cf in concept_files:
                if cf.name.endswith(".json") or cf.stem == "index":
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

    if kind == "concepts":
        return

    pmids_to_embed = _collect_pmids(db_root, mode, pmid)
    n_ok = 0
    for p in pmids_to_embed:
        try:
            _embed_one(kb_root, db_root, provider, model, dims, p, kind)
            n_ok += 1
        except typer.Exit as e:
            if e.exit_code != 0:
                console.print(f"[yellow]  skipped {p}[/yellow]")

    if pmids_to_embed:
        console.print(f"[green]Embedded {n_ok}/{len(pmids_to_embed)} papers[/green]")


def _embed_one(
    kb_root: Path,
    db_root: Path,
    provider,
    model: str,
    dims: int,
    pmid: str,
    kind: str,
):
    record_dir = db_root / pmid
    if not (record_dir / "record.json").exists():
        console.print(f"[red]No record for PMID {pmid}[/red]")
        raise typer.Exit(code=2)

    rec = load_record(record_dir)

    if kind in ("chunks", "both"):
        _embed_chunks(kb_root, record_dir, rec, provider, model, dims, pmid)
    if kind in ("distillation", "both"):
        _embed_distillation(kb_root, record_dir, rec, provider, model, dims, pmid)

    save_record(record_dir, rec)


def _embed_chunks(kb_root, record_dir, rec, provider, model, dims, pmid):
    md_path = record_dir / "fulltext.md"
    ci_path = record_dir / "chunk_index.json"
    chunks_path = record_dir / "chunks.jsonl"

    if not md_path.exists():
        console.print(f"[red]No fulltext.md for PMID {pmid}. Run papyrus convert first.[/red]")
        raise typer.Exit(code=2)
    if not ci_path.exists():
        console.print(f"[red]No chunk_index.json for PMID {pmid}. Run papyrus convert first.[/red]")
        raise typer.Exit(code=2)

    if _chunks_up_to_date(chunks_path, model):
        console.print(f"[dim]  chunks for PMID {pmid} already up-to-date (model: {model})[/dim]")
        return

    with open(md_path) as f:
        fulltext = f.read()
    with open(ci_path) as f:
        ci = json.load(f)
    chunk_defs = ci.get("chunks", [])
    texts = []
    for cd in chunk_defs:
        texts.append(fulltext[cd["start_byte"] : cd["end_byte"]])

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


def _embed_distillation(kb_root, record_dir, rec, provider, model, dims, pmid):
    dist_path = record_dir / "distillation.md"
    dist_json_path = record_dir / "distillation.json"

    if not dist_path.exists():
        console.print(f"[yellow]No distillation.md for PMID {pmid}[/yellow]")
        return

    if _distillation_up_to_date(dist_json_path, model):
        console.print(f"[dim]  distillation for PMID {pmid} already up-to-date (model: {model})[/dim]")
        return

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
