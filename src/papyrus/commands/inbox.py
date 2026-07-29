from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from papyrus.core.config import load_config, locate_kb_root

app = typer.Typer()
console = Console()


def _inbox_dir(kb_root: Path, config: dict) -> Path:
    rel = config.get("fulltext", {}).get("pdfinbox", "pdfinbox/")
    inbox = kb_root / rel
    return inbox


@app.command()
def list():
    """List PDFs in pdfinbox/ directory."""
    kb_root = locate_kb_root()
    config = load_config(kb_root)
    inbox = _inbox_dir(kb_root, config)

    if not inbox.exists():
        console.print("[yellow]pdfinbox/ directory does not exist[/yellow]")
        return

    pdfs = sorted(inbox.glob("*.pdf"))
    if not pdfs:
        console.print("[dim]No PDFs in pdfinbox/[/dim]")
        return

    table = Table(title="pdfinbox/")
    table.add_column("Filename", style="cyan")
    table.add_column("Size", style="dim")
    table.add_column("PMID guess", style="green")

    for pdf in pdfs:
        stem = pdf.stem
        size = f"{pdf.stat().st_size / 1024:.0f} KB"
        pmid_guess = stem if stem.isdigit() else "-"
        table.add_row(pdf.name, size, pmid_guess)

    console.print(table)
    console.print("[dim]Use 'papyrus ingest <pmid> --pdf pdfinbox/<file>' to ingest.[/dim]")


@app.command()
def clean(
    delete: Annotated[
        bool,
        typer.Option("--delete", "-d", help="Delete ingested PDFs from pdfinbox/"),
    ] = False,
):
    """Remove PDFs from pdfinbox/ that have already been ingested."""
    kb_root = locate_kb_root()
    config = load_config(kb_root)
    inbox = _inbox_dir(kb_root, config)
    db_root = kb_root / config.get("settings", {}).get("db_path", "db")

    if not inbox.exists():
        console.print("[yellow]pdfinbox/ directory does not exist[/yellow]")
        return

    cleaned = 0
    for pdf in sorted(inbox.glob("*.pdf")):
        stem = pdf.stem
        if stem.isdigit() and (db_root / stem).is_dir():
            if delete:
                pdf.unlink()
                console.print(f"[green]Deleted {pdf.name} (PMID {stem} already in db/)[/green]")
            else:
                console.print(f"[dim]{pdf.name} → PMID {stem} already ingested (dry-run)[/dim]")
            cleaned += 1

    if not cleaned:
        console.print("[dim]No ingested PDFs to clean[/dim]")
    elif not delete:
        console.print(f"[dim]{cleaned} PDF(s) ready to clean. Use --delete to remove.[/dim]")
    else:
        console.print(f"[green]Cleaned {cleaned} PDF(s) from pdfinbox/[/green]")
