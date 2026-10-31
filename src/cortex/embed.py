"""Pluggable embedders. Anything with `name`, `dimensions` and `embed(texts)` works."""

from __future__ import annotations

import hashlib
import math
import os
from typing import Protocol

import httpx

from .config import Settings


class Embedder(Protocol):
    name: str
    dimensions: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FastEmbedder:
    """Local CPU model; no API key. The model downloads on first use."""

    def __init__(self, model: str):
        from fastembed import TextEmbedding

        self.name = f"fastembed:{model}"
        self._model = TextEmbedding(model_name=model)
        self.dimensions = len(self.embed(["dimension probe"])[0])

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self._model.embed(texts)]


class VoyageEmbedder:
    def __init__(self, model: str):
        self.name = f"voyage:{model}"
        self._model = model
        self._key = os.environ["VOYAGE_API_KEY"]
        self.dimensions = len(self.embed(["dimension probe"])[0])

    def embed(self, texts: list[str]) -> list[list[float]]:
        response = httpx.post(
            "https://api.voyageai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {self._key}"},
            json={"model": self._model, "input": texts},
            timeout=60,
        )
        response.raise_for_status()
        return [item["embedding"] for item in response.json()["data"]]


class HashEmbedder:
    """Word-hashing vectors: instant and deterministic, for tests and machines with no model."""

    def __init__(self, dimensions: int = 128):
        self.name = f"hash:{dimensions}"
        self.dimensions = dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(text) for text in texts]

    def _one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for word in text.lower().split():
            digest = hashlib.blake2b(word.strip(".,:;!?()[]").encode(), digest_size=4).digest()
            vector[int.from_bytes(digest) % self.dimensions] += 1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]


def make_embedder(settings: Settings) -> Embedder:
    provider = settings.embedding_provider
    if provider == "fastembed":
        return FastEmbedder(settings.embedding_model)
    if provider == "voyage":
        return VoyageEmbedder(settings.embedding_model)
    if provider == "hash":
        return HashEmbedder()
    raise ValueError(f"unknown embedding provider: {provider}")
