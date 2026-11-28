"""Hybrid search: keyword and vector rankings merged with reciprocal rank fusion."""

from __future__ import annotations

import logging

import sqlite_vec

from .index import Index, fts_query
from .rerank import Reranker, rerank

log = logging.getLogger("cortex.search")

DEFAULT_FOLDERS = ("memory", "entities")
# Already in every session's context, so a search result would only repeat it.
UNSEARCHED_FOLDER = "core"
DEFAULT_LIMIT = 8
MIN_SHARE_OF_TOP_SCORE = 0.5
RRF_K = 60
CANDIDATES = 50
# A current note past its `expires` date counts as expired from that day on, without a reindex.
STATUS = "CASE WHEN f.status='current' AND e.expires < date('now','localtime') THEN 'expired' ELSE f.status END"


def _keyword_ranking(index: Index, query: str) -> list[int]:
    match = fts_query(query)
    if not match:
        return []
    rows = index.db.execute(
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?", (match, CANDIDATES)
    )
    return [row["rowid"] for row in rows]


def _embed(index: Index, text: str, kind: str) -> list[float] | None:
    """The text's vector, or None when the embedder is down; callers then do without vectors."""
    try:
        return index.embedder.embed([text], kind)[0]
    except Exception as error:  # noqa: BLE001 - an embedder problem must never fail a search
        log.warning("embedding a %s failed, searching by keyword only: %s", kind, error)
        return None


def _vector_ranking(index: Index, vector: list[float] | None) -> list[int]:
    if vector is None:
        return []
    rows = index.db.execute(
        "SELECT rowid FROM chunks_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
        (sqlite_vec.serialize_float32(vector), CANDIDATES),
    )
    return [row["rowid"] for row in rows]


def _excluded(index: Index, query: str, scope: str | None, hidden: dict[str, str]) -> list[dict]:
    excluded = [{"path": path, "reason": f"status: {status}"} for path, status in hidden.items()]
    match = fts_query(query)
    if scope != "all" and match:
        for folder in ("docs", "logs"):
            row = index.db.execute(
                "SELECT count(DISTINCT path) AS n FROM chunks_fts WHERE chunks_fts MATCH ? AND path LIKE ?",
                (match, f"{folder}/%"),
            ).fetchone()
            if row["n"]:
                excluded.append(
                    {
                        "folder": f"{folder}/",
                        "matches": row["n"],
                        "reason": "not searched by default; tell the user, use scope='all' if they ask",
                    }
                )
    return excluded


def search(
    index: Index,
    query: str,
    scope: str | None = None,
    exact: bool = False,
    limit: int = DEFAULT_LIMIT,
    reranker: Reranker | None = None,
) -> dict:
    """Fused results, reranked when a reranker is given; `reranked` says whether that happened.

    The weak-match cut (MIN_SHARE_OF_TOP_SCORE) is applied to the RRF list before reranking, so
    the reranker only reorders notes that would have qualified anyway; it never brings back one
    the cut dropped. exact=true is plain matching and is never reranked.
    """
    if exact:
        with index.lock:
            return {**_exact(index, query, scope, limit), "reranked": False}
    wanted = max(limit, reranker.candidates) if reranker else limit
    # Outside the lock, like the reranker: a slow embedder must not hold up writes and other searches.
    vector = _embed(index, query, "query")
    with index.lock:
        results, excluded = _fused(index, query, vector, scope, wanted)
    reranked = False
    if reranker and results:
        results, reranked = rerank(reranker, query, results)
    reply = {"results": results[:limit], "excluded": excluded, "reranked": reranked}
    if vector is None:
        reply["semantic"] = False
    return reply


def _fused(
    index: Index, query: str, vector: list[float] | None, scope: str | None, limit: int
) -> tuple[list[dict], list[dict]]:
    """The best `limit` notes by RRF, one (best) chunk each, plus what was left out."""
    scores: dict[int, float] = {}
    for ranking in (_keyword_ranking(index, query), _vector_ranking(index, vector)):
        for position, rowid in enumerate(ranking):
            scores[rowid] = scores.get(rowid, 0.0) + 1.0 / (RRF_K + position + 1)
    results: dict[str, dict] = {}
    hidden: dict[str, str] = {}
    top_score = None
    for rowid in sorted(scores, key=scores.__getitem__, reverse=True):
        if top_score is not None and scores[rowid] < top_score * MIN_SHARE_OF_TOP_SCORE:
            break
        row = index.db.execute(
            f"SELECT c.path, c.heading, c.text, f.folder, {STATUS} AS status, f.title FROM chunks c "
            "JOIN files f USING(path) LEFT JOIN expiry e USING(path) WHERE c.id=?",
            (rowid,),
        ).fetchone()
        if (
            row is None
            or row["folder"] == UNSEARCHED_FOLDER
            or (scope != "all" and row["folder"] not in DEFAULT_FOLDERS)
        ):
            continue
        if row["status"] != "current":
            hidden[row["path"]] = row["status"]
            continue
        top_score = top_score or scores[rowid]
        if row["path"] not in results and len(results) < limit:
            results[row["path"]] = {
                "path": row["path"],
                "title": row["title"],
                "heading": row["heading"],
                "snippet": row["text"][:400],
                "score": round(scores[rowid], 4),
                **index.neighbours(row["path"]),
            }
    return list(results.values()), _excluded(index, query, scope, hidden)


def _exact(index: Index, query: str, scope: str | None, limit: int) -> dict:
    needle = query.lower()
    results, hidden = [], {}
    rows = index.db.execute(
        f"SELECT f.path, f.folder, {STATUS} AS status FROM files f LEFT JOIN expiry e USING(path) "
        "WHERE f.folder != ? ORDER BY f.path",
        (UNSEARCHED_FOLDER,),
    ).fetchall()
    outside: dict[str, int] = {}
    for row in rows:
        text = index.vault.read_text(row["path"])
        position = text.lower().find(needle)
        if position < 0:
            continue
        if scope != "all" and row["folder"] not in DEFAULT_FOLDERS:
            outside[row["folder"]] = outside.get(row["folder"], 0) + 1
        elif row["status"] != "current":
            hidden[row["path"]] = row["status"]
        elif len(results) < limit:
            results.append(
                {
                    "path": row["path"],
                    "snippet": text[max(0, position - 80) : position + 200],
                    **index.neighbours(row["path"]),
                }
            )
    excluded = [{"path": path, "reason": f"status: {status}"} for path, status in hidden.items()]
    excluded += [
        {
            "folder": f"{folder}/",
            "matches": count,
            "reason": "not searched by default; tell the user, use scope='all' if they ask",
        }
        for folder, count in outside.items()
    ]
    return {"results": results, "excluded": excluded}


def nearest_live_note(index: Index, text: str, folder: str, threshold: float) -> str | None:
    """The current note in `folder` closest in meaning to `text`, if it is close enough to be the same subject."""
    # With the embedder down the duplicate check is skipped; the write is not refused for it.
    vector = _embed(index, text, "document")
    if vector is None:
        return None
    with index.lock:
        rows = index.db.execute(
            "SELECT rowid, distance FROM chunks_vec WHERE embedding MATCH ? AND k = 20 ORDER BY distance",
            (sqlite_vec.serialize_float32(vector),),
        ).fetchall()
        for row in rows:
            if 1.0 - row["distance"] < threshold:
                return None
            match = index.db.execute(
                "SELECT f.path FROM chunks c JOIN files f USING(path) "
                "WHERE c.id=? AND f.folder=? AND f.status='current'",
                (row["rowid"], folder),
            ).fetchone()
            if match:
                return match["path"]
    return None
