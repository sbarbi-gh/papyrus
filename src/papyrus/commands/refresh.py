import json
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

from papyrus.core.config import db_path, load_config, locate_kb_root
from papyrus.core.events import write_event
from papyrus.core.pubmed import efetch
from papyrus.core.record import load_record, save_record

app = typer.Typer()
console = Console()

# Bibliographic top-level fields backfilled by refresh-meta, mapped from the
# PubMed enrichment dict. Only empty/missing values are filled; everything
# else in the record (custom.*, status, materials, concepts) is left alone.
_BIBLIO_MAP = {
    "volume": "volume",
    "issue": "issue",
    "page": "pages",
    "ISSN": "issn",
    "container-title-short": "journal_abbrev",
}


def _collect_all_pmids(db_root: Path) -> list[str]:
    return sorted(
        d.name for d in db_root.iterdir()
        if d.is_dir() and (d / "record.json").exists()
    )


def _apply_enrichment(rec: dict, article: dict) -> bool:
    """Fill empty bibliographic fields from a PubMed enrichment dict.

    Returns True if the record was modified. Fields already populated are
    never overwritten — this is a backfill, not a re-import.
    """
    changed = False
    for rec_key, enrich_key in _BIBLIO_MAP.items():
        value = article.get(enrich_key)
        if not value:
            continue
        if not rec.get(rec_key):
            rec[rec_key] = value
            changed = True
    return changed


@app.command()
def refresh_meta(
    pmid: Annotated[
        Optional[str],
        typer.Argument(help="Only refresh this PMID (default: all records in db/)"),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Report what would change without writing"),
    ] = False,
):
    """Backfill metadata (volume, issue, pages, ISSN) for existing records.

    Fetches records from PubMed in batches and fills only the bibliographic
    fields that are currently missing or empty in each db/<pmid>/record.json.
    Does not touch title/author/journal (already present), custom process
    state, status, or concepts.
    """
    kb_root = locate_kb_root()
    config = load_config(kb_root)
    db_root = db_path(config)

    pmids = [pmid] if pmid else _collect_all_pmids(db_root)
    if not pmids:
        console.print("[yellow]No records in db/.[/yellow]")
        raise typer.Exit(code=0)
    console.print(f"[dim]Refreshing metadata for {len(pmids)} record(s)...[/dim]")

    articles_by_pmid: dict[str, dict] = {}
    batch_size = 50
    for i in range(0, len(pmids), batch_size):
        batch = pmids[i : i + batch_size]
        console.print(f"  fetching {i + 1}–{min(i + batch_size, len(pmids))}")
        fetched = efetch(batch)
        for article in fetched:
            articles_by_pmid[article["pmid"]] = article

    n_updated = 0
    n_missing = 0
    for p in pmids:
        record_dir = db_root / p
        rec = load_record(record_dir)
        article = articles_by_pmid.get(p)
        if article is None:
            console.print(f"[yellow]  PMID {p}: not found in PubMed[/yellow]")
            n_missing += 1
            continue
        if _apply_enrichment(rec, article):
            if dry_run:
                console.print(f"[dim]  would update {p}[/dim]")
            else:
                save_record(record_dir, rec)
                write_event(kb_root, {
                    "kind": "refresh-meta",
                    "pmid": p,
                    "fields": [k for k in _BIBLIO_MAP if rec.get(k)],
                })
            n_updated += 1

    verb = "Would refresh" if dry_run else "Refreshed"
    suffix = f", {n_missing} not found in PubMed" if n_missing else ""
    console.print(f"[green]{verb} {n_updated}/{len(pmids)} records[/green]{suffix}")