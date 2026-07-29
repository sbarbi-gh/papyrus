import json
import shutil
import tempfile
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

from papyrus.core.config import db_path, load_config, locate_kb_root
from papyrus.core.events import write_event
from papyrus.core.record import (
    create_record,
    load_enrichment_from_staging,
    remove_from_staging,
    save_record,
    load_record,
)
from papyrus.core.pubmed import efetch
from papyrus.core.storage import push_pdf

app = typer.Typer()
console = Console()


def _ingest_one(
    pmid: str,
    enrichment: dict | None,
    kb_root: Path,
    db_root: Path,
    staging_root: Path,
    via: str,
    pdf_path: Path | None,
    config: dict,
) -> int:
    record_path = db_root / pmid / "record.json"
    if record_path.exists():
        console.print(f"[yellow]PMID {pmid} already exists in db/[/yellow]")
        return 2

    storages = config.get("fulltext", {}).get("storages", ["local"])
    pdfinbox = Path(config.get("fulltext", {}).get("pdfinbox", "pdfinbox/"))

    if pdf_path is not None:
        mat_fulltext = "external"
        fulltext_location = ""
    else:
        mat_fulltext = "absent"
        fulltext_location = ""

    record_dir = create_record(
        pmid=pmid,
        enrichment=enrichment,
        db_root=db_root,
        status="included",
        materials_fulltext=mat_fulltext,
    )

    if pdf_path is not None:
        external_storages = [s for s in storages if s != "local"]
        if external_storages:
            uploaded = push_pdf(pdf_path, external_storages, pmid)
            fulltext_location = uploaded[0] if uploaded else ""
            console.print(f"[dim]Uploaded to {', '.join(uploaded)}[/dim]")
        else:
            dest = record_dir / "fulltext.pdf"
            shutil.copy2(str(pdf_path), str(dest))
            mat_fulltext = "absent"
            fulltext_location = ""

        rec = load_record(record_dir)
        rec["custom"]["materials"]["fulltext"] = mat_fulltext
        rec["custom"]["materials"]["fulltext_location"] = fulltext_location
        save_record(record_dir, rec)

        if pdf_path.parent == (kb_root / pdfinbox):
            pdf_path.unlink()
            console.print(f"[dim]Removed {pdf_path.name} from pdfinbox/[/dim]")

    if enrichment is not None:
        remove_from_staging(pmid, staging_root)

    cite_key = "?"
    with open(record_dir / "record.json") as f:
        cite_key = json.load(f).get("id", "?")

    write_event(kb_root, {"kind": "ingest", "pmid": pmid, "via": via})
    console.print(f"[green]{record_dir.relative_to(kb_root)}/  {cite_key} (via: {via})[/green]")
    return 0


@app.command()
def ingest(
    pmid: Annotated[
        str,
        typer.Argument(
            help="PubMed ID(s) to ingest, comma-separated (e.g. '123456,789012')"
        ),
    ],
    pdf: Annotated[
        Optional[Path],
        typer.Option("--pdf", help="Path to PDF (only with a single PMID)"),
    ] = None,
    via: Annotated[
        str,
        typer.Option("--via", help="Provenance tag (seed, manual, query)"),
    ] = "",
):
    """Ingest one or more articles into the database.

    If a PMID exists in staging/, it is unstaged (screening flow).
    Otherwise metadata is fetched from PubMed.
    When multiple PMIDs are given, they are batch-fetched from PubMed for speed.
    """
    kb_root = locate_kb_root()
    config = load_config(kb_root)
    db_root = db_path(config)
    staging_root = kb_root / "staging"

    pmids = [p.strip() for p in pmid.split(",") if p.strip()]
    if not pmids:
        console.print("[red]No PMIDs provided[/red]")
        raise typer.Exit(code=2)

    if pdf is not None and len(pmids) > 1:
        console.print("[red]--pdf can only be used with a single PMID[/red]")
        raise typer.Exit(code=2)

    if not via:
        if len(pmids) == 1 and load_enrichment_from_staging(pmids[0], staging_root):
            via = "query"
        else:
            via = "seed"

    staging_pmids: dict[str, dict] = {}
    fetch_pmids: list[str] = []

    for p in pmids:
        enrich = load_enrichment_from_staging(p, staging_root)
        if enrich is not None:
            staging_pmids[p] = enrich
        else:
            fetch_pmids.append(p)

    results: dict[str, dict | None] = {}
    results.update(staging_pmids)

    if fetch_pmids:
        console.print(
            f"[dim]Fetching metadata for {len(fetch_pmids)} PMID(s) from PubMed...[/dim]"
        )
        fetched = efetch(fetch_pmids)
        fetched_map = {f["pmid"]: f for f in fetched}
        for p in fetch_pmids:
            if p in fetched_map:
                results[p] = fetched_map[p]
            else:
                results[p] = None
                console.print(f"[red]PMID {p} not found in PubMed[/red]")

    ok, skip, not_found = 0, 0, 0
    for p in pmids:
        enrich = results.get(p)
        if enrich is None:
            not_found += 1
            continue
        code = _ingest_one(p, enrich, kb_root, db_root, staging_root, via, pdf if len(pmids) == 1 else None, config)
        if code == 0:
            ok += 1
        elif code == 2:
            skip += 1

    suffix = ""
    if skip:
        suffix += f", {skip} already existed"
    if not_found:
        suffix += f", {not_found} not found"
    console.print(f"[bold green]{ok} ingested[/bold green]{suffix}")
