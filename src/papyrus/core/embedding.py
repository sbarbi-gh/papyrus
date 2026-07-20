import json
import os
from pathlib import Path
from typing import Any, Protocol

import httpx
import numpy as np
from openai import OpenAI


class EmbeddingProvider(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIProvider:
    def __init__(self, model: str, dimensions: int | None = None):
        self.model = model
        self.dimensions = dimensions
        self._client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

    def embed(self, texts: list[str]) -> list[list[float]]:
        kwargs: dict[str, Any] = {"model": self.model, "input": texts}
        if self.dimensions:
            kwargs["dimensions"] = self.dimensions
        resp = self._client.embeddings.create(**kwargs)
        return [d.embedding for d in resp.data]


class OllamaProvider:
    def __init__(self, model: str, base_url: str = "http://localhost:11434"):
        self.model = model
        self._url = f"{base_url}/api/embed"

    def embed(self, texts: list[str]) -> list[list[float]]:
        embeddings: list[list[float]] = []
        for text in texts:
            resp = httpx.post(
                self._url,
                json={"model": self.model, "input": text},
                timeout=60.0,
            )
            resp.raise_for_status()
            data = resp.json()
            embeddings.append(data.get("embeddings", [[]])[0])
        return embeddings


def get_provider(config: dict[str, Any]) -> EmbeddingProvider:
    emb = config.get("embeddings", {})
    provider_name = emb.get("provider", "openai")
    model = emb.get("model", "text-embedding-3-small")
    dimensions = emb.get("dimensions", None)

    if provider_name == "ollama":
        base_url = emb.get("base_url", "http://localhost:11434")
        return OllamaProvider(model=model, base_url=base_url)
    return OpenAIProvider(model=model, dimensions=dimensions)


def _is_quality_text(text: str) -> bool:
    if len(text.strip()) < 30:
        return False
    alpha = sum(1 for c in text if c.isalpha())
    total = len(text)
    if total > 0 and alpha / total < 0.4:
        return False
    if text.count("<br>") > 5:
        return False
    return True


def load_embeddings_from_tree(db_root: Path, concepts_root: Path) -> tuple[np.ndarray, list[dict[str, Any]]]:
    vectors: list[list[float]] = []
    meta: list[dict[str, Any]] = []

    for chunks_file in sorted(db_root.glob("*/chunks.jsonl")):
        pmid = chunks_file.parent.name
        with open(chunks_file) as f:
            for line in f:
                chunk = json.loads(line)
                emb = chunk.get("embedding", [])
                text = chunk.get("text", "")
                if emb and _is_quality_text(text):
                    vectors.append(emb)
                    meta.append({
                        "pmid": pmid,
                        "kind": "chunk",
                        "chunk_id": chunk.get("chunk_id", ""),
                        "text": chunk.get("text", ""),
                    })

    for dist_file in sorted(db_root.glob("*/distillation.json")):
        pmid = dist_file.parent.name
        with open(dist_file) as f:
            dist = json.load(f)
            emb = dist.get("embedding", [])
            if emb:
                vectors.append(emb)
                meta.append({
                    "pmid": pmid,
                    "kind": "distillation",
                    "text": dist.get("text", ""),
                })

    for concept_file in sorted(concepts_root.glob("*.json")):
        if concept_file.name == "index.json":
            continue
        concept_id = concept_file.stem
        with open(concept_file) as f:
            concept = json.load(f)
            emb = concept.get("embedding", [])
            if emb:
                vectors.append(emb)
                meta.append({
                    "concept_id": concept_id,
                    "kind": "concept",
                    "text": concept.get("text", ""),
                })

    if not vectors:
        return np.array([]), []
    return np.array(vectors, dtype=np.float32), meta
