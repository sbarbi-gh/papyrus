import hashlib
import json
import re
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from papyrus.core.config import db_path, load_config, locate_kb_root
from papyrus.core.events import read_events
from papyrus.core.storage import check_pdf

app = typer.Typer()
console = Console()


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
    deferred_root = kb_root / "deferred"
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

    # 13. external fulltext
    _check13(config, db_root, results)

    # 14. deferred coherence
    _check14(db_root, deferred_root, staging_root, events_path, results)

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
        _add_result(results, 1, "folder ↔ record + intent.md", errors, warnings)
        return
    for d in db_root.iterdir():
        if not d.is_dir():
            continue
        if not (d / "record.json").exists():
            errors.append(f"db/{d.name}/ missing record.json")
    if not (kb_root / "intent.md").exists():
        warnings.append("intent.md not found")
    _add_result(results, 1, "folder ↔ record + intent.md", errors, warnings)


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
        ft_status = mats.get("fulltext", "")
        if ft_status == "present" and not (d / "fulltext.pdf").exists():
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
    for ev in read_events(events_path):
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
            errors.append(f"db/{d.name}/: schema_version {rv} != method.toml {mt_version}")
    _add_result(results, 4, "record version ↔ method.toml version", errors, warnings)


def _check5(db_root: Path, events_path: Path, results: list[dict]):
    errors, warnings = [], []
    if not events_path.exists():
        _add_result(results, 5, "status ↔ event log", errors, warnings)
        return
    last_event_by_pmid: dict[str, dict] = {}
    lifecycle_kinds = {"ingest", "discard", "defer", "revisit"}
    for ev in read_events(events_path):
        if ev.get("kind") in lifecycle_kinds and "pmid" in ev:
            last_event_by_pmid[ev["pmid"]] = ev

    db_pmids: set[str] = set()
    if db_root.exists():
        for d in db_root.iterdir():
            if not d.is_dir():
                continue
            rec_file = d / "record.json"
            if not rec_file.exists():
                continue
            db_pmids.add(d.name)
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

    for pmid, ev in last_event_by_pmid.items():
        if pmid in db_pmids:
            continue
        if ev["kind"] == "ingest":
            warnings.append(f"PMID {pmid}: ingest event but no db/ record")
    _add_result(results, 5, "status ↔ event log", errors, warnings)


def _check6(db_root: Path, events_path: Path, results: list[dict]):
    errors, warnings = [], []
    if not events_path.exists() or not db_root.exists():
        _add_result(results, 6, "ingest event for included records", errors, warnings)
        return
    ingest_pmids: set[str] = set()
    for ev in read_events(events_path):
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


def _check13(config: dict, db_root: Path, results: list[dict]):
    errors, warnings = [], []
    storages = config.get("fulltext", {}).get("storages", [])
    external_storages = [s for s in storages if s != "local"]
    if not external_storages:
        _add_result(results, 13, "external fulltext", errors, warnings)
        return
    if not db_root.exists():
        _add_result(results, 13, "external fulltext", errors, warnings)
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
        if rec.get("custom", {}).get("materials", {}).get("fulltext") != "external":
            continue
        pmid = d.name
        found = check_pdf(pmid, external_storages)
        missing = [url for url, exists in found.items() if not exists]
        if missing:
            warnings.append(f"db/{d.name}/: external fulltext missing on {', '.join(missing)}")
    _add_result(results, 13, "external fulltext", errors, warnings)


def _check14(db_root: Path, deferred_root: Path, staging_root: Path,
             events_path: Path, results: list[dict]):
    errors, warnings = [], []
    if not deferred_root.exists():
        _add_result(results, 14, "deferred coherence", errors, warnings)
        return

    last_event_by_pmid: dict[str, dict] = {}
    for ev in read_events(events_path):
        if ev.get("kind") in ("defer", "ingest", "discard", "revisit") and "pmid" in ev:
            last_event_by_pmid[ev["pmid"]] = ev

    db_pmids: set[str] = set()
    if db_root.exists():
        db_pmids = {d.name for d in db_root.iterdir() if d.is_dir()}
    staging_pmids: set[str] = set()
    if staging_root.exists():
        for run_dir in staging_root.iterdir():
            if run_dir.is_dir():
                for cand in run_dir.iterdir():
                    if cand.is_dir():
                        staging_pmids.add(cand.name)

    for d in deferred_root.iterdir():
        if not d.is_dir():
            continue
        if not (d / "candidate.json").exists():
            errors.append(f"deferred/{d.name}/: missing candidate.json")
            continue
        if d.name in db_pmids:
            warnings.append(f"deferred/{d.name}/: also in db/ (stale)")
        if d.name in staging_pmids:
            warnings.append(f"deferred/{d.name}/: also in staging (stale)")
        last_ev = last_event_by_pmid.get(d.name)
        if last_ev is None:
            warnings.append(f"deferred/{d.name}/: no defer event in log")
        elif last_ev["kind"] != "defer":
            warnings.append(
                f"deferred/{d.name}/: last event is {last_ev['kind']} (expected defer)"
            )

    _add_result(results, 14, "deferred coherence", errors, warnings)
