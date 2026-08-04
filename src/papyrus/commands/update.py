import json
import time
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

from papyrus.core.config import db_path, load_config, locate_kb_root
from papyrus.core.events import write_event
from papyrus.core.kb import KB, collect_known_pmids
from papyrus.core.pubmed import efetch, elink_neighbors, esearch

app = typer.Typer()
console = Console()


def _promote_deferred(kb_root: Path, deferred_root: Path, staging_run: Path, count: int) -> list[str]:
    promoted: list[str] = []
    if not deferred_root.exists():
        return promoted
    candidates = sorted(
        [d for d in deferred_root.iterdir() if d.is_dir() and (d / "candidate.json").exists()]
    )
    for cand_dir in candidates[:count]:
        pmid = cand_dir.name
        dest = staging_run / pmid
        dest.mkdir(exist_ok=True)
        with open(cand_dir / "candidate.json") as src, open(dest / "candidate.json", "w") as dst:
            dst.write(src.read())
        import shutil
        shutil.rmtree(cand_dir)
        write_event(kb_root, {"kind": "revisit", "pmid": pmid, "via": "auto-promote"})
        promoted.append(pmid)
    return promoted


@app.command()
def update(
    query_id: Annotated[
        Optional[str],
        typer.Option("--query-id", help="Run a single query from method.toml"),
    ] = None,
    related_to: Annotated[
        Optional[str],
        typer.Option("--related-to", help="ELink neighbor expansion from this PMID"),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Fetch and dedup, do not write staging or events"),
    ] = False,
):
    """Query PubMed and write enriched candidates to staging."""
    kb_root = locate_kb_root()
    config = load_config(kb_root)
    db_root = db_path(config)
    staging_root = kb_root / "staging"
    deferred_root = kb_root / "deferred"

    kb = KB(root=kb_root, config=config)
    known_pmids = collect_known_pmids(kb)

    all_candidates: set[str] = set()

    if related_to:
        console.print(f"[dim]ELink neighbor expansion from PMID {related_to}[/dim]")
        neighbor_ids = elink_neighbors(related_to)
        new = set(neighbor_ids) - known_pmids
        matched = len(neighbor_ids) - len(new)
        all_candidates = new
        console.print(f"  {len(neighbor_ids)} neighbors ({len(new)} new, {matched} matched)")
        if len(neighbor_ids) == 0:
            console.print(
                "[yellow]  No neighbors found — the PMID may be too recent for ELink data, "
                "or has no indexed references/citations yet.[/yellow]"
            )
        query_run_id = f"related-{related_to}"
    else:
        queries = config.get("queries", [])
        if not queries:
            console.print("[red]No queries defined in method.toml[/red]")
            raise typer.Exit(code=1)

        for q in queries:
            if query_id and q.get("id") != query_id:
                continue
            if not q.get("enabled", True):
                continue

            qid = q["id"]
            term = q.get("term", "")
            if not term:
                continue

            console.print(f"[dim]Query {qid}: {term[:80]}...[/dim]")
            pmids = esearch(term)
            new = set(pmids) - known_pmids
            matched = len(pmids) - len(new)
            n_found = len(new)
            console.print(f"  {len(pmids)} results ({n_found} new, {matched} matched)")

            if not dry_run:
                write_event(kb_root, {
                    "kind": "query-run",
                    "query_id": qid,
                    "term": term,
                    "n_found": len(pmids),
                    "n_new": n_found,
                })
                for pmid in new:
                    write_event(kb_root, {
                        "kind": "query-match",
                        "query_id": qid,
                        "pmid": pmid,
                    })

            all_candidates |= new

    n_new = len(all_candidates)
    papers_per_week = config.get("update", {}).get("papers_per_week", 5)

    if n_new < papers_per_week and not dry_run and deferred_root.exists():
        shortage = papers_per_week - n_new
        deferred_count = sum(
            1 for d in deferred_root.iterdir()
            if d.is_dir() and (d / "candidate.json").exists()
        )
        if deferred_count > 0:
            to_promote = min(shortage, deferred_count)
            console.print(
                f"[dim]{n_new} new (target: {papers_per_week}/week), "
                f"promoting {to_promote} from deferred/[/dim]"
            )
            ts = time.strftime("%Y-%m-%dT%H%MZ", time.gmtime())
            run_dir = staging_root / ts
            run_dir.mkdir(parents=True, exist_ok=True)
            promoted = _promote_deferred(kb_root, deferred_root, run_dir, to_promote)
            all_candidates |= set(promoted)

    if not all_candidates:
        console.print("[yellow]No new candidates found.[/yellow]")
        raise typer.Exit(code=0)

    if dry_run:
        console.print(f"[yellow]Dry run: {len(all_candidates)} candidates would be written.[/yellow]")
        return

    ts = time.strftime("%Y-%m-%dT%H%MZ", time.gmtime())
    run_dir = staging_root / ts
    run_dir.mkdir(parents=True, exist_ok=True)

    candidate_list = sorted(all_candidates)
    console.print(f"[dim]Fetching metadata for {len(candidate_list)} candidates...[/dim]")

    batch_size = 50
    for i in range(0, len(candidate_list), batch_size):
        batch = candidate_list[i : i + batch_size]
        console.print(f"  fetching {i + 1}–{min(i + batch_size, len(candidate_list))}")
        enriched = efetch(batch)
        for article in enriched:
            pmid = article["pmid"]
            cand_dir = run_dir / pmid
            cand_dir.mkdir(parents=True, exist_ok=True)
            with open(cand_dir / "candidate.json", "w") as f:
                json.dump(article, f, indent=2, ensure_ascii=False)

    existing_staging = sum(1 for _ in staging_root.glob("*/candidate.json"))
    console.print(
        f"[green]{run_dir.relative_to(kb_root)}/  {len(candidate_list)} candidates "
        f"(+{existing_staging} pending in other staging runs)[/green]"
    )
