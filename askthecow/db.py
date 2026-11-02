"""SQLite storage: chats, messages, knowledge base (commit notes, digests, briefs) and state."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT 'New chat',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS chats_owner ON chats(owner, updated_at);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role TEXT NOT NULL,              -- 'user' or 'assistant'
    content TEXT NOT NULL,
    commit_sha TEXT,                 -- master commit the answer was produced on
    meta TEXT NOT NULL DEFAULT '{}', -- JSON: tool calls, usage, errors
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_chat ON messages(chat_id, id);

-- One row per master commit that the calibration has read.
CREATE TABLE IF NOT EXISTS commit_notes (
    sha TEXT PRIMARY KEY,
    author TEXT NOT NULL,
    date TEXT NOT NULL,
    subject TEXT NOT NULL,
    note TEXT NOT NULL,      -- plain-language explanation for hardware engineers
    files TEXT NOT NULL,     -- space separated paths
    topics TEXT NOT NULL,    -- space separated keywords (peripherals, signals, features)
    digest_id INTEGER
);

-- Summary of a group of consecutive commits.
CREATE TABLE IF NOT EXISTS digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    first_sha TEXT NOT NULL,
    last_sha TEXT NOT NULL,
    first_date TEXT NOT NULL,
    last_date TEXT NOT NULL,
    commit_count INTEGER NOT NULL,
    summary TEXT NOT NULL,
    created_at REAL NOT NULL
);

-- The agent brief. The newest row is the active one.
CREATE TABLE IF NOT EXISTS briefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    commit_sha TEXT NOT NULL,
    kind TEXT NOT NULL,      -- 'full' or 'incremental'
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS commit_notes_fts USING fts5(
    sha UNINDEXED, subject, note, files, topics, tokenize = 'porter unicode61'
);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            try:
                self._conn.executescript(FTS_SCHEMA)
                self.has_fts = True
            except sqlite3.OperationalError:
                self.has_fts = False

    # ------------------------------------------------------------------ helpers
    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def _all(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    def _one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        with self._lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
            return dict(row) if row else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -------------------------------------------------------------------- state
    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self._one("SELECT value FROM state WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        self._exec(
            "INSERT INTO state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # -------------------------------------------------------------------- chats
    def create_chat(self, owner: str, title: str = "New chat") -> dict:
        now = time.time()
        chat_id = uuid.uuid4().hex
        self._exec(
            "INSERT INTO chats(id, owner, title, created_at, updated_at) VALUES(?, ?, ?, ?, ?)",
            (chat_id, owner, title, now, now),
        )
        return self.get_chat(chat_id)  # type: ignore[return-value]

    def get_chat(self, chat_id: str) -> dict | None:
        return self._one("SELECT * FROM chats WHERE id = ?", (chat_id,))

    def list_chats(self, owner: str | None = None, limit: int = 200) -> list[dict]:
        if owner is None:
            return self._all("SELECT * FROM chats ORDER BY updated_at DESC LIMIT ?", (limit,))
        return self._all(
            "SELECT * FROM chats WHERE owner = ? ORDER BY updated_at DESC LIMIT ?", (owner, limit)
        )

    def rename_chat(self, chat_id: str, title: str) -> None:
        self._exec("UPDATE chats SET title = ?, updated_at = ? WHERE id = ?", (title, time.time(), chat_id))

    def delete_chat(self, chat_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
            self._conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))

    def add_message(
        self, chat_id: str, role: str, content: str, commit_sha: str | None = None, meta: dict | None = None
    ) -> dict:
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO messages(chat_id, role, content, commit_sha, meta, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (chat_id, role, content, commit_sha, json.dumps(meta or {}), now),
            )
            self._conn.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (now, chat_id))
            msg_id = cur.lastrowid
        return self._message(msg_id)

    def _message(self, msg_id: int) -> dict:
        row = self._one("SELECT * FROM messages WHERE id = ?", (msg_id,))
        assert row is not None
        row["meta"] = json.loads(row["meta"] or "{}")
        return row

    def list_messages(self, chat_id: str) -> list[dict]:
        rows = self._all("SELECT * FROM messages WHERE chat_id = ? ORDER BY id", (chat_id,))
        for row in rows:
            row["meta"] = json.loads(row["meta"] or "{}")
        return rows

    # ---------------------------------------------------------------- knowledge
    def known_commit_shas(self) -> set[str]:
        return {r["sha"] for r in self._all("SELECT sha FROM commit_notes")}

    def commit_note_count(self) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM commit_notes")
        return int(row["n"]) if row else 0

    def save_digest(self, digest: dict, notes: list[dict]) -> int:
        """Store one digest and the notes of its commits in one transaction."""
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                cur = self._conn.execute(
                    "INSERT INTO digests(first_sha, last_sha, first_date, last_date, commit_count, summary, created_at)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (
                        digest["first_sha"],
                        digest["last_sha"],
                        digest["first_date"],
                        digest["last_date"],
                        digest["commit_count"],
                        digest["summary"],
                        time.time(),
                    ),
                )
                digest_id = cur.lastrowid
                for n in notes:
                    self._conn.execute(
                        "INSERT OR REPLACE INTO commit_notes(sha, author, date, subject, note, files, topics, digest_id)"
                        " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                        (n["sha"], n["author"], n["date"], n["subject"], n["note"], n["files"], n["topics"], digest_id),
                    )
                    if self.has_fts:
                        self._conn.execute("DELETE FROM commit_notes_fts WHERE sha = ?", (n["sha"],))
                        self._conn.execute(
                            "INSERT INTO commit_notes_fts(sha, subject, note, files, topics) VALUES(?, ?, ?, ?, ?)",
                            (n["sha"], n["subject"], n["note"], n["files"], n["topics"]),
                        )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return int(digest_id)

    def list_digests(self, since_id: int = 0) -> list[dict]:
        return self._all("SELECT * FROM digests WHERE id > ? ORDER BY id", (since_id,))

    def last_digest_id(self) -> int:
        row = self._one("SELECT MAX(id) AS m FROM digests")
        return int(row["m"] or 0) if row else 0

    def search_commit_notes(self, query: str, limit: int = 15) -> list[dict]:
        words = re.findall(r"[A-Za-z0-9_]+", query)
        if not words:
            return []
        if self.has_fts:
            fts_query = " OR ".join(f'"{w}"' for w in words[:12])
            try:
                return self._all(
                    "SELECT n.sha, n.date, n.author, n.subject, n.note, n.files, n.topics "
                    "FROM commit_notes_fts f JOIN commit_notes n ON n.sha = f.sha "
                    "WHERE commit_notes_fts MATCH ? ORDER BY bm25(commit_notes_fts) LIMIT ?",
                    (fts_query, limit),
                )
            except sqlite3.OperationalError:
                pass
        clauses = " OR ".join(["(subject LIKE ? OR note LIKE ? OR files LIKE ? OR topics LIKE ?)"] * len(words))
        params: list[Any] = []
        for w in words:
            params += [f"%{w}%"] * 4
        return self._all(
            f"SELECT sha, date, author, subject, note, files, topics FROM commit_notes WHERE {clauses} "
            "ORDER BY date DESC LIMIT ?",
            (*params, limit),
        )

    def save_brief(self, commit_sha: str, kind: str, content: str) -> int:
        cur = self._exec(
            "INSERT INTO briefs(commit_sha, kind, content, created_at) VALUES(?, ?, ?, ?)",
            (commit_sha, kind, content, time.time()),
        )
        return int(cur.lastrowid)

    def latest_brief(self) -> dict | None:
        return self._one("SELECT * FROM briefs ORDER BY id DESC LIMIT 1")
