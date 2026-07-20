import json
import shutil
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

from papyrus.core.config import db_path, locate_kb_root
from papyrus.core.events import write_event
from papyrus.core.pubmed import efetch
from papyrus.core.record import (
    create_record,
    load_enrichment_from_staging,
    remove_from_staging,
)

app = typer.Typer()
console = Console()


@app.command()
def ingest(
    pmid: Annotated[str, typer.Argument(help="PubMed ID to ingest")],
    pdf: Annotated[
        Optional[Path],
        typer.Option("--pdf", help="Path to PDF for direct seed ingestion"),
    ] = None,
    via: Annotated[
        str,
        typer.Option("--via", help="Provenance tag (seed, manual, query)"),
    ] = "",
):
    """Ingest an article into the database.

    If the PMID exists in staging/, unstages it (screening flow).
    Otherwise fetches metadata from PubMed directly (seed/manual — bypasses screening).
    """
    kb_root = locate_kb_root()
    db_root = db_path({"settings": {"db_path": "./db"}, "_kb_root": kb_root})
    staging_root = kb_root / "staging"

    # Check if already in db
    record_path = db_root / pmid / "record.json"
    if record_path.exists():
        console.print(f"[yellow]PMID {pmid} already exists in db/[/yellow]")
        raise typer.Exit(code=2)

    # Try staging first
    enrichment = load_enrichment_from_staging(pmid, staging_root)
    mat_fulltext = "absent"

    if enrichment is not None:
        if not via:
            via = "query"
        console.print(f"[dim]Found in staging — unstaging[/dim]")
    else:
        if not via:
            via = "seed"
        console.print(f"[dim]Fetching metadata from PubMed[/dim]")
        fetched = efetch([pmid])
        if not fetched:
            console.print(f"[red]PMID {pmid} not found in PubMed[/red]")
            raise typer.Exit(code=3)
        enrichment = fetched[0]

    if pdf is not None:
        mat_fulltext = "present"

    record_dir = create_record(
        pmid=pmid,
        enrichment=enrichment,
        db_root=db_root,
        status="included",
        materials_fulltext=mat_fulltext,
    )

    if pdf is not None:
        dest = record_dir / "fulltext.pdf"
        shutil.copy2(str(pdf), str(dest))

    if enrichment is not None:
        remove_from_staging(pmid, staging_root)

    cite_key = "?"
    with open(record_dir / "record.json") as f:
        cite_key = json.load(f).get("id", "?")

    write_event(kb_root, {"kind": "ingest", "pmid": pmid, "via": via})

    console.print(f"[green]{record_dir.relative_to(kb_root)}/  {cite_key} (via: {via})[/green]")
