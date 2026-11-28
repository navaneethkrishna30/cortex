"""Pluggable embedders. Anything with `name`, `dimensions` and `embed(texts, kind, patient)` works.

`kind` is "document" for note chunks and "query" for search text; models that tell the two apart
(Voyage) embed them differently, the others ignore it. An embedder may raise: the index keeps the
note searchable by keyword and fills its vectors in later.

`patient` is for the background backfill: a long timeout and a retry. Calls made while a write or
a search waits are not patient: a few seconds, then the caller does without vectors.
"""

from __future__ import annotations

import hashlib
import math
import os
import time
from typing import Protocol

import httpx

from .config import Settings


class Embedder(Protocol):
    name: str
    dimensions: int

    def embed(self, texts: list[str], kind: str = "document", patient: bool = False) -> list[list[float]]: ...


class FastEmbedder:
    """Local CPU model; no API key. Needs the `local` extra; the model downloads on first use."""

    def __init__(self, model: str):
        from fastembed import TextEmbedding  # pyright: ignore[reportMissingImports] - the optional `local` extra

        self.name = f"fastembed:{model}"
        self._model = TextEmbedding(model_name=model)
        self.dimensions = len(self.embed(["dimension probe"])[0])

    def embed(self, texts: list[str], kind: str = "document", patient: bool = False) -> list[list[float]]:
        return [vector.tolist() for vector in self._model.embed(texts)]


VOYAGE_EMBED_URL = "https://api.voyageai.com/v1/embeddings"


class VoyageEmbedder:
    """Voyage's API. Dimensions come from config, so starting up needs no network."""

    BATCH = 128

    def __init__(
        self,
        model: str,
        dimensions: int,
        client: httpx.Client | None = None,
        quick_timeout: float = 5.0,
        patient_timeout: float = 30.0,
    ):
        self.name = f"voyage:{model}"
        self.dimensions = dimensions
        self._model = model
        self._client = client or httpx.Client()
        self._timeouts = {False: quick_timeout, True: patient_timeout}
        self._key = os.environ.get("VOYAGE_API_KEY", "")

    def embed(self, texts: list[str], kind: str = "document", patient: bool = False) -> list[list[float]]:
        if not self._key:
            raise RuntimeError("VOYAGE_API_KEY is not set")
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.BATCH):
            vectors += self._request(texts[start : start + self.BATCH], kind, patient)
        return vectors

    def _request(self, texts: list[str], kind: str, patient: bool) -> list[list[float]]:
        body = {"model": self._model, "input": texts, "input_type": kind, "output_dimension": self.dimensions}
        for attempt in range(2):
            response = self._client.post(
                VOYAGE_EMBED_URL,
                headers={"Authorization": f"Bearer {self._key}"},
                json=body,
                timeout=self._timeouts[patient],
            )
            # A patient call retries a rate limit or a server hiccup once; anything else is the caller's.
            if patient and response.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                time.sleep(1.0)
                continue
            response.raise_for_status()
            data = sorted(response.json()["data"], key=lambda item: item["index"])
            return [item["embedding"] for item in data]
        raise AssertionError("unreachable")


class HashEmbedder:
    """Word-hashing vectors: instant and deterministic, for tests and machines with no model."""

    def __init__(self, dimensions: int = 128):
        self.name = f"hash:{dimensions}"
        self.dimensions = dimensions

    def embed(self, texts: list[str], kind: str = "document", patient: bool = False) -> list[list[float]]:
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
    if provider == "voyage":
        return VoyageEmbedder(settings.embedding_model, settings.embedding_dimensions)
    if provider == "fastembed":
        return FastEmbedder(settings.embedding_model)
    if provider == "hash":
        return HashEmbedder()
    raise ValueError(f"unknown embedding provider: {provider}")
