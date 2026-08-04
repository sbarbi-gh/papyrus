from typing import Annotated

import typer
from rich.console import Console

from papyrus.core.config import locate_kb_root
from papyrus.core.events import write_event
from papyrus.core.record import move_to_deferred

app = typer.Typer()
console = Console()


@app.command()
def defer(
    pmid: Annotated[str, typer.Argument(help="PubMed ID to defer for later review")],
):
    """Move a paper from staging to deferred — park it for later."""
    kb_root = locate_kb_root()
    staging_root = kb_root / "staging"
    deferred_root = kb_root / "deferred"

    enrichment = move_to_deferred(pmid, staging_root, deferred_root)
    if enrichment is None:
        console.print(f"[red]PMID {pmid} not found in staging[/red]")
        raise typer.Exit(code=3)

    write_event(kb_root, {"kind": "defer", "pmid": pmid})

    title = enrichment.get("title", "")[:80]
    console.print(
        f"[blue]deferred PMID {pmid}[/blue]"
        + (f"\n[dim]  {title}...[/dim]" if title else "")
    )
