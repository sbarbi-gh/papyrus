import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Annotated, Optional

import numpy as np
import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from papyrus.core.config import db_path, load_config, locate_kb_root
from papyrus.core.pubmed import efetch, elink_neighbors, esearch
from papyrus.core.record import (
    create_record,
    load_enrichment_from_staging,
    remove_from_staging,
)

app = typer.Typer(no_args_is_help=True)
console = Console()

SCHEMA_VERSION = "1.1.0"


def _write_event(kb_root: Path, event: dict):
    event.setdefault("schema_version", "1.0.0")
    event["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(kb_root / "events.jsonl", "a") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


@app.command()
def init(
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite existing files and skip intent.md check"),
    ] = False,
):
    """Initialize a new KB tree in the current directory."""
    cwd = Path.cwd()

    intent = cwd / "intent.md"
    if not intent.exists() and not force:
        console.print(
            "[red]intent.md not found.[/red] "
            "Write the premise (intent.md) before initializing, or use --force."
        )
        raise typer.Exit(code=2)

    dirs = ["db", "concepts", "updates", "staging", "embeddings"]
    conflicts = [d for d in dirs if (cwd / d).exists() and not force]
    if conflicts:
        console.print(f"[red]Existing directories found: {', '.join(conflicts)}[/red]")
        console.print("Use --force to overwrite.")
        raise typer.Exit(code=1)

    for d in dirs:
        (cwd / d).mkdir(parents=True, exist_ok=True)

    (cwd / "events.jsonl").touch(exist_ok=force)

    index = {
        "schema_version": SCHEMA_VERSION,
        "concepts": {},
        "citekey_index": {},
        "pmid_to_citekey": {},
    }
    with open(cwd / "concepts" / "index.json", "w") as f:
        json.dump(index, f, indent=2)
        f.write("\n")

    method_toml = f'''schema_version = "1.0.0"

[settings]
db_path = "./db"

[relevance]
# TODO: derive from intent.md

[source_conversion]
engine = "marker"
fallback = "pymupdf"

[embeddings]
model = "text-embedding-3-small"
dimensions = 1536
provider = "openai"

[fulltext]
[chunking]
[distillation]
[concepts]
[search]
'''
    with open(cwd / "method.toml", "w") as f:
        f.write(method_toml)

    env_example = """# papyrus — API keys (copy to .env and fill in)
# .env is git-ignored; never commit it
OPENAI_API_KEY=sk-...
NCBI_API_KEY=
"""
    with open(cwd / ".env.example", "w") as f:
        f.write(env_example)

    console.print(f"[green]Initialized KB tree (schema {SCHEMA_VERSION})[/green]")
    console.print("  db/ concepts/ updates/ staging/ embeddings/")
    console.print("  events.jsonl  concepts/index.json  method.toml")
    console.print("  [dim].env.example — copy to .env and set your API keys[/dim]")
    if not intent.exists():
        console.print("  [yellow]intent.md NOT FOUND -- create it before running update[/yellow]")


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

    _write_event(kb_root, {"kind": "ingest", "pmid": pmid, "via": via})

    console.print(f"[green]{record_dir.relative_to(kb_root)}/  {cite_key} (via: {via})[/green]")


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

    _write_event(kb_root, {
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


@app.command()
def convert(
    pmid: Annotated[str, typer.Argument(help="PubMed ID to convert fulltext for")],
):
    """Convert PDF to structured markdown.

    Reads db/<pmid>/fulltext.pdf, writes fulltext.md and chunk_index.json.
    Idempotent — skips if fulltext.md already present and chunk_index matches source hash.
    """
    kb_root = locate_kb_root()
    config = load_config(kb_root)
    db_root = db_path(config)

    record_dir = db_root / pmid
    pdf_path = record_dir / "fulltext.pdf"
    md_path = record_dir / "fulltext.md"
    ci_path = record_dir / "chunk_index.json"

    if not pdf_path.exists():
        console.print(f"[red]No fulltext.pdf for PMID {pmid}[/red]")
        raise typer.Exit(code=2)

    engine = config.get("source_conversion", {}).get("engine", "pymupdf")

    if engine == "marker":
        console.print("[dim]marker conversion not yet implemented, using pymupdf4llm[/dim]")
        engine = "pymupdf"

    if engine == "pymupdf":
        try:
            import pymupdf4llm
        except ImportError:
            console.print("[red]pymupdf4llm not installed. Run: uv add pymupdf4llm[/red]")
            raise typer.Exit(code=1)

        console.print(f"[dim]Converting db/{pmid}/fulltext.pdf → fulltext.md (pymupdf4llm)[/dim]")
        md_text = pymupdf4llm.to_markdown(str(pdf_path), write_images=False)

        md_path.write_text(md_text, encoding="utf-8")

        from papyrus.core.chunking import write_chunks

        chunking_cfg = config.get("chunking", {})
        write_chunks(
            pmid=pmid,
            fulltext_path=md_path,
            record_dir=record_dir,
            target_bytes=chunking_cfg.get("target_bytes", 2048),
            overlap_bytes=chunking_cfg.get("overlap_bytes", 256),
            max_chunks=chunking_cfg.get("max_chunks_per_article", 80),
        )

        with open(record_dir / "record.json") as f:
            rec = json.load(f)
        rec["custom"]["materials"]["fulltext_md"] = "present"
        rec["custom"]["materials"]["chunks"] = _count_chunks(record_dir / "chunk_index.json")
        with open(record_dir / "record.json", "w") as f:
            json.dump(rec, f, indent=2, ensure_ascii=False)
            f.write("\n")

        _write_event(kb_root, {
            "kind": "source-converted",
            "pmid": pmid,
            "engine": "pymupdf4llm",
            "pages": _count_pdf_pages(pdf_path),
            "n_chunks": rec["custom"]["materials"]["chunks"],
        })

        n_chunks = rec["custom"]["materials"]["chunks"]
        console.print(f"[green]db/{pmid}/fulltext.md  {n_chunks} chunks[/green]")
    else:
        console.print(f"[red]Unknown conversion engine: {engine}[/red]")
        raise typer.Exit(code=1)


def _count_chunks(ci_path: Path) -> int:
    if not ci_path.exists():
        return 0
    with open(ci_path) as f:
        ci = json.load(f)
    return len(ci.get("chunks", []))


def _count_pdf_pages(pdf_path: Path) -> int:
    try:
        import pymupdf
        doc = pymupdf.open(str(pdf_path))
        n = doc.page_count
        doc.close()
        return n
    except Exception:
        return 0


@app.command()
def check(
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Output machine-readable JSON"),
    ] = False,
):
    """Run integrity checks over the KB tree."""
    kb_root = locate_kb_root()
    config = load_config(kb_root)
    db_root = db_path(config)
    staging_root = kb_root / "staging"
    events_path = kb_root / "events.jsonl"
    method_path = kb_root / "method.toml"
    index_path = kb_root / "concepts" / "index.json"
    concepts_root = kb_root / "concepts"

    results: list[dict] = []

    # 1. folder ↔ record
    _check1(kb_root, db_root, results)

    # 2. record ↔ materials on disk
    _check2(db_root, results)

    # 3. query-id ↔ method.toml
    _check3(config, events_path, results)

    # 4. record version ↔ method.toml version
    _check4(config, db_root, method_path, results)

    # 5. status ↔ event log
    _check5(db_root, events_path, results)

    # 6. included record has ingest event
    _check6(db_root, events_path, results)

    # 7. embedding model coherence
    _check7(config, db_root, results)

    # 8. chunk index ↔ source hash
    _check8(db_root, results)

    # 9. concept ↔ index coherence
    _check9(concepts_root, index_path, results)

    # 10. distillation concept tags
    _check10(db_root, concepts_root, results)

    # 11. consolidated index freshness
    _check11(kb_root, db_root, results)

    # 12. staging coherence
    _check12(db_root, staging_root, results)

    # Also check intent.md
    _check_intent(kb_root, results)

    n_errors = sum(r.get("errors", 0) for r in results)
    n_warnings = sum(r.get("warnings", 0) for r in results)
    n_checks = len(results)

    if json_out:
        report = {
            "result": "pass" if n_errors == 0 else "fail",
            "checks": n_checks,
            "errors": n_errors,
            "warnings": n_warnings,
            "details": results,
        }
        console.print_json(json.dumps(report))
    else:
        table = Table(title="Papyrus Integrity Check")
        table.add_column("#", style="dim")
        table.add_column("Check")
        table.add_column("Status")
        for r in results:
            if r["errors"] > 0:
                status = "[red]FAIL[/red]"
            elif r["warnings"] > 0:
                status = "[yellow]WARN[/yellow]"
            else:
                status = "[green]OK[/green]"
            table.add_row(str(r["id"]), r["name"], status)
        console.print(table)

        error_msgs: list[str] = []
        warn_msgs: list[str] = []
        for r in results:
            error_msgs.extend(r.get("error_list", []))
            warn_msgs.extend(r.get("warn_list", []))

        if error_msgs:
            console.print("\n[red]Errors:[/red]")
            for e in error_msgs:
                console.print(f"  [red]•[/red] {e}")
        if warn_msgs:
            console.print("\n[yellow]Warnings:[/yellow]")
            for w in warn_msgs:
                console.print(f"  [yellow]•[/yellow] {w}")

        console.print(
            f"\n[bold]{n_checks} checks: [green]{n_checks - n_errors} passed[/green]"
            + (f", [red]{n_errors} failed[/red]" if n_errors else "")
            + (f", [yellow]{n_warnings} warnings[/yellow]" if n_warnings else "")
        )

    if n_errors:
        raise typer.Exit(code=1)


def _add_result(results: list[dict], id: int, name: str, errors: list[str], warnings: list[str]):
    results.append({
        "id": id,
        "name": name,
        "errors": len(errors),
        "warnings": len(warnings),
        "error_list": errors,
        "warn_list": warnings,
    })


def _check1(kb_root: Path, db_root: Path, results: list[dict]):
    errors, warnings = [], []
    if not db_root.exists():
        errors.append("db/ directory not found")
        _add_result(results, 1, "folder ↔ record", errors, warnings)
        return
    for d in db_root.iterdir():
        if not d.is_dir():
            continue
        if not (d / "record.json").exists():
            errors.append(f"db/{d.name}/ missing record.json")
    # Check for record.json files without a db/ folder (orphan records)
    # This can't happen with the folder-per-pmid structure, so it's a pass
    _add_result(results, 1, "folder ↔ record", errors, warnings)


def _check2(db_root: Path, results: list[dict]):
    errors, warnings = [], []
    if not db_root.exists():
        _add_result(results, 2, "record ↔ materials", errors, warnings)
        return
    for d in db_root.iterdir():
        if not d.is_dir() or not (d / "record.json").exists():
            continue
        try:
            with open(d / "record.json") as f:
                rec = json.load(f)
        except Exception:
            continue
        mats = rec.get("custom", {}).get("materials", {})
        if mats.get("fulltext") == "present" and not (d / "fulltext.pdf").exists():
            errors.append(f"db/{d.name}/: fulltext: present but no fulltext.pdf")
        if mats.get("fulltext_md") == "present" and not (d / "fulltext.md").exists():
            errors.append(f"db/{d.name}/: fulltext_md: present but no fulltext.md")
        if (d / "chunks.jsonl").exists() and not (d / "chunk_index.json").exists():
            errors.append(f"db/{d.name}/: chunks.jsonl exists but chunk_index.json missing")
        if mats.get("distillation") == "present" and not (d / "distillation.md").exists():
            errors.append(f"db/{d.name}/: distillation: present but no distillation.md")
    _add_result(results, 2, "record ↔ materials", errors, warnings)


def _check3(config: dict, events_path: Path, results: list[dict]):
    errors, warnings = [], []
    if not events_path.exists():
        _add_result(results, 3, "query-id ↔ method.toml", errors, warnings)
        return
    query_ids = {q["id"] for q in config.get("queries", []) if q.get("id")}
    event_query_ids: set[str] = set()
    with open(events_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("kind") == "query-run" and "query_id" in ev:
                event_query_ids.add(ev["query_id"])
    orphan = event_query_ids - query_ids
    if orphan:
        warnings.append(f"Event log references unknown query ids: {', '.join(sorted(orphan))}")
    _add_result(results, 3, "query-id ↔ method.toml", errors, warnings)


def _check4(config: dict, db_root: Path, method_path: Path, results: list[dict]):
    errors, warnings = [], []
    mt_version = config.get("schema_version", "")
    if not db_root.exists():
        _add_result(results, 4, "record version ↔ method.toml version", errors, warnings)
        return
    for d in db_root.iterdir():
        if not d.is_dir():
            continue
        rec_file = d / "record.json"
        if not rec_file.exists():
            continue
        try:
            with open(rec_file) as f:
                rec = json.load(f)
        except Exception:
            continue
        rv = rec.get("schema_version", "")
        if rv and mt_version and rv != mt_version:
            warnings.append(f"db/{d.name}/: schema_version {rv} != method.toml {mt_version}")
    _add_result(results, 4, "record version ↔ method.toml version", errors, warnings)


def _check5(db_root: Path, events_path: Path, results: list[dict]):
    errors, warnings = [], []
    if not events_path.exists() or not db_root.exists():
        _add_result(results, 5, "status ↔ event log", errors, warnings)
        return
    last_event_by_pmid: dict[str, dict] = {}
    with open(events_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("kind") in ("ingest", "discard") and "pmid" in ev:
                pmid = ev["pmid"]
                last_event_by_pmid[pmid] = ev
    for d in db_root.iterdir():
        if not d.is_dir():
            continue
        rec_file = d / "record.json"
        if not rec_file.exists():
            continue
        try:
            with open(rec_file) as f:
                rec = json.load(f)
        except Exception:
            continue
        status = rec.get("custom", {}).get("status", "")
        last_ev = last_event_by_pmid.get(d.name)
        if not last_ev:
            continue
        expected = "included" if last_ev.get("kind") == "ingest" else "discarded"
        if status != expected and status != "excluded":
            warnings.append(
                f"db/{d.name}/: status={status} but last event is {last_ev['kind']}"
            )
    _add_result(results, 5, "status ↔ event log", errors, warnings)


def _check6(db_root: Path, events_path: Path, results: list[dict]):
    errors, warnings = [], []
    if not events_path.exists() or not db_root.exists():
        _add_result(results, 6, "ingest event for included records", errors, warnings)
        return
    ingest_pmids: set[str] = set()
    with open(events_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("kind") == "ingest" and "pmid" in ev:
                ingest_pmids.add(ev["pmid"])
    for d in db_root.iterdir():
        if not d.is_dir():
            continue
        rec_file = d / "record.json"
        if not rec_file.exists():
            continue
        try:
            with open(rec_file) as f:
                rec = json.load(f)
        except Exception:
            continue
        status = rec.get("custom", {}).get("status", "")
        if status == "included" and d.name not in ingest_pmids:
            errors.append(f"db/{d.name}/: status=included but no ingest event in log")
    _add_result(results, 6, "ingest event for included records", errors, warnings)


def _check7(config: dict, db_root: Path, results: list[dict]):
    errors, warnings = [], []
    emb_model = config.get("embeddings", {}).get("model", "")
    emb_dim = config.get("embeddings", {}).get("dimensions", 0)
    if not db_root.exists() or not emb_model:
        _add_result(results, 7, "embedding model coherence", errors, warnings)
        return
    for d in db_root.iterdir():
        if not d.is_dir():
            continue
        rec_file = d / "record.json"
        if not rec_file.exists():
            continue
        try:
            with open(rec_file) as f:
                rec = json.load(f)
        except Exception:
            continue
        m = rec.get("custom", {}).get("materials", {})
        rec_model = m.get("embeddings_model", "")
        if rec_model and rec_model != emb_model:
            warnings.append(f"db/{d.name}/: embeddings model mismatch ({rec_model} != {emb_model})")
    _add_result(results, 7, "embedding model coherence", errors, warnings)


def _check8(db_root: Path, results: list[dict]):
    errors, warnings = [], []
    if not db_root.exists():
        _add_result(results, 8, "chunk index ↔ source hash", errors, warnings)
        return
    import hashlib

    for d in db_root.iterdir():
        if not d.is_dir():
            continue
        ci_file = d / "chunk_index.json"
        if not ci_file.exists():
            continue
        try:
            with open(ci_file) as f:
                ci = json.load(f)
        except Exception:
            warnings.append(f"db/{d.name}/: chunk_index.json parse error")
            continue
        source_hash = ci.get("source_hash", "")
        md_file = d / "fulltext.md"
        if not md_file.exists():
            errors.append(f"db/{d.name}/: chunk_index.json present but fulltext.md missing")
            continue
        with open(md_file, "rb") as f:
            actual = hashlib.sha256(f.read()).hexdigest()
        _, _, expected = source_hash.partition("sha256:")
        if not expected:
            expected = source_hash
        if actual != expected:
            errors.append(f"db/{d.name}/: chunk_index source_hash mismatch")
    _add_result(results, 8, "chunk index ↔ source hash", errors, warnings)


def _check9(concepts_root: Path, index_path: Path, results: list[dict]):
    errors, warnings = [], []
    if not index_path.exists():
        _add_result(results, 9, "concept ↔ index coherence", errors, warnings)
        return
    try:
        with open(index_path) as f:
            idx = json.load(f)
    except Exception:
        errors.append("concepts/index.json parse error")
        _add_result(results, 9, "concept ↔ index coherence", errors, warnings)
        return
    idx_concepts = set(idx.get("concepts", {}).keys())
    disk_concepts = {
        p.stem for p in concepts_root.glob("*.md")
        if p.name not in ("index.json",)
    }
    in_idx_not_disk = idx_concepts - disk_concepts
    in_disk_not_idx = disk_concepts - idx_concepts
    for c in in_idx_not_disk:
        warnings.append(f"Concept '{c}' in index.json but no concepts/{c}.md")
    for c in in_disk_not_idx:
        warnings.append(f"Concept '{c}' has concepts/{c}.md but not in index.json")
    _add_result(results, 9, "concept ↔ index coherence", errors, warnings)


def _check10(db_root: Path, concepts_root: Path, results: list[dict]):
    errors, warnings = [], []
    if not db_root.exists():
        _add_result(results, 10, "distillation concept tags", errors, warnings)
        return
    disk_concepts = {
        p.stem for p in concepts_root.glob("*.md")
        if p.name not in ("index.json",)
    }
    for d in db_root.iterdir():
        if not d.is_dir():
            continue
        dist_file = d / "distillation.md"
        if not dist_file.exists():
            continue
        try:
            text = dist_file.read_text()
        except Exception:
            continue
        import re

        tags = set(re.findall(r"\[\[([^\]]+)\]\]", text))
        for tag in tags:
            if tag not in disk_concepts:
                warnings.append(f"db/{d.name}/: distillation tags unknown concept '{tag}'")
    _add_result(results, 10, "distillation concept tags", errors, warnings)


def _check11(kb_root: Path, db_root: Path, results: list[dict]):
    errors, warnings = [], []
    emb_dir = kb_root / "embeddings"
    if not emb_dir.exists() or not any(emb_dir.glob("*.lance")):
        _add_result(results, 11, "consolidated index freshness", errors, warnings)
        return
    n_records = sum(1 for d in db_root.iterdir() if (d / "record.json").exists()) if db_root.exists() else 0
    try:
        import lancedb

        db = lancedb.connect(str(emb_dir))
        tbl_names = db.table_names()
        for name in tbl_names:
            tbl = db.open_table(name)
            count = tbl.count_rows()
            if count != n_records and n_records > 0:
                warnings.append(f"embeddings/{name}.lance: {count} rows != {n_records} records in db/")
    except ImportError:
        _add_result(results, 11, "consolidated index freshness", errors, warnings)
        return
    except Exception as e:
        errors.append(f"consolidated index error: {e}")
    _add_result(results, 11, "consolidated index freshness", errors, warnings)


def _check12(db_root: Path, staging_root: Path, results: list[dict]):
    errors, warnings = [], []
    if not staging_root.exists():
        _add_result(results, 12, "staging coherence", errors, warnings)
        return
    db_pmids: set[str] = set()
    if db_root.exists():
        db_pmids = {d.name for d in db_root.iterdir() if d.is_dir()}
    for run_dir in staging_root.iterdir():
        if not run_dir.is_dir():
            continue
        for cand in run_dir.iterdir():
            if not cand.is_dir():
                continue
            if cand.name in db_pmids:
                warnings.append(f"staging/{run_dir.name}/{cand.name}/: already in db/ (stale)")
    _add_result(results, 12, "staging coherence", errors, warnings)


def _check_intent(kb_root: Path, results: list[dict]):
    errors, warnings = [], []
    if not (kb_root / "intent.md").exists():
        warnings.append("intent.md not found")
    _add_result(results, 13, "intent.md exists", errors, warnings)


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

    known_pmids = _collect_known_pmids(db_root, staging_root)

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
                _write_event(kb_root, {
                    "kind": "query-run",
                    "query_id": qid,
                    "term": term,
                    "n_found": len(pmids),
                    "n_new": n_found,
                })
                for pmid in new:
                    _write_event(kb_root, {
                        "kind": "query-match",
                        "query_id": qid,
                        "pmid": pmid,
                    })

            all_candidates |= new

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


@app.command()
def embed(
    pmid: Annotated[
        Optional[str],
        typer.Argument(help="PubMed ID to embed (omit with --all or --pending)"),
    ] = None,
    kind: Annotated[
        str,
        typer.Option("--kind", help="chunks (default), distillation, or both"),
    ] = "chunks",
    all_: Annotated[
        bool,
        typer.Option("--all", help="Embed all papers with fulltext"),
    ] = False,
    pending: Annotated[
        bool,
        typer.Option("--pending", help="Embed papers with unconverted or unembedded fulltext"),
    ] = False,
):
    """Chunk and embed source text or distillation."""
    kb_root = locate_kb_root()
    config = load_config(kb_root)
    db_root = db_path(config)

    if pmid is None and not all_ and not pending:
        if kind == "concepts":
            pmids_to_embed = []
        else:
            console.print("[red]Specify a PMID, --all, or --pending[/red]")
            raise typer.Exit(code=2)

    from papyrus.core.embedding import get_provider

    emb_cfg = config.get("embeddings", {})
    provider = get_provider(config)
    model = emb_cfg.get("model", "")
    dims = emb_cfg.get("dimensions", 0)

    pmids_to_embed: list[str] = []
    if all_:
        for d in sorted(db_root.iterdir()):
            if not d.is_dir() or not (d / "record.json").exists():
                continue
            with open(d / "record.json") as f:
                rec = json.load(f)
            if rec.get("custom", {}).get("status") != "included":
                continue
            if (d / "fulltext.md").exists():
                pmids_to_embed.append(d.name)
    elif pending:
        for d in sorted(db_root.iterdir()):
            if not d.is_dir() or not (d / "record.json").exists():
                continue
            with open(d / "record.json") as f:
                rec = json.load(f)
            if rec.get("custom", {}).get("status") != "included":
                continue
            mats = rec.get("custom", {}).get("materials", {})
            has_md = (d / "fulltext.md").exists()
            has_chunks = (d / "chunks.jsonl").exists() and (d / "chunks.jsonl").stat().st_size > 1000
            if has_md and not has_chunks:
                pmids_to_embed.append(d.name)
    else:
        if pmid is not None:
            pmids_to_embed = [pmid]  # type: ignore[list-item]

    n_ok = 0
    for p in pmids_to_embed:
        try:
            _embed_one(kb_root, config, db_root, provider, model, dims, p, kind)
            n_ok += 1
        except typer.Exit as e:
            if e.exit_code != 0:
                console.print(f"[yellow]  skipped {p}[/yellow]")

    console.print(f"[green]Embedded {n_ok}/{len(pmids_to_embed)} papers[/green]")

    if kind in ("concepts", "both"):
        concepts_root = kb_root / "concepts"
        concept_files = sorted(concepts_root.glob("*.md"))
        if not concept_files:
            return
        n_conc = 0
        for cf in concept_files:
            if cf.name == "index.json":
                continue
            cid = cf.stem
            text = cf.read_text()
            console.print(f"[dim]Embedding concept {cid}[/dim]")
            vecs = provider.embed([text])
            conc_rec = {
                "schema_version": "1.0.0",
                "concept_id": cid,
                "text": text,
                "embedding": vecs[0],
                "model": model,
                "dimensions": dims,
                "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            with open(concepts_root / f"{cid}.json", "w") as f:
                json.dump(conc_rec, f, indent=2, ensure_ascii=False)
                f.write("\n")
            n_conc += 1
        console.print(f"[green]Embedded {n_conc} concepts[/green]")


def _embed_one(
    kb_root: Path,
    config: dict,
    db_root: Path,
    provider,
    model: str,
    dims: int,
    pmid: str,
    kind: str,
):
    record_dir = db_root / pmid
    record_path = record_dir / "record.json"
    if not record_path.exists():
        console.print(f"[red]No record for PMID {pmid}[/red]")
        raise typer.Exit(code=2)

    with open(record_path) as f:
        rec = json.load(f)

    if kind in ("chunks", "both"):
        md_path = record_dir / "fulltext.md"
        ci_path = record_dir / "chunk_index.json"
        chunks_path = record_dir / "chunks.jsonl"

        if not md_path.exists():
            console.print(f"[red]No fulltext.md for PMID {pmid}. Run papyrus convert first.[/red]")
            if kind == "chunks":
                raise typer.Exit(code=2)
        elif not ci_path.exists():
            console.print(f"[red]No chunk_index.json for PMID {pmid}. Run papyrus convert first.[/red]")
            if kind == "chunks":
                raise typer.Exit(code=2)
        else:
            with open(md_path) as f:
                fulltext = f.read()
            with open(ci_path) as f:
                ci = json.load(f)
            chunk_defs = ci.get("chunks", [])
            texts = []
            for cd in chunk_defs:
                start = cd["start_byte"]
                end = cd["end_byte"]
                texts.append(fulltext[start:end])

            console.print(f"[dim]Embedding {len(texts)} chunks for PMID {pmid} (model: {model})[/dim]")
            vectors = provider.embed(texts)

            with open(chunks_path, "w") as f:
                for cd, vec, txt in zip(chunk_defs, vectors, texts):
                    line = {
                        "chunk_id": cd["chunk_id"],
                        "text": txt,
                        "embedding": vec,
                        "model": model,
                        "dimensions": dims,
                    }
                    f.write(json.dumps(line, ensure_ascii=False) + "\n")

            rec["custom"]["materials"]["embeddings_model"] = model
            rec["custom"]["materials"]["embeddings_dim"] = dims
            _write_event(kb_root, {
                "kind": "embedded",
                "pmid": pmid,
                "model": model,
                "kind": "source_chunks",
                "n_vectors": len(vectors),
            })
            console.print(f"[green]  {len(vectors)} chunk vectors written to chunks.jsonl[/green]")

    if kind in ("distillation", "both"):
        dist_path = record_dir / "distillation.md"
        dist_json_path = record_dir / "distillation.json"

        if not dist_path.exists():
            console.print(f"[yellow]No distillation.md for PMID {pmid}[/yellow]")
        else:
            text = dist_path.read_text()
            console.print(f"[dim]Embedding distillation for PMID {pmid}[/dim]")
            vectors = provider.embed([text])

            dist_record = {
                "schema_version": "1.0.0",
                "pmid": pmid,
                "text": text,
                "embedding": vectors[0],
                "model": model,
                "dimensions": dims,
                "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            with open(dist_json_path, "w") as f:
                json.dump(dist_record, f, indent=2, ensure_ascii=False)
                f.write("\n")

            rec["custom"]["materials"]["embeddings_model"] = model
            rec["custom"]["materials"]["embeddings_dim"] = dims
            rec["custom"]["materials"]["distillation"] = "present"
            _write_event(kb_root, {
                "kind": "embedded",
                "pmid": pmid,
                "model": model,
                "kind": "distillation",
                "n_vectors": 1,
            })
            console.print(f"[green]  distillation embedding written to distillation.json[/green]")

    with open(record_path, "w") as f:
        json.dump(rec, f, indent=2, ensure_ascii=False)
        f.write("\n")


@app.command()
def index():
    """Regenerate concepts/index.json from records and concept files."""
    kb_root = locate_kb_root()
    config = load_config(kb_root)
    db_root = db_path(config)
    concepts_root = kb_root / "concepts"
    index_path = concepts_root / "index.json"

    concepts_map: dict = {}
    citekey_index: dict[str, str] = {}
    pmid_to_citekey: dict[str, str] = {}

    # Read concepts from concept files
    if concepts_root.exists():
        for md_file in concepts_root.glob("*.md"):
            cid = md_file.stem
            with open(md_file) as f:
                text = f.read()
            related = []
            for match in re.findall(r"\[\[([^\]]+)\]\]", text):
                if match != cid:
                    related.append(match)
            mtime = md_file.stat().st_mtime
            concepts_map[cid] = {
                "pmids": [],
                "related": related,
                "n_articles": 0,
                "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime)),
            }

    # Sync concepts from distillation.md [[tags]] into record.json
    if db_root.exists():
        for d in sorted(db_root.iterdir()):
            if not d.is_dir():
                continue
            dist_path = d / "distillation.md"
            if not dist_path.exists():
                continue
            rec_path = d / "record.json"
            if not rec_path.exists():
                continue
            dist_text = dist_path.read_text()
            tags = list(set(re.findall(r"\[\[([^\]]+)\]\]", dist_text)))
            if not tags:
                continue
            with open(rec_path) as f:
                rec = json.load(f)
            existing = set(rec.get("custom", {}).get("concepts", []))
            merged = sorted(existing | set(tags))
            if merged != rec.get("custom", {}).get("concepts", []):
                rec["custom"]["concepts"] = merged
                with open(rec_path, "w") as f:
                    json.dump(rec, f, indent=2, ensure_ascii=False)
                    f.write("\n")

    # Read concepts from record files
    if db_root.exists():
        for d in sorted(db_root.iterdir()):
            if not d.is_dir():
                continue
            rec_path = d / "record.json"
            if not rec_path.exists():
                continue
            with open(rec_path) as f:
                rec = json.load(f)
            cite_key = rec.get("id", "")
            pmid = rec.get("custom", {}).get("pmid", d.name)
            if cite_key and pmid:
                citekey_index[cite_key] = pmid
                pmid_to_citekey[pmid] = cite_key
            for cid in rec.get("custom", {}).get("concepts", []):
                if cid not in concepts_map:
                    concepts_map[cid] = {
                        "pmids": [],
                        "related": [],
                        "n_articles": 0,
                        "last_updated": "",
                    }
                if pmid not in concepts_map[cid]["pmids"]:
                    concepts_map[cid]["pmids"].append(pmid)
                concepts_map[cid]["n_articles"] = len(concepts_map[cid]["pmids"])

    index = {
        "schema_version": SCHEMA_VERSION,
        "concepts": concepts_map,
        "citekey_index": citekey_index,
        "pmid_to_citekey": pmid_to_citekey,
    }
    with open(index_path, "w") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)
        f.write("\n")

    n_concepts = len(concepts_map)
    n_articles = len(citekey_index)
    console.print(f"[green]concepts/index.json  {n_concepts} concepts, {n_articles} articles[/green]")


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Search query text")],
    kind: Annotated[
        str,
        typer.Option("--kind", help="concepts, distillations, chunks, or auto (default)"),
    ] = "auto",
    top_k: Annotated[int, typer.Option("--top-k", help="Number of results")] = 10,
    cite: Annotated[
        Optional[str],
        typer.Option("--cite", help="Restrict search to chunks from this cite key"),
    ] = None,
):
    """Semantic search over the KB."""
    if not query.strip():
        console.print("[red]Query cannot be empty.[/red]")
        raise typer.Exit(code=2)

    kb_root = locate_kb_root()
    config = load_config(kb_root)
    db_root = db_path(config)
    concepts_root = kb_root / "concepts"

    from papyrus.core.embedding import get_provider, load_embeddings_from_tree

    # If citing, resolve cite key to PMID
    restrict_pmid: str | None = None
    if cite:
        index_path = concepts_root / "index.json"
        if index_path.exists():
            with open(index_path) as f:
                idx = json.load(f)
            restrict_pmid = idx.get("citekey_index", {}).get(cite)
            if not restrict_pmid:
                console.print(f"[red]Cite key '{cite}' not found[/red]")
                raise typer.Exit(code=2)
        else:
            console.print("[red]No concepts/index.json — run papyrus index first[/red]")
            raise typer.Exit(code=2)

    provider = get_provider(config)
    query_vec = provider.embed([query])[0]
    q = np.array(query_vec, dtype=np.float32)

    vectors, meta = load_embeddings_from_tree(db_root, concepts_root)
    if len(vectors) == 0:
        console.print("[yellow]No embeddings found. Run papyrus embed first.[/yellow]")
        raise typer.Exit(code=0)

    if restrict_pmid:
        mask = [m.get("pmid") == restrict_pmid for m in meta]
        vectors = vectors[mask]
        meta = [m for i, m in enumerate(meta) if mask[i]]

    if kind != "auto":
        want = {"chunks": "chunk", "distillations": "distillation", "concepts": "concept"}.get(kind, kind)
        kind_mask = [m.get("kind") == want for m in meta]
        vectors = vectors[kind_mask]
        meta = [m for i, m in enumerate(meta) if kind_mask[i]]

    if len(vectors) == 0:
        console.print("[yellow]No matching embeddings.[/yellow]")
        raise typer.Exit(code=0)

    norms = np.linalg.norm(vectors, axis=1)
    q_norm = np.linalg.norm(q)
    scores = np.dot(vectors, q) / (norms * q_norm + 1e-8)
    top_idx = np.argsort(scores)[-top_k:][::-1]

    table = Table(title=f"Search: {query[:60]}")
    table.add_column("Score", style="green")
    table.add_column("Kind")
    table.add_column("PMID/Cite", style="cyan")
    table.add_column("Text")
    for idx in top_idx:
        score = float(scores[idx])
        m = meta[idx]
        pmid = m.get("pmid", "")
        cid = m.get("concept_id", "")
        ref = pmid or cid
        text_preview = m.get("text", "")[:120].replace("\n", " ")
        table.add_row(f"{score:.3f}", m.get("kind", ""), ref, text_preview)
    console.print(table)


def _collect_known_pmids(db_root: Path, staging_root: Path) -> set[str]:
    known: set[str] = set()
    if db_root.exists():
        for d in db_root.iterdir():
            if d.is_dir() and (d / "record.json").exists():
                known.add(d.name)
    if staging_root.exists():
        for d in staging_root.iterdir():
            if d.is_dir() and d.name not in known:
                for cand in d.iterdir():
                    if cand.is_dir():
                        known.add(cand.name)
    return known


def main():
    # Load .env from KB root or CWD
    env_path = Path.cwd() / ".env"
    for p in [Path.cwd(), *Path.cwd().parents]:
        if (p / ".env").exists():
            env_path = p / ".env"
            break
    load_dotenv(dotenv_path=env_path)
    app()


if __name__ == "__main__":
    env_path = Path.cwd() / ".env"
    for p in [Path.cwd(), *Path.cwd().parents]:
        if (p / ".env").exists():
            env_path = p / ".env"
            break
    load_dotenv(dotenv_path=env_path)
    app()
