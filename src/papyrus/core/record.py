import json
import shutil
from pathlib import Path
from typing import Any

from papyrus.core.citekey import generate_cite_key
from papyrus.core.constants import ABSTRACT_FILE, CANDIDATE_FILE, RECORD_FILE, SCHEMA_VERSION


def create_record(
    pmid: str,
    enrichment: dict[str, Any],
    db_root: Path,
    status: str = "included",
    materials_fulltext: str = "absent",
    skip_abstract: bool = False,
) -> Path:
    record_dir = db_root / pmid
    record_dir.mkdir(parents=True, exist_ok=True)

    authors = enrichment.get("authors", [])
    year = enrichment.get("year")
    journal = enrichment.get("journal")
    cite_key = generate_cite_key(pmid, authors, _make_issued(year), journal, db_root)

    record = {
        "schema_version": SCHEMA_VERSION,
        "id": cite_key,
        "type": "article-journal",
        "title": enrichment.get("title", ""),
        "author": [{"family": a["family"], "given": a["given"]} for a in authors],
        "issued": _make_issued(year),
        "DOI": enrichment.get("doi", ""),
        "container-title": journal or "",
        "custom": {
            "pmid": pmid,
            "status": status,
            "references": enrichment.get("references", []),
            "citekey_aliases": [],
            "materials": {
                "fulltext": materials_fulltext,
                "fulltext_location": "",
                "fulltext_md": "absent",
                "supp": False,
                "distillation": "absent",
                "chunks": 0,
                "embeddings_model": "",
                "embeddings_dim": 0,
            },
            "concepts": [],
        },
    }

    with open(record_dir / "record.json", "w") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)
        f.write("\n")

    if not skip_abstract:
        abstract = enrichment.get("abstract", "")
        if abstract:
            with open(record_dir / "abstract.txt", "w") as f:
                f.write(abstract)
                f.write("\n")

    return record_dir


def _make_issued(year: str | None) -> dict[str, Any]:
    if year:
        try:
            y = int(year)
            return {"date-parts": [[y]]}
        except (ValueError, TypeError):
            pass
    return {"date-parts": [[]]}


def load_enrichment_from_staging(pmid: str, staging_root: Path) -> dict[str, Any] | None:
    for run_dir in staging_root.iterdir():
        if not run_dir.is_dir():
            continue
        candidate_file = run_dir / pmid / "candidate.json"
        if candidate_file.exists():
            with open(candidate_file) as f:
                return json.load(f)
    return None


def remove_from_staging(pmid: str, staging_root: Path):
    for run_dir in sorted(staging_root.iterdir()):
        if not run_dir.is_dir():
            continue
        candidate_dir = run_dir / pmid
        if candidate_dir.is_dir():
            shutil.rmtree(candidate_dir)
        remaining = [p for p in run_dir.iterdir() if p.is_dir()]
        if not remaining:
            shutil.rmtree(run_dir)
            return


def load_record(record_dir: Path) -> dict[str, Any]:
    with open(record_dir / RECORD_FILE) as f:
        return json.load(f)


def save_record(record_dir: Path, rec: dict[str, Any]) -> None:
    with open(record_dir / RECORD_FILE, "w") as f:
        json.dump(rec, f, indent=2, ensure_ascii=False)
        f.write("\n")
