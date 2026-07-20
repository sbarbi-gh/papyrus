import re
from pathlib import Path


def _base_name(authors: list[dict], year: str | None, container_title: str | None, pmid: str) -> str:
    if authors and authors[0].get("family"):
        family = authors[0]["family"].lower()
        family = re.sub(r"[^a-z]", "", family)
        return family
    if container_title:
        token = container_title.split()[0].lower()
        token = re.sub(r"[^a-z]", "", token)
        return token
    return f"pmid{pmid}"


def _year_suffix(year: str | None) -> str:
    if year:
        return str(year)
    return "nd"


def _suffixes():
    letters = "abcdefghijklmnopqrstuvwxyz"
    for c in letters:
        yield c
    for c1 in letters:
        for c2 in letters:
            yield c1 + c2


def existing_cite_keys(db_root: Path) -> set[str]:
    existing: set[str] = set()
    if not db_root.exists():
        return existing
    for record_path in db_root.glob("*/record.json"):
        try:
            with open(record_path) as f:
                import json

                record = json.load(f)
            if "id" in record:
                existing.add(record["id"])
        except Exception:
            pass
    return existing


def generate_cite_key(
    pmid: str,
    authors: list[dict],
    issued: dict | None,
    container_title: str | None,
    db_root: Path,
) -> str:
    year = None
    if issued and "date-parts" in issued and issued["date-parts"]:
        parts = issued["date-parts"][0]
        if parts:
            year = str(parts[0])

    base = _base_name(authors, year, container_title, pmid)
    year_part = _year_suffix(year)
    stem = f"{base}{year_part}"

    existing = existing_cite_keys(db_root)
    if stem not in existing:
        return stem
    for suffix in _suffixes():
        candidate = f"{stem}{suffix}"
        if candidate not in existing:
            return candidate
    raise RuntimeError(f"Cannot generate unique cite key for PMID {pmid}: {stem} exhausted")
