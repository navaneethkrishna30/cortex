"""Optional second pass over fused search results: a cross-encoder reorders the top candidates.

Off by default. Snippets leave the server when it is on, and a slow or failing reranker must
never cost a search, so every failure falls back to the RRF order.
"""

from __future__ import annotations

import logging
import os
from typing import Protocol

import httpx

from .config import Settings

log = logging.getLogger("cortex.rerank")

# Request and response shape: https://docs.voyageai.com/reference/reranker-api
# POST {query, documents, model}; the reply's `data` holds {index, relevance_score} per document,
# where `index` is the document's position in the request, sorted by descending relevance.
VOYAGE_RERANK_URL = "https://api.voyageai.com/v1/rerank"


class Reranker(Protocol):
    candidates: int

    def scores(self, query: str, documents: list[str]) -> list[float]: ...


class VoyageReranker:
    def __init__(self, model: str, key: str, timeout_s: float, candidates: int = 30, client=None):
        self.model = model
        self.timeout_s = timeout_s
        self.candidates = candidates
        self._key = key
        self._client = client or httpx.Client()

    def __repr__(self) -> str:
        return f"VoyageReranker(model={self.model!r}, timeout_s={self.timeout_s}, candidates={self.candidates})"

    def scores(self, query: str, documents: list[str]) -> list[float]:
        """One relevance score per document, in the order given. Raises on anything unexpected."""
        response = self._client.post(
            VOYAGE_RERANK_URL,
            headers={"Authorization": f"Bearer {self._key}"},
            json={"model": self.model, "query": query, "documents": documents},
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        found: dict[int, float] = {}
        for item in response.json()["data"]:
            index, score = item["index"], item["relevance_score"]
            if type(index) is not int or not 0 <= index < len(documents) or index in found:
                raise ValueError(f"bad index in rerank reply: {index!r}")
            found[index] = float(score)
        if len(found) != len(documents):
            raise ValueError(f"rerank reply scored {len(found)} of {len(documents)} documents")
        return [found[i] for i in range(len(documents))]


def make_reranker(settings: Settings, client=None) -> Reranker | None:
    """None means search keeps the RRF order. A missing key turns reranking off rather than
    stopping the server, since search works without it."""
    provider = settings.rerank_provider
    if provider == "none":
        return None
    if provider == "voyage":
        key = os.environ.get("VOYAGE_API_KEY")
        if not key:
            log.warning("rerank_provider is voyage but VOYAGE_API_KEY is not set: reranking is off")
            return None
        return VoyageReranker(
            settings.rerank_model, key, settings.rerank_timeout_s, settings.rerank_candidates, client=client
        )
    raise ValueError(f"unknown rerank provider: {provider}")


def rerank(reranker: Reranker, query: str, results: list[dict]) -> tuple[list[dict], bool]:
    """Results reordered by relevance, and whether that worked. On any failure, the input order.

    Each note is sent once, as its title and best snippet.
    """
    documents = [f"{r['title']}\n{r['snippet']}" if r.get("title") else r["snippet"] for r in results]
    try:
        scores = reranker.scores(query, documents)
    except Exception as error:  # noqa: BLE001 - a reranker problem must never fail a search
        status = getattr(getattr(error, "response", None), "status_code", None)
        log.warning("rerank failed (%s%s): keeping RRF order", type(error).__name__, f" {status}" if status else "")
        return results, False
    ranked = sorted(zip(scores, range(len(results))), key=lambda pair: (-pair[0], pair[1]))
    return [{**results[i], "rerank_score": round(score, 4)} for score, i in ranked], True
