"""SQLite access. Metadata only — media never touches this process's disk
except as scratch, and scratch is tmpfs.

Concurrency model is `01-architecture.md`: many readers, exactly one writer.
Endpoints are defined `def` rather than `async def` so FastAPI runs them in its
threadpool, which means the writer guard is a threading.Lock rather than the
asyncio.Lock the doc names. Same discipline, correct primitive for sync code.
"""

import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("TARMAC_DB", Path(__file__).parent / "tarmac.db"))

# v0.1 has no auth. One hardcoded user, stable across restarts so the catalogue
# survives a server bounce. Deleted the moment passkeys land in v0.4.
DEV_USER_ID = "01920000-0000-7000-8000-000000000001"
DEV_USER_EMAIL = "dev@localhost"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id                TEXT PRIMARY KEY,
  email             TEXT NOT NULL UNIQUE,
  display_name      TEXT,
  invited_by        TEXT REFERENCES users(id),
  daily_byte_budget INTEGER NOT NULL DEFAULT 5368709120,
  max_concurrent    INTEGER NOT NULL DEFAULT 2,
  created_at        TEXT NOT NULL,
  disabled_at       TEXT
);

CREATE TABLE IF NOT EXISTS sources (
  source_key    TEXT PRIMARY KEY,
  extractor     TEXT NOT NULL,
  source_id     TEXT NOT NULL,
  canonical_url TEXT NOT NULL,
  title         TEXT,
  uploader      TEXT,
  duration_s    INTEGER,
  thumb_url     TEXT,
  refreshed_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS library_items (
  id             TEXT PRIMARY KEY,
  user_id        TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  source_key     TEXT NOT NULL REFERENCES sources(source_key),
  format_profile TEXT NOT NULL CHECK (json_valid(format_profile)),
  added_at       TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  deleted_at     TEXT,
  UNIQUE (user_id, source_key)
);
CREATE INDEX IF NOT EXISTS ix_items_sync ON library_items(user_id, updated_at);

CREATE TABLE IF NOT EXISTS jobs (
  id                  TEXT PRIMARY KEY,
  user_id             TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  item_id             TEXT NOT NULL REFERENCES library_items(id) ON DELETE CASCADE,
  state               TEXT NOT NULL CHECK (state IN (
                        'queued','fetching','transforming','ready',
                        'collected','failed','expired')),
  attempt             INTEGER NOT NULL DEFAULT 0,
  progress            REAL    NOT NULL DEFAULT 0,
  stage_detail        TEXT,
  error               TEXT,
  artifact_path       TEXT,
  artifact_bytes      INTEGER,
  artifact_sha256     TEXT,
  -- The pipeline emits a SET of files with fixed names (05-formats.md), so the
  -- manifest carries per-file name/bytes/sha256. See D-015.
  artifact_manifest   TEXT,
  artifact_token      TEXT,
  artifact_expires_at TEXT,
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_jobs_claim ON jobs(state, created_at);
CREATE INDEX IF NOT EXISTS ix_jobs_reap  ON jobs(artifact_expires_at)
                                         WHERE artifact_path IS NOT NULL;

CREATE TABLE IF NOT EXISTS usage_ledger (
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  day     TEXT NOT NULL,
  bytes   INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, day)
);
"""

# playlists / playlist_items / credentials / sessions are in 03-data-model.md but
# land with the phases that use them (v0.3, v0.4). Creating tables nothing reads
# is how a schema starts lying about what the app does.

_writer: sqlite3.Connection | None = None
_write_lock = threading.Lock()


def now() -> str:
    """ISO 8601 UTC, milliseconds, Z suffix. Sorts lexicographically, which is
    the property the sync cursor depends on."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def uuid7() -> str:
    """UUIDv7: 48-bit big-endian unix-ms prefix, so ids sort by creation time.

    ponytail: eight lines instead of a dependency. stdlib grows uuid.uuid7() in
    3.14 — delete this then.
    """
    b = bytearray(os.urandom(16))
    b[0:6] = int(time.time() * 1000).to_bytes(6, "big")
    b[6] = (b[6] & 0x0F) | 0x70  # version
    b[8] = (b[8] & 0x3F) | 0x80  # variant
    return str(uuid.UUID(bytes=bytes(b)))


def _configure(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous  = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")  # off by default in SQLite
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA temp_store   = MEMORY")
    return conn


@contextmanager
def reading():
    """A fresh read connection per call.

    ponytail: no pool. Opening SQLite is microseconds and this app serves a
    handful of users. Add one when a profiler says connection setup is hot.
    """
    conn = _configure(sqlite3.connect(DB_PATH))
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def writing():
    """The single writer, serialised. Every mutation goes through here.

    Yields inside a transaction: commits on clean exit, rolls back on raise.
    """
    with _write_lock:
        with _writer:  # sqlite3 connection as context manager == transaction
            yield _writer


def init() -> None:
    global _writer
    if sqlite3.sqlite_version_info < (3, 35):
        raise RuntimeError(
            f"SQLite {sqlite3.sqlite_version} is too old; RETURNING needs >= 3.35"
        )
    # isolation_level="IMMEDIATE" makes `with conn:` open BEGIN IMMEDIATE rather
    # than a deferred transaction, which is what makes the job claim in main.py
    # race-free without SKIP LOCKED.
    _writer = _configure(
        sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level="IMMEDIATE")
    )
    with _write_lock:
        _writer.executescript(SCHEMA)
        _writer.execute(
            "INSERT OR IGNORE INTO users (id, email, display_name, created_at)"
            " VALUES (?, ?, ?, ?)",
            (DEV_USER_ID, DEV_USER_EMAIL, "dev", now()),
        )
        _writer.commit()
