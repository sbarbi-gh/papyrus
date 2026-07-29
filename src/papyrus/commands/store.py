from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from papyrus.core.config import db_path, load_config, locate_kb_root
from papyrus.core.storage import check_pdf, pull_pdf, push_pdf

app = typer.Typer()
console = Console()


@app.command()
def push(
    pmid: Annotated[str, typer.Argument(help="PubMed ID to push PDF for")],
):
    """Push local PDF to configured external storages."""
    kb_root = locate_kb_root()
    config = load_config(kb_root)
    db_root = db_path(config)
    storages = config.get("fulltext", {}).get("storages", [])

    external_storages = [s for s in storages if s != "local"]
    if not external_storages:
        console.print("[red]No external storages configured in [fulltext].storages[/red]")
        raise typer.Exit(code=1)

    pdf_path = db_root / pmid / "fulltext.pdf"
    if not pdf_path.exists():
        console.print(f"[red]No local fulltext.pdf for PMID {pmid}[/red]")
        console.print("Use 'papyrus store pull' to fetch from external storage.")
        raise typer.Exit(code=1)

    console.print(f"Pushing {pmid} to external storages...")
    try:
        uploaded = push_pdf(pdf_path, external_storages, pmid)
        for url in uploaded:
            console.print(f"  [green]OK[/green] {url}")
    except Exception as e:
        console.print(f"[red]Failed: {e}[/red]")
        raise typer.Exit(code=1)
    console.print("[green]Done.[/green]")


@app.command()
def pull(
    pmid: Annotated[str, typer.Argument(help="PubMed ID to pull PDF for")],
    output: Annotated[
        Optional[str],
        typer.Option("--output", "-o", help="Output directory (default: db/<pmid>/)"),
    ] = None,
):
    """Pull PDF from external storage to local or specified directory."""
    kb_root = locate_kb_root()
    config = load_config(kb_root)
    db_root = db_path(config)
    storages = config.get("fulltext", {}).get("storages", [])

    external_storages = [s for s in storages if s != "local"]
    if not external_storages:
        console.print("[red]No external storages configured in [fulltext].storages[/red]")
        raise typer.Exit(code=1)

    dest_dir = Path(output) if output else (db_root / pmid)
    dest_dir.mkdir(parents=True, exist_ok=True)

    console.print(f"Pulling {pmid} from external storage...")
    result = pull_pdf(pmid, external_storages, dest_dir)
    if result:
        console.print(f"[green]Downloaded to {result}[/green]")
    else:
        console.print(f"[red]PDF not found on any configured storage[/red]")
        raise typer.Exit(code=1)


@app.command()
def check(
    pmid: Annotated[
        Optional[str],
        typer.Argument(help="PubMed ID to check (omit for all with external fulltext)"),
    ] = None,
):
    """Verify PDF presence on configured external storages."""
    kb_root = locate_kb_root()
    config = load_config(kb_root)
    db_root = db_path(config)
    storages = config.get("fulltext", {}).get("storages", [])

    external_storages = [s for s in storages if s != "local"]
    if not external_storages:
        console.print("[yellow]No external storages configured[/yellow]")
        return

    if pmid:
        results = check_pdf(pmid, external_storages)
        for url, found in results.items():
            status = "[green]found[/green]" if found else "[red]missing[/red]"
            console.print(f"  {url}: {status}")
        return

    console.print("Scanning records with external fulltext...")
    missing = 0
    ok = 0
    table = Table(title="External storage check")
    table.add_column("PMID", style="cyan")
    for storage_url in external_storages:
        table.add_column(storage_url[:40], style="green")

    for record_dir in sorted(db_root.iterdir()):
        if not record_dir.is_dir():
            continue
        rec_path = record_dir / "record.json"
        if not rec_path.exists():
            continue
        import json
        with open(rec_path) as f:
            rec = json.load(f)
        if rec.get("custom", {}).get("materials", {}).get("fulltext") != "external":
            continue
        pmid_val = rec["custom"]["pmid"]
        results = check_pdf(pmid_val, external_storages)
        row = [pmid_val]
        all_ok = True
        for storage_url in external_storages:
            found = results.get(storage_url, False)
            row.append("[green]found[/green]" if found else "[red]missing[/red]")
            if not found:
                all_ok = False
        table.add_row(*row)
        if all_ok:
            ok += 1
        else:
            missing += 1

    console.print(table)
    console.print(f"[bold]{ok} OK, {missing} with missing external fulltext[/bold]")
