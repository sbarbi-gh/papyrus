from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

from papyrus.core.config import load_config, locate_kb_root
from papyrus.core.events import write_event
from papyrus.core.record import (
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
    """Discard an article — track via event log, no db/ record written."""
    kb_root = locate_kb_root()
    staging_root = kb_root / "staging"

    enrichment = load_enrichment_from_staging(pmid, staging_root)
    if enrichment is None:
        console.print(f"[red]PMID {pmid} not found in staging[/red]")
        raise typer.Exit(code=3)

    title = enrichment.get("title", "")[:80]
    remove_from_staging(pmid, staging_root)

    write_event(kb_root, {
        "kind": "discard",
        "pmid": pmid,
        "reason": reason,
        "confidence": confidence,
    })

    conf_str = f", confidence: {confidence}" if confidence is not None else ""
    reason_str = f" (reason: {reason}{conf_str})" if reason or confidence is not None else ""
    console.print(
        f"[yellow]discarded PMID {pmid}{reason_str}[/yellow]"
        + (f"\n[dim]  {title}...[/dim]" if title else "")
    )
