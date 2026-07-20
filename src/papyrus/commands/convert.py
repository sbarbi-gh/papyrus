import hashlib
import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from papyrus.core.config import db_path, load_config, locate_kb_root
from papyrus.core.events import write_event
from papyrus.core.record import load_record, save_record

app = typer.Typer()
console = Console()


def _source_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _idempotent_check(md_path: Path, ci_path: Path) -> bool:
    if not md_path.exists() or not ci_path.exists():
        return False
    with open(ci_path) as f:
        ci = json.load(f)
    stored = ci.get("source_hash", "")
    _, _, expected = stored.partition("sha256:")
    if not expected:
        expected = stored
    return expected == _source_hash(md_path)


def _run_conversion(pdf_path: Path, md_path: Path, record_dir: Path, config: dict) -> int:
    engine = config.get("source_conversion", {}).get("engine", "pymupdf")

    if engine == "marker":
        console.print("[dim]marker conversion not yet implemented, using pymupdf4llm[/dim]")
        engine = "pymupdf"

    if engine == "pymupdf":
        try:
            import pymupdf4llm
        except ImportError:
            console.print("[red]pymupdf4llm not installed. Run: uv add pymupdf4llm[/red]")
            raise typer.Exit(code=1)

        md_text = pymupdf4llm.to_markdown(str(pdf_path), write_images=False)
        md_path.write_text(md_text, encoding="utf-8")

        from papyrus.core.chunking import write_chunks

        chunking_cfg = config.get("chunking", {})
        write_chunks(
            pmid=record_dir.name,
            fulltext_path=md_path,
            record_dir=record_dir,
            target_bytes=chunking_cfg.get("target_bytes", 2048),
            overlap_bytes=chunking_cfg.get("overlap_bytes", 256),
            max_chunks=chunking_cfg.get("max_chunks_per_article", 80),
        )
        return engine
    else:
        console.print(f"[red]Unknown conversion engine: {engine}[/red]")
        raise typer.Exit(code=1)


@app.command()
def convert(
    pmid: Annotated[str, typer.Argument(help="PubMed ID to convert fulltext for")],
):
    """Convert PDF to structured markdown.

    Reads db/<pmid>/fulltext.pdf, writes fulltext.md and chunk_index.json.
    Idempotent — skips if fulltext.md already present and chunk_index matches source hash.
    """
    kb_root = locate_kb_root()
    config = load_config(kb_root)
    db_root = db_path(config)

    record_dir = db_root / pmid
    pdf_path = record_dir / "fulltext.pdf"
    md_path = record_dir / "fulltext.md"
    ci_path = record_dir / "chunk_index.json"

    if not pdf_path.exists():
        console.print(f"[red]No fulltext.pdf for PMID {pmid}[/red]")
        raise typer.Exit(code=2)

    if _idempotent_check(md_path, ci_path):
        console.print(f"[dim]db/{pmid}/fulltext.md already up-to-date[/dim]")
        raise typer.Exit(code=0)

    console.print(f"[dim]Converting db/{pmid}/fulltext.pdf → fulltext.md[/dim]")
    engine = _run_conversion(pdf_path, md_path, record_dir, config)

    rec = load_record(record_dir)
    rec["custom"]["materials"]["fulltext_md"] = "present"
    rec["custom"]["materials"]["chunks"] = _count_chunks(ci_path)
    save_record(record_dir, rec)

    write_event(kb_root, {
        "kind": "source-converted",
        "pmid": pmid,
        "engine": "pymupdf4llm" if engine == "pymupdf" else engine,
        "pages": _count_pdf_pages(pdf_path),
        "n_chunks": rec["custom"]["materials"]["chunks"],
    })

    n_chunks = rec["custom"]["materials"]["chunks"]
    console.print(f"[green]db/{pmid}/fulltext.md  {n_chunks} chunks[/green]")


def _count_chunks(ci_path: Path) -> int:
    if not ci_path.exists():
        return 0
    with open(ci_path) as f:
        ci = json.load(f)
    return len(ci.get("chunks", []))


def _count_pdf_pages(pdf_path: Path) -> int:
    try:
        import pymupdf
        doc = pymupdf.open(str(pdf_path))
        n = doc.page_count
        doc.close()
        return n
    except Exception:
        return 0
