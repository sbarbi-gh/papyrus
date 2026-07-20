from pathlib import Path
from typing import Any

import tomlkit

DEFAULT_SCHEMA_VERSION = "1.0.0"

DEFAULT_CONFIG: dict[str, Any] = {
    "settings": {"db_path": "./db", "primary_db": "./db", "chunk_size": 60},
    "fulltext": {"max_attempts": 5, "backoff": "exponential", "keep_on_wishlist": True},
    "relevance": {},
    "source_conversion": {"engine": "marker", "fallback": "pymupdf", "max_pages": 100, "image_handling": "reference"},
    "chunking": {"method": "section", "target_bytes": 2048, "overlap_bytes": 256, "max_chunks_per_article": 80},
    "embeddings": {"model": "text-embedding-3-small", "dimensions": 1536, "provider": "openai", "batch_size": 50},
    "distillation": {},
    "concepts": {"max_concepts": 150, "min_articles_per_concept": 2, "allow_orphan": False},
    "search": {"consolidated_index": True, "index_format": "lance"},
    "queries": [],
}


def locate_kb_root(path: Path | None = None) -> Path:
    start = path or Path.cwd()
    for p in [start, *start.parents]:
        if (p / "method.toml").exists():
            return p
    raise FileNotFoundError(
        "method.toml not found. Run 'papyrus init' in a KB directory or cd into one."
    )


def load_config(kb_root: Path | None = None) -> dict[str, Any]:
    root = kb_root or locate_kb_root()
    path = root / "method.toml"
    with open(path, "r") as f:
        raw = tomlkit.parse(f.read())
    merged = _merge_defaults(raw)
    merged["_kb_root"] = root
    return merged


def _merge_defaults(raw: dict[str, Any]) -> dict[str, Any]:
    config: dict[str, Any] = {}
    for section, defaults in DEFAULT_CONFIG.items():
        if isinstance(defaults, dict):
            config[section] = {**defaults, **(raw.get(section, {}))}
        else:
            config[section] = raw.get(section, defaults)
    config["schema_version"] = raw.get("schema_version", DEFAULT_SCHEMA_VERSION)
    return config


def db_path(config: dict[str, Any]) -> Path:
    return config["_kb_root"] / config["settings"]["db_path"]


def embeddings_provider(config: dict[str, Any]) -> str:
    return config["embeddings"].get("provider", "openai")
