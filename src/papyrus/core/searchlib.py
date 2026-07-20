import json
from pathlib import Path
from typing import Any

import numpy as np


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


def cosine_scores(query_vec: np.ndarray, vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1)
    q_norm = np.linalg.norm(query_vec)
    return np.dot(vectors, query_vec) / (norms * q_norm + 1e-8)


def cascade_search(
    query_vec: np.ndarray,
    concepts_vectors: np.ndarray,
    concepts_meta: list[dict],
    distillations_vectors: np.ndarray,
    distillations_meta: list[dict],
    chunks_vectors: np.ndarray,
    chunks_meta: list[dict],
    top_k: int = 10,
    threshold: float = 0.5,
) -> tuple[np.ndarray, np.ndarray]:
    if len(concepts_vectors) > 0:
        scores = cosine_scores(query_vec, concepts_vectors)
        best = float(scores.max()) if len(scores) > 0 else 0.0
        if best >= threshold:
            top_idx = np.argsort(scores)[-top_k:][::-1]
            return scores[top_idx], top_idx

    if len(distillations_vectors) > 0:
        scores = cosine_scores(query_vec, distillations_vectors)
        best = float(scores.max()) if len(scores) > 0 else 0.0
        if best >= threshold:
            top_idx = np.argsort(scores)[-top_k:][::-1]
            return scores[top_idx], top_idx

    if len(chunks_vectors) > 0:
        scores = cosine_scores(query_vec, chunks_vectors)
        top_idx = np.argsort(scores)[-top_k:][::-1]
        return scores[top_idx], top_idx

    return np.array([]), np.array([], dtype=np.int64)


def split_by_kind(vectors: np.ndarray, meta: list[dict]) -> dict[str, tuple[np.ndarray, list[dict]]]:
    result: dict[str, tuple[list, list]] = {
        "concept": ([], []),
        "distillation": ([], []),
        "chunk": ([], []),
    }
    for i, m in enumerate(meta):
        kind = m.get("kind", "chunk")
        if kind not in result:
            kind = "chunk"
        result[kind][0].append(vectors[i])
        result[kind][1].append(m)
    return {
        k: (np.array(vs, dtype=np.float32) if vs else np.array([]), ms)
        for k, (vs, ms) in result.items()
    }
