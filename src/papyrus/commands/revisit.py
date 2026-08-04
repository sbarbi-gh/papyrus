from typing import Annotated

import typer
from rich.console import Console

from papyrus.core.config import locate_kb_root
from papyrus.core.events import write_event
from papyrus.core.record import move_from_deferred

app = typer.Typer()
console = Console()


@app.command()
def revisit(
    pmid: Annotated[str, typer.Argument(help="PubMed ID to return to the staging queue")],
):
    """Return a deferred paper to the staging queue for re-screening."""
    kb_root = locate_kb_root()
    deferred_root = kb_root / "deferred"
    staging_root = kb_root / "staging"

    enrichment = move_from_deferred(pmid, deferred_root, staging_root)
    if enrichment is None:
        console.print(f"[red]PMID {pmid} not found in deferred/[/red]")
        raise typer.Exit(code=3)

    write_event(kb_root, {"kind": "revisit", "pmid": pmid})

    title = enrichment.get("title", "")[:80]
    console.print(
        f"[cyan]revisited PMID {pmid} → staging[/cyan]"
        + (f"\n[dim]  {title}...[/dim]" if title else "")
    )
