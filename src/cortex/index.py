"""The search index: a rebuildable SQLite cache of the notes (FTS5, vectors, edges)."""

from __future__ import annotations

import logging
import re
import sqlite3
import threading

import sqlite_vec

from .embed import Embedder
from .notes import body_links, chunks, parse, relation_edges, sha256
from .vault import ARCHIVE, Vault

log = logging.getLogger("cortex.index")

# Bump whenever what the index holds changes, so existing indexes rebuild themselves on startup.
# 2: bare relation values became edges.
SCHEMA_VERSION = "2"
DATA_TABLES = ("files", "chunks", "chunks_fts", "edges", "expiry")

STOPWORDS = set("a an and are as at be by for from how i in is it my of on or that the to was what when where which who why with".split())

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files(
    path TEXT PRIMARY KEY, sha TEXT NOT NULL, folder TEXT NOT NULL,
    status TEXT NOT NULL, title TEXT NOT NULL, mtime REAL NOT NULL);
CREATE TABLE IF NOT EXISTS chunks(
    id INTEGER PRIMARY KEY, path TEXT NOT NULL, heading TEXT NOT NULL, text TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, heading, path UNINDEXED);
CREATE TABLE IF NOT EXISTS edges(
    src TEXT NOT NULL, relation TEXT NOT NULL, target TEXT NOT NULL, dst TEXT);
CREATE INDEX IF NOT EXISTS edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS edges_dst ON edges(dst);
-- Kept apart from files so a note's expiry is checked at query time, not only when indexed.
CREATE TABLE IF NOT EXISTS expiry(path TEXT PRIMARY KEY, expires TEXT NOT NULL);
"""


class IndexMismatch(RuntimeError):
    pass


def fts_query(text: str) -> str:
    words = [w for w in re.findall(r"\w+", text.lower()) if w not in STOPWORDS]
    return " OR ".join(f'"{word}"' for word in words)


class Index:
    def __init__(self, vault: Vault, embedder: Embedder):
        self.vault = vault
        self.embedder = embedder
        self.lock = threading.RLock()
        vault.settings.state_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(vault.settings.index_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.enable_load_extension(True)
        sqlite_vec.load(self.db)
        self.db.enable_load_extension(False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._ensure_vectors()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """An index from older code is rebuilt, since it can't be patched up row by row.

        A fresh index has nothing to rebuild; an old one may predate the version key altogether.
        """
        stored = self._meta("schema_version")
        if stored != SCHEMA_VERSION:
            if self.db.execute("SELECT 1 FROM files LIMIT 1").fetchone():
                log.info("index schema %s, code expects %s: rebuilding", stored or "unversioned", SCHEMA_VERSION)
                # Recreated rather than emptied, so a changed table definition takes effect.
                for table in DATA_TABLES:
                    self.db.execute(f"DROP TABLE IF EXISTS {table}")
                self.db.executescript(SCHEMA)
                self.rebuild()
            self.db.execute("INSERT OR REPLACE INTO meta VALUES('schema_version', ?)", (SCHEMA_VERSION,))
            self.db.commit()

    def _meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def _ensure_vectors(self) -> None:
        model, dims = self._meta("embedding_model"), self._meta("embedding_dimensions")
        if model is None:
            self._create_vectors()
        elif model != self.embedder.name or int(dims) != self.embedder.dimensions:
            raise IndexMismatch(
                f"index was built with {model} ({dims}d), config says "
                f"{self.embedder.name} ({self.embedder.dimensions}d). Run: cortex index --rebuild"
            )

    def _create_vectors(self) -> None:
        self.db.execute("DROP TABLE IF EXISTS chunks_vec")
        self.db.execute(
            f"CREATE VIRTUAL TABLE chunks_vec USING vec0(embedding float[{self.embedder.dimensions}] distance_metric=cosine)"
        )
        self.db.executemany(
            "INSERT OR REPLACE INTO meta VALUES(?,?)",
            [("embedding_model", self.embedder.name), ("embedding_dimensions", str(self.embedder.dimensions))],
        )
        self.db.commit()

    def rebuild(self) -> int:
        with self.lock:
            for table in DATA_TABLES:
                self.db.execute(f"DELETE FROM {table}")
            self._create_vectors()
            return self.sync()

    def sync(self) -> int:
        """Bring the index in line with the folder; only changed files are re-embedded."""
        with self.lock:
            paths = self.vault.all_paths()
            known = {r["path"]: r["sha"] for r in self.db.execute("SELECT path, sha FROM files")}
            changed = 0
            for path in paths:
                if known.get(path) != self.vault.hash(path):
                    self.index_file(path, relink=False)
                    changed += 1
            for path in set(known) - set(paths):
                self.remove_file(path, relink=False)
                changed += 1
            if changed:
                self.relink()
            return changed

    def index_file(self, path: str, relink: bool = True) -> None:
        # Archived notes open by path but are never searched.
        if path.split("/")[0] == ARCHIVE:
            return
        with self.lock:
            text = self.vault.read_text(path)
            note = parse(text)
            self._delete_rows(path)
            self.db.execute(
                "INSERT INTO files VALUES(?,?,?,?,?,?)",
                (path, sha256(text), path.split("/")[0], note.status, note.title,
                 (self.vault.root / path).stat().st_mtime),
            )
            if note.expires:
                self.db.execute("INSERT INTO expiry VALUES(?,?)", (path, note.expires))
            pieces = chunks(note)
            if pieces:
                # The path joins the embedded text so a slug helps find its one-line note.
                vectors = self.embedder.embed([f"{path}\n{heading}\n{body}" for heading, body in pieces])
                for (heading, body), vector in zip(pieces, vectors):
                    row = self.db.execute(
                        "INSERT INTO chunks(path, heading, text) VALUES(?,?,?)", (path, heading, body)
                    ).lastrowid
                    self.db.execute(
                        "INSERT INTO chunks_fts(rowid, text, heading, path) VALUES(?,?,?,?)",
                        (row, body, heading, path),
                    )
                    self.db.execute(
                        "INSERT INTO chunks_vec(rowid, embedding) VALUES(?,?)",
                        (row, sqlite_vec.serialize_float32(vector)),
                    )
            self.db.commit()
            if relink:
                self.relink()

    def remove_file(self, path: str, relink: bool = True) -> None:
        with self.lock:
            self._delete_rows(path)
            self.db.commit()
            if relink:
                self.relink()

    def _delete_rows(self, path: str) -> None:
        for row in self.db.execute("SELECT id FROM chunks WHERE path=?", (path,)).fetchall():
            self.db.execute("DELETE FROM chunks_fts WHERE rowid=?", (row["id"],))
            self.db.execute("DELETE FROM chunks_vec WHERE rowid=?", (row["id"],))
        self.db.execute("DELETE FROM chunks WHERE path=?", (path,))
        self.db.execute("DELETE FROM files WHERE path=?", (path,))
        self.db.execute("DELETE FROM expiry WHERE path=?", (path,))

    def relink(self) -> None:
        """Recompute every edge. A vault is small, and targets move when any note appears or goes."""
        with self.lock:
            paths = self.vault.all_paths()
            relations = self.vault.relation_types()
            self.db.execute("DELETE FROM edges")
            for path in paths:
                note = self.vault.load(path)
                edges = relation_edges(note, relations) + [("links_to", target) for target in body_links(note)]
                for relation, target in dict.fromkeys(edges):
                    self.db.execute(
                        "INSERT INTO edges VALUES(?,?,?,?)",
                        (path, relation, target, self.vault.resolve_link(target, paths)),
                    )
            self.db.commit()

    def links(self, path: str) -> list[dict]:
        rows = self.db.execute("SELECT relation, target, dst FROM edges WHERE src=?", (path,))
        return [dict(row) for row in rows]

    def backlinks(self, path: str) -> list[dict]:
        rows = self.db.execute("SELECT src, relation FROM edges WHERE dst=? AND src != ?", (path, path))
        return [dict(row) for row in rows]

    def neighbours(self, path: str) -> dict[str, list[str]]:
        """Paths this note links to and is linked from, so a search hit can be followed without another search."""
        links = self.db.execute(
            "SELECT DISTINCT dst FROM edges WHERE src=? AND dst IS NOT NULL AND dst != src ORDER BY dst", (path,)
        )
        backlinks = self.db.execute(
            "SELECT DISTINCT src FROM edges WHERE dst=? AND src != dst ORDER BY src", (path,)
        )
        return {"links": [row["dst"] for row in links], "backlinks": [row["src"] for row in backlinks]}

    def broken_links(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT src, target FROM edges WHERE dst IS NULL AND src NOT LIKE 'logs/%' ORDER BY src"
        )
        return [dict(row) for row in rows]
