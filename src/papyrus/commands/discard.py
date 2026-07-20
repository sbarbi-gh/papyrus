import json
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

from papyrus.core.config import db_path, locate_kb_root
from papyrus.core.events import write_event
from papyrus.core.record import (
    create_record,
    load_enrichment_from_staging,
    remove_from_staging,
)

app = typer.Typer()
console = Console()


@app.command()
def discard(
    pmid: Annotated[str, typer.Argument(help="PubMed ID to discard")],
    reason: Annotated[
        str,
        typer.Option("--reason", help="Reason for discarding this article"),
    ] = "",
    confidence: Annotated[
        Optional[float],
        typer.Option("--confidence", help="Confidence score (0.0–1.0)"),
    ] = None,
):
    """Discard an article and create a permanent record so it won't be re-proposed."""
    kb_root = locate_kb_root()
    db_root = db_path({"settings": {"db_path": "./db"}, "_kb_root": kb_root})
    staging_root = kb_root / "staging"

    record_path = db_root / pmid / "record.json"
    if record_path.exists():
        console.print(f"[yellow]PMID {pmid} already exists in db/[/yellow]")
        raise typer.Exit(code=2)

    enrichment = load_enrichment_from_staging(pmid, staging_root)
    if enrichment is None:
        console.print(f"[red]PMID {pmid} not found in staging[/red]")
        raise typer.Exit(code=3)

    record_dir = create_record(
        pmid=pmid,
        enrichment=enrichment,
        db_root=db_root,
        status="discarded",
        skip_abstract=True,
    )

    remove_from_staging(pmid, staging_root)

    cite_key = "?"
    with open(record_dir / "record.json") as f:
        cite_key = json.load(f).get("id", "?")

    write_event(kb_root, {
        "kind": "discard",
        "pmid": pmid,
        "reason": reason,
        "confidence": confidence,
    })

    conf_str = f", confidence: {confidence}" if confidence is not None else ""
    console.print(
        f"[yellow]{record_dir.relative_to(kb_root)}/  {cite_key}  "
        f"(reason: {reason}{conf_str})[/yellow]"
    )
