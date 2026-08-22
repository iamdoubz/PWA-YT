# Data model

Two stores that mirror each other, and one that never syncs.

- **Server: SQLite.** Metadata only. Never media.
- **Client: IndexedDB.** Catalogue mirror + outbox + `local_media`.
- **Client: OPFS.** The media itself.

The organising key is `source_key` = `{extractor}:{source_id}` —
`youtube:dQw4w9WgXcQ`, `soundcloud:1234567`. The same track added via a direct
link and via a playlist dedupes to one item and one set of bytes.

---

## 1. SQLite pragmas

Apply on every connection. WAL is set once and persists in the file.

```sql
PRAGMA journal_mode = WAL;        -- readers don't block the writer
PRAGMA synchronous  = NORMAL;     -- correct with WAL; FULL is unnecessary here
PRAGMA foreign_keys = ON;         -- off by default in SQLite. Turn it on.
PRAGMA busy_timeout = 5000;       -- wait instead of failing on contention
PRAGMA temp_store   = MEMORY;
```

Requires SQLite ≥ 3.35 for `RETURNING`. Python 3.11+ bundles a new enough build;
assert it at startup rather than discovering it in production.

## 2. Schema

```sql
CREATE TABLE users (
  id                TEXT PRIMARY KEY,              -- UUIDv7
  email             TEXT NOT NULL UNIQUE,
  display_name      TEXT,
  invited_by        TEXT REFERENCES users(id),
  daily_byte_budget INTEGER NOT NULL DEFAULT 5368709120,   -- 5 GiB
  max_concurrent    INTEGER NOT NULL DEFAULT 2,
  created_at        TEXT NOT NULL,
  disabled_at       TEXT
);

CREATE TABLE credentials (                          -- WebAuthn passkeys
  id           TEXT PRIMARY KEY,                    -- credential id, base64url
  user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  public_key   BLOB NOT NULL,
  sign_count   INTEGER NOT NULL DEFAULT 0,
  transports   TEXT,
  created_at   TEXT NOT NULL,
  last_used_at TEXT
);

CREATE TABLE sessions (
  token_hash   TEXT PRIMARY KEY,                    -- SHA-256 of the bearer token
  user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  device_label TEXT,
  created_at   TEXT NOT NULL,
  expires_at   TEXT NOT NULL
);

CREATE TABLE sources (                              -- shared cache, not per-user
  source_key    TEXT PRIMARY KEY,                   -- 'youtube:dQw4w9WgXcQ'
  extractor     TEXT NOT NULL,
  source_id     TEXT NOT NULL,
  canonical_url TEXT NOT NULL,
  title         TEXT,
  uploader      TEXT,
  duration_s    INTEGER,
  thumb_url     TEXT,                               -- for re-download ONLY, never rendered
  refreshed_at  TEXT NOT NULL
);

CREATE TABLE library_items (
  id             TEXT PRIMARY KEY,
  user_id        TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  source_key     TEXT NOT NULL REFERENCES sources(source_key),
  format_profile TEXT NOT NULL CHECK (json_valid(format_profile)),
  added_at       TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  deleted_at     TEXT,
  UNIQUE (user_id, source_key)
);
CREATE INDEX ix_items_sync ON library_items(user_id, updated_at);

CREATE TABLE playlists (
  id         TEXT PRIMARY KEY,
  user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name       TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  deleted_at TEXT
);
CREATE INDEX ix_playlists_sync ON playlists(user_id, updated_at);

CREATE TABLE playlist_items (
  playlist_id TEXT NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
  item_id     TEXT NOT NULL REFERENCES library_items(id) ON DELETE CASCADE,
  position    TEXT NOT NULL,                        -- fractional index, sorts lexicographically
  updated_at  TEXT NOT NULL,
  deleted_at  TEXT,
  PRIMARY KEY (playlist_id, item_id)
);
CREATE INDEX ix_pli_order ON playlist_items(playlist_id, position);

CREATE TABLE jobs (
  id                  TEXT PRIMARY KEY,
  user_id             TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  item_id             TEXT NOT NULL REFERENCES library_items(id) ON DELETE CASCADE,
  state               TEXT NOT NULL CHECK (state IN (
                        'queued','fetching','transforming','ready',
                        'collected','failed','expired')),
  attempt             INTEGER NOT NULL DEFAULT 0,
  progress            REAL    NOT NULL DEFAULT 0,   -- 0.0 .. 1.0
  stage_detail        TEXT,
  error               TEXT,
  artifact_path       TEXT,                         -- inside tmpfs scratch
  artifact_bytes      INTEGER,
  artifact_sha256     TEXT,
  artifact_expires_at TEXT,
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL
);
CREATE INDEX ix_jobs_claim ON jobs(state, created_at);
CREATE INDEX ix_jobs_reap  ON jobs(artifact_expires_at)
                            WHERE artifact_path IS NOT NULL;

CREATE TABLE usage_ledger (
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  day     TEXT NOT NULL,                            -- 'YYYY-MM-DD' UTC
  bytes   INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, day)
);
```

All timestamps are ISO 8601 UTC strings. They sort lexicographically, which is
the property the sync cursor depends on.

## 3. Claiming a job without SKIP LOCKED

Postgres's `FOR UPDATE SKIP LOCKED` has no SQLite equivalent, but SQLite's
single-writer model makes the problem simpler rather than harder: an `IMMEDIATE`
transaction takes the write lock up front, so two workers cannot claim the same
row.

```sql
BEGIN IMMEDIATE;

UPDATE jobs
   SET state      = 'fetching',
       attempt    = attempt + 1,
       updated_at = :now
 WHERE id = (
       SELECT j.id
         FROM jobs j
         JOIN users u ON u.id = j.user_id
        WHERE j.state = 'queued'
          AND u.disabled_at IS NULL
          AND (SELECT COUNT(*) FROM jobs r
                WHERE r.user_id = j.user_id
                  AND r.state IN ('fetching','transforming')) < u.max_concurrent
        ORDER BY j.created_at
        LIMIT 1)
RETURNING *;

COMMIT;
```

The correlated subquery enforces per-user concurrency in the same statement that
claims the job, so it cannot race. Poll on an interval with jitter; do not busy-loop.

## 4. Client — IndexedDB

Database `pwa-yt`, version 1.

| Store | Key | Indexes | Contents |
|---|---|---|---|
| `items` | `id` | `by_source_key`, `by_updated_at` | mirror of `library_items` + denormalised source metadata |
| `playlists` | `id` | `by_updated_at` | mirror |
| `playlist_items` | `[playlist_id, item_id]` | `by_playlist_position` | mirror |
| `local_media` | `item_id` | `by_state` | **never syncs** |
| `artwork` | `item_id` | — | small JPEG `Blob` for list rendering |
| `outbox` | `seq` (autoIncrement) | — | queued offline mutations |
| `meta` | `key` | — | session, sync cursor, persisted flag, shell version |

```js
// local_media — the client's private answer to "do I actually have this?"
{
  item_id:      'uuid',
  state:        'absent' | 'partial' | 'present' | 'missing',
  audio_path:   '/media/{itemId}/audio.m4a',
  video_path:   null,
  art_path:     '/media/{itemId}/art.jpg',
  bytes:        7340032,
  sha256:       'a1b2…',
  downloaded_at:'2026-08-21T…Z',
  verified_at:  '2026-08-21T…Z'
}
```

`local_media` is deliberately per-device and never synced. Your laptop and your
phone hold different subsets of the same library, and that is correct.

## 5. Sync protocol

Deliberately simple. The conflict surface is "I reordered the same playlist on
two devices while offline". Last-write-wins is an acceptable answer to that.
**Do not build CRDTs for this.**

**Pull:** `GET /sync?since={cursor}` returns changed rows across `items`,
`playlists`, and `playlist_items`, including tombstones (`deleted_at` set).
Cursor is `{updated_at}|{id}` — the composite avoids skipping rows that share a
millisecond.

**Push:** the outbox replays in `seq` order. Each mutation carries an idempotency
key so a retry after a dropped response cannot double-apply.

**Resolve:** per row, higher `updated_at` wins. A tombstone always beats an
update at the same timestamp — deletes are intentional, and resurrecting a
deleted item is a worse failure than losing a rename.

**Deletes on the client:** when a tombstone arrives for an item, remove the OPFS
directory and the `local_media` row. This is the only path that deletes media
without direct user action, so log it.

## 6. Fractional indexing

`playlist_items.position` is a string key that sorts lexicographically between
its neighbours (the `fractional-indexing` algorithm — keys like `a0`, `a1`,
`a0V`). Inserting or reordering one track writes exactly one row rather than
renumbering the tail.

This matters specifically because of offline use: an integer `position` column
means a reorder on a plane produces fifty queued mutations that must all replay
cleanly hours later. A fractional index produces one.
