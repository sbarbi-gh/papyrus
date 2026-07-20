import json
import re
import time
from pathlib import Path

import typer
from rich.console import Console

from papyrus.core.config import db_path, load_config, locate_kb_root
from papyrus.core.constants import SCHEMA_VERSION

app = typer.Typer()
console = Console()


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
