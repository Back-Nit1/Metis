"""Corpus chunk index in ``memory.sqlite``: float32 vectors + FTS5.

A separate lane from memories (spec §4): 50k imported chunks must not drown
the user's own decisions. FTS5 indexes title and uri as well as content, so a
file is findable by name even when its body never mentions the word.

Every read filters by ``user_id``; the retrieval service re-checks owner and
source version at hydration.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from array import array
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    user_id        TEXT NOT NULL,
    chunk_id       TEXT NOT NULL,
    source_id      TEXT NOT NULL,
    source_version INTEGER NOT NULL,
    connector_id   TEXT,
    origin         TEXT NOT NULL,
    title          TEXT NOT NULL,
    uri            TEXT NOT NULL,
    start_char     INTEGER NOT NULL,
    end_char       INTEGER NOT NULL,
    content        TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    model_id       TEXT,
    dimensions     INTEGER,
    vector         BLOB,
    validity       TEXT NOT NULL DEFAULT 'current',
    PRIMARY KEY (user_id, chunk_id)
);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks (user_id, source_id);
CREATE INDEX IF NOT EXISTS idx_chunks_hash ON chunks (user_id, content_hash, model_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    chunk_id UNINDEXED, user_id UNINDEXED, title, uri, content, tokenize = 'unicode61'
);
"""

#: bm25 column weights (chunk_id, user_id, title, uri, content): a name hit
#: outranks a passing mention in the body.
_BM25 = "bm25(chunks_fts, 0, 0, 4.0, 2.0, 1.0)"


def run_schema(conn: sqlite3.Connection, script: str, *, attempts: int = 20) -> None:
    """Apply a schema script, retrying briefly while another worker process
    holds the write lock: ``executescript`` (WAL switch, CREATE) can fail with
    "database is locked" even under a busy timeout when several open at once."""
    for attempt in range(attempts):
        try:
            conn.executescript(script)
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


@dataclass(frozen=True)
class ChunkRow:
    chunk_id: str
    user_id: str
    source_id: str
    source_version: int
    connector_id: str | None
    origin: str
    title: str
    uri: str
    start_char: int
    end_char: int
    content: str


def chunk_id_for(user_id: str, source_id: str, source_version: int, start: int) -> str:
    """Deterministic, so a retried ingest rewrites the same rows."""
    digest = hashlib.sha256(f"{user_id}:{source_id}:{source_version}:{start}".encode()).hexdigest()
    return f"chk_{digest[:24]}"


def fts_query(text: str, *, prefix: bool = True) -> str | None:
    """Quoted OR of terms; prefix match is the ``find -iname '*report*'`` case."""
    terms = [t for t in "".join(c if c.isalnum() else " " for c in text).split() if t]
    if not terms:
        return None
    star = "*" if prefix else ""
    return " OR ".join(f'"{t}"{star}' for t in terms[:20])


class CorpusStore:
    """Shares the memory store's connection and lock (one local file)."""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock) -> None:
        self._conn = conn
        self._lock = lock
        with self._lock:
            run_schema(self._conn, SCHEMA)

    # -- writes -------------------------------------------------------- #

    def replace_source(self, user_id: str, rows: Sequence[ChunkRow], hashes: Sequence[str]) -> None:
        """Replace every chunk of the rows' source with ``rows``. Unchanged text
        keeps its vector; new text has none yet, but keyword search works at once."""
        if not rows:
            return
        source_id = rows[0].source_id
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                kept = {
                    r["content_hash"]: (r["model_id"], r["dimensions"], r["vector"])
                    for r in self._conn.execute(
                        """SELECT content_hash, model_id, dimensions, vector FROM chunks
                           WHERE user_id=? AND source_id=? AND vector IS NOT NULL""",
                        (user_id, source_id),
                    )
                }
                self._delete_source_locked(user_id, source_id)
                for row, digest in zip(rows, hashes, strict=True):
                    if row.user_id != user_id or row.source_id != source_id:
                        raise ValueError("chunk owner or source mismatch")
                    self._conn.execute(
                        """INSERT INTO chunks (user_id, chunk_id, source_id, source_version,
                               connector_id, origin, title, uri, start_char, end_char, content,
                               content_hash, model_id, dimensions, vector)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            user_id,
                            row.chunk_id,
                            row.source_id,
                            row.source_version,
                            row.connector_id,
                            row.origin,
                            row.title,
                            row.uri,
                            row.start_char,
                            row.end_char,
                            row.content,
                            digest,
                            *kept.get(digest, (None, None, None)),
                        ),
                    )
                    self._conn.execute(
                        "INSERT INTO chunks_fts (chunk_id, user_id, title, uri, content) VALUES (?,?,?,?,?)",
                        (row.chunk_id, user_id, row.title, row.uri, row.content),
                    )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def pending_embeddings(self, user_id: str, source_id: str, model_id: str) -> list[tuple[str, str]]:
        """``(chunk_id, content_hash)`` for this source's chunks lacking a vector from ``model_id``."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT chunk_id, content_hash FROM chunks
                   WHERE user_id=? AND source_id=? AND (model_id IS NOT ? OR vector IS NULL)""",
                (user_id, source_id, model_id),
            ).fetchall()
        return [(r["chunk_id"], r["content_hash"]) for r in rows]

    def known_vectors(self, user_id: str, model_id: str, hashes: Sequence[str]) -> dict[str, bytes]:
        """Reuse: vectors already computed for identical text with the same model."""
        if not hashes:
            return {}
        marks = ",".join("?" for _ in hashes)
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT content_hash, vector FROM chunks WHERE user_id=? AND model_id=?
                    AND vector IS NOT NULL AND content_hash IN ({marks})""",
                (user_id, model_id, *hashes),
            ).fetchall()
        return {r["content_hash"]: r["vector"] for r in rows}

    def set_vectors(self, user_id: str, model_id: str, vectors: dict[str, Sequence[float] | bytes]) -> None:
        with self._lock:
            for chunk_id, vector in vectors.items():
                blob = vector if isinstance(vector, bytes) else array("f", vector).tobytes()
                self._conn.execute(
                    "UPDATE chunks SET vector=?, model_id=?, dimensions=? WHERE user_id=? AND chunk_id=?",
                    (blob, model_id, len(blob) // 4, user_id, chunk_id),
                )

    def delete_source(self, user_id: str, source_id: str) -> int:
        with self._lock:
            return self._delete_source_locked(user_id, source_id)

    def _delete_source_locked(self, user_id: str, source_id: str) -> int:
        ids = [
            r["chunk_id"]
            for r in self._conn.execute(
                "SELECT chunk_id FROM chunks WHERE user_id=? AND source_id=?", (user_id, source_id)
            )
        ]
        for chunk_id in ids:
            self._conn.execute("DELETE FROM chunks_fts WHERE user_id=? AND chunk_id=?", (user_id, chunk_id))
        self._conn.execute("DELETE FROM chunks WHERE user_id=? AND source_id=?", (user_id, source_id))
        return len(ids)

    # -- reads --------------------------------------------------------- #

    def get(self, user_id: str, chunk_ids: Sequence[str]) -> dict[str, ChunkRow]:
        if not chunk_ids:
            return {}
        marks = ",".join("?" for _ in chunk_ids)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM chunks WHERE user_id=? AND chunk_id IN ({marks})", (user_id, *chunk_ids)
            ).fetchall()
        return {r["chunk_id"]: _row(r) for r in rows if r["user_id"] == user_id}

    def keyword(self, user_id: str, text: str, *, limit: int, origins: Sequence[str] = ()) -> list[str]:
        """Chunk ids by BM25 over title, uri and content, best first."""
        query = fts_query(text)
        if query is None:
            return []
        sql = """SELECT f.chunk_id FROM chunks_fts f JOIN chunks c
                    ON c.user_id = f.user_id AND c.chunk_id = f.chunk_id
                  WHERE f.user_id = ? AND chunks_fts MATCH ? AND c.validity = 'current'"""
        params: list[Any] = [user_id, query]
        if origins:
            sql += f" AND c.origin IN ({','.join('?' for _ in origins)})"
            params.extend(origins)
        sql += f" ORDER BY {_BM25} LIMIT ?"
        params.append(limit)
        try:
            with self._lock:
                return [r["chunk_id"] for r in self._conn.execute(sql, params)]
        except sqlite3.OperationalError:
            return []

    def semantic(
        self,
        user_id: str,
        vector: Sequence[float],
        *,
        model_id: str,
        limit: int,
        origins: Sequence[str] = (),
    ) -> list[tuple[str, float]]:
        """Exact cosine over the owner's vectors from the same model.

        ponytail: linear float32 scan in Python, fine to ~10k chunks; swap for a
        sqlite-vec ``vec0`` KNN table when a corpus is large enough to measure.
        """
        sql = """SELECT chunk_id, vector FROM chunks WHERE user_id=? AND model_id=? AND dimensions=?
                 AND vector IS NOT NULL AND validity='current'"""
        params: list[Any] = [user_id, model_id, len(vector)]
        if origins:
            sql += f" AND origin IN ({','.join('?' for _ in origins)})"
            params.extend(origins)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        query = list(vector)  # normalised by the embedder, so dot == cosine
        scored = []
        for r in rows:
            stored = array("f")
            stored.frombytes(r["vector"])
            score = sum(a * b for a, b in zip(query, stored, strict=True))
            if score > 0:
                scored.append((r["chunk_id"], score))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:limit]


def _row(r: sqlite3.Row) -> ChunkRow:
    return ChunkRow(
        chunk_id=r["chunk_id"],
        user_id=r["user_id"],
        source_id=r["source_id"],
        source_version=int(r["source_version"]),
        connector_id=r["connector_id"],
        origin=r["origin"],
        title=r["title"],
        uri=r["uri"],
        start_char=int(r["start_char"]),
        end_char=int(r["end_char"]),
        content=r["content"],
    )


__all__ = ["ChunkRow", "CorpusStore", "chunk_id_for", "fts_query"]
