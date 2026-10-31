"""Hybrid search: keyword and vector rankings merged with reciprocal rank fusion."""

from __future__ import annotations

import sqlite_vec

from .index import Index, fts_query

DEFAULT_FOLDERS = ("memory", "entities")
# Already in every session's context, so a search result would only repeat it.
UNSEARCHED_FOLDER = "core"
DEFAULT_LIMIT = 8
MIN_SHARE_OF_TOP_SCORE = 0.5
RRF_K = 60
CANDIDATES = 50


def _keyword_ranking(index: Index, query: str) -> list[int]:
    match = fts_query(query)
    if not match:
        return []
    rows = index.db.execute(
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?", (match, CANDIDATES)
    )
    return [row["rowid"] for row in rows]


def _vector_ranking(index: Index, query: str) -> list[int]:
    vector = index.embedder.embed([query])[0]
    rows = index.db.execute(
        "SELECT rowid FROM chunks_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
        (sqlite_vec.serialize_float32(vector), CANDIDATES),
    )
    return [row["rowid"] for row in rows]


def _excluded(index: Index, query: str, scope: str, hidden: dict[str, str]) -> list[dict]:
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
                    {"folder": f"{folder}/", "matches": row["n"],
                     "reason": "not searched by default; tell the user, use scope='all' if they ask"}
                )
    return excluded


def search(index: Index, query: str, scope: str | None = None, exact: bool = False, limit: int = DEFAULT_LIMIT) -> dict:
    with index.lock:
        if exact:
            return _exact(index, query, scope, limit)
        scores: dict[int, float] = {}
        for ranking in (_keyword_ranking(index, query), _vector_ranking(index, query)):
            for position, rowid in enumerate(ranking):
                scores[rowid] = scores.get(rowid, 0.0) + 1.0 / (RRF_K + position + 1)
        results: dict[str, dict] = {}
        hidden: dict[str, str] = {}
        top_score = None
        for rowid in sorted(scores, key=scores.__getitem__, reverse=True):
            if top_score is not None and scores[rowid] < top_score * MIN_SHARE_OF_TOP_SCORE:
                break
            row = index.db.execute(
                "SELECT c.path, c.heading, c.text, f.folder, f.status, f.title FROM chunks c "
                "JOIN files f USING(path) WHERE c.id=?", (rowid,)
            ).fetchone()
            if row is None or row["folder"] == UNSEARCHED_FOLDER or (scope != "all" and row["folder"] not in DEFAULT_FOLDERS):
                continue
            if row["status"] != "current":
                hidden[row["path"]] = row["status"]
                continue
            top_score = top_score or scores[rowid]
            if row["path"] not in results and len(results) < limit:
                results[row["path"]] = {
                    "path": row["path"], "title": row["title"], "heading": row["heading"],
                    "snippet": row["text"][:400], "score": round(scores[rowid], 4),
                }
        return {"results": list(results.values()), "excluded": _excluded(index, query, scope, hidden)}


def _exact(index: Index, query: str, scope: str | None, limit: int) -> dict:
    needle = query.lower()
    results, hidden = [], {}
    rows = index.db.execute(
        "SELECT path, folder, status FROM files WHERE folder != ? ORDER BY path", (UNSEARCHED_FOLDER,)
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
            results.append({"path": row["path"], "snippet": text[max(0, position - 80) : position + 200]})
    excluded = [{"path": path, "reason": f"status: {status}"} for path, status in hidden.items()]
    excluded += [
        {"folder": f"{folder}/", "matches": count,
         "reason": "not searched by default; tell the user, use scope='all' if they ask"}
        for folder, count in outside.items()
    ]
    return {"results": results, "excluded": excluded}


def nearest_live_note(index: Index, text: str, folder: str, threshold: float) -> str | None:
    """The current note in `folder` closest in meaning to `text`, if it is close enough to be the same subject."""
    vector = index.embedder.embed([text])[0]
    rows = index.db.execute(
        "SELECT rowid, distance FROM chunks_vec WHERE embedding MATCH ? AND k = 20 ORDER BY distance",
        (sqlite_vec.serialize_float32(vector),),
    ).fetchall()
    for row in rows:
        if 1.0 - row["distance"] < threshold:
            return None
        match = index.db.execute(
            "SELECT f.path FROM chunks c JOIN files f USING(path) "
            "WHERE c.id=? AND f.folder=? AND f.status='current'", (row["rowid"], folder)
        ).fetchone()
        if match:
            return match["path"]
    return None
