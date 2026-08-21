# API surface

All endpoints require a bearer token except `/auth/*` and `/health`.
All responses are JSON except the artifact download.

## Auth

| Method | Path | Notes |
|---|---|---|
| `POST` | `/auth/register/begin` | Requires a valid invite code. Returns WebAuthn creation options. |
| `POST` | `/auth/register/finish` | Verifies attestation, creates user + credential. |
| `POST` | `/auth/login/begin` | Returns WebAuthn request options. |
| `POST` | `/auth/login/finish` | Verifies assertion, returns `{token, expires_at, user}`. |
| `POST` | `/auth/magic-link` | Fallback and recovery path. Rate-limited hard. |
| `POST` | `/auth/logout` | Invalidates the session server-side only. |

**Client rule:** logout clears the token from `meta`. It does **not** clear
`items`, `local_media`, `artwork`, or OPFS. See `02-offline-playback.md` FM-2.

## Library

| Method | Path | Notes |
|---|---|---|
| `POST` | `/resolve` | `{url, format_profile}` → a **plan**. No download, no job. Streams NDJSON for playlists so entries appear as they resolve. |
| `POST` | `/items` | `{entries: [{source_key, format_profile}], playlist_name?}` → creates items and enqueues jobs. Idempotent on `(user_id, source_key)`. |
| `GET` | `/items` | Full list. Prefer `/sync` for incremental. |
| `DELETE` | `/items/{id}` | Soft delete, sets `deleted_at`. Client removes local blobs on next sync. |
| `POST` | `/items/{id}/redownload` | Re-runs the pipeline from stored `source_key` + `format_profile`. The recovery path for FM-6. |

### `/resolve` response (single item)

```json
{
  "kind": "item",
  "entry": {
    "source_key": "youtube:dQw4w9WgXcQ",
    "title": "…", "uploader": "…", "duration_s": 213,
    "thumb_url": "https://…",
    "estimated_bytes": 5112000,
    "already_in_library": false
  }
}
```

### `/resolve` response (playlist, NDJSON stream)

```
{"kind":"playlist_head","title":"…","entry_count":412}
{"kind":"entry","index":0,"source_key":"…","title":"…","duration_s":213,"estimated_bytes":5112000,"already_in_library":true}
{"kind":"entry","index":1, …}
…
{"kind":"playlist_done","total_estimated_bytes":2105344000}
```

`estimated_bytes` is `duration_s × target_bitrate ÷ 8`. It is an estimate and
must be labelled as one in the UI, but the running total is what stops a user
committing 2 GB to a phone with 900 MB free.

## Jobs

| Method | Path | Notes |
|---|---|---|
| `GET` | `/jobs` | Current jobs for this user. |
| `GET` | `/jobs/stream` | **SSE.** One connection for all in-flight jobs. Events: `progress`, `ready`, `failed`. |
| `GET` | `/jobs/{id}/artifact` | Signed, single-use, short TTL. Supports `Range` for resume. Response headers carry `X-Artifact-SHA256` and `Content-Length`. |
| `DELETE` | `/jobs/{id}/artifact` | **The collection acknowledgement.** Server deletes scratch immediately and sets `state='collected'`. |
| `POST` | `/jobs/{id}/retry` | Requeue a failed job. |

The `DELETE` is not optional cleanup — it is the contract that keeps the server
stateless. A client that downloads and never acknowledges leaves the artifact to
the TTL reaper, which is a correctness backstop, not the happy path.

## Playlists

| Method | Path | Notes |
|---|---|---|
| `GET` | `/playlists` | With ordered item ids. |
| `POST` | `/playlists` | Create. |
| `PATCH` | `/playlists/{id}` | Rename. |
| `DELETE` | `/playlists/{id}` | Soft delete. |
| `PUT` | `/playlists/{id}/items` | Full ordered replacement, or a fractional-index patch set. |

## Sync

| Method | Path | Notes |
|---|---|---|
| `GET` | `/sync?since={cursor}` | Changed rows + tombstones across items, playlists, playlist_items. Returns a new cursor. |
| `POST` | `/sync/outbox` | Replays queued offline mutations. Each carries an idempotency key. |

## Account

| Method | Path | Notes |
|---|---|---|
| `GET` | `/me` | Profile, budgets, concurrency limit. |
| `GET` | `/me/usage` | Bytes used today, remaining budget, active job count. |
| `PUT` | `/me/settings` | Default format profile, client-transcode preference. |
| `PUT` | `/me/cookies` | Optional per-user cookie jar for private content. Encrypted at rest, scoped strictly to this user's jobs. |

## Health

| Method | Path | Notes |
|---|---|---|
| `GET` | `/health` | Liveness. |
| `GET` | `/health/extractors` | Result of the nightly canary — one known-good URL per source. This is your early warning for extractor breakage. |

## Error shape

```json
{ "error": "quota_exceeded",
  "message": "This download would exceed today's 5 GB limit.",
  "retry_after": "2026-08-22T00:00:00Z" }
```

Messages are written for the user, not the log. State what happened and what to
do about it.
