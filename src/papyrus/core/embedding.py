import os
from typing import Any, Protocol

import httpx
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
