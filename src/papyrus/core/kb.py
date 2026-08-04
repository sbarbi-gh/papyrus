import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from papyrus.core.config import load_config, locate_kb_root
from papyrus.core.constants import (
    DIR_CONCEPTS,
    DIR_DB,
    DIR_DEFERRED,
    DIR_EMBEDDINGS,
    DIR_STAGING,
    DIR_UPDATES,
)


@dataclass(frozen=True)
class KB:
    root: Path
    config: dict[str, Any] = field(repr=False)

    @classmethod
    def from_cwd(cls, path: Path | None = None) -> "KB":
        root = locate_kb_root(path)
        config = load_config(root)
        return cls(root=root, config=config)

    @property
    def db(self) -> Path:
        return self.root / self.config.get("settings", {}).get("db_path", DIR_DB)

    @property
    def staging(self) -> Path:
        return self.root / DIR_STAGING

    @property
    def deferred(self) -> Path:
        return self.root / DIR_DEFERRED

    @property
    def concepts(self) -> Path:
        return self.root / DIR_CONCEPTS

    @property
    def updates(self) -> Path:
        return self.root / DIR_UPDATES

    @property
    def embeddings(self) -> Path:
        return self.root / DIR_EMBEDDINGS

    @property
    def events_path(self) -> Path:
        return self.root / "events.jsonl"

    def record_dir(self, pmid: str) -> Path:
        return self.db / pmid


def collect_known_pmids(kb: KB) -> set[str]:
    known: set[str] = set()

    if kb.db.exists():
        for d in kb.db.iterdir():
            if d.is_dir() and (d / "record.json").exists():
                known.add(d.name)

    if kb.events_path.exists():
        with open(kb.events_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("kind") in ("ingest", "discard") and "pmid" in ev:
                    known.add(ev["pmid"])

    if kb.staging.exists():
        for d in kb.staging.iterdir():
            if d.is_dir():
                for cand in d.iterdir():
                    if cand.is_dir() and cand.name not in known:
                        known.add(cand.name)

    if kb.deferred.exists():
        for d in kb.deferred.iterdir():
            if d.is_dir() and d.name not in known:
                known.add(d.name)

    return known
