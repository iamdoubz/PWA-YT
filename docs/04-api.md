# API surface

All endpoints require a bearer token except `/auth/*`, `/health`, and
`/health/extractors` (the same liveness/monitoring concern as `/health`, and
it carries no user-specific data).
All responses are JSON except the artifact download.

## Auth

Registration and login are both usernameless (resident/discoverable
credentials) two-step ceremonies. `begin` returns a `ceremony_id` alongside
the WebAuthn options; `finish` takes that same `ceremony_id` back with the
browser's response. A ceremony id is single-use and expires after 5 minutes.
See `08-decisions.md` D-019.

| Method | Path | Notes |
|---|---|---|
| `POST` | `/auth/register/begin` | `{invite_code, email, display_name?}` → `{ceremony_id, options}`. 422 if the invite code is unknown or already used. |
| `POST` | `/auth/register/finish` | `{ceremony_id, credential}` → `{token, expires_at, user}`. Creates the user + credential and consumes the invite in one transaction. |
| `POST` | `/auth/login/begin` | No body — usernameless. → `{ceremony_id, options}` with an empty `allowCredentials`, so the authenticator's own picker shows every passkey for this RP. |
| `POST` | `/auth/login/finish` | `{ceremony_id, credential}` → `{token, expires_at, user}`. |
| `POST` | `/auth/magic-link` | **Deferred** — not built in the auth foundation pass. Fallback and recovery path, to be rate-limited hard when it lands. |
| `POST` | `/auth/logout` | Invalidates the session server-side only. |

Invite codes are minted by `server/scripts/create_invite.py`, an operator
action — there is deliberately no endpoint for it.

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

`cursor` is opaque to the client — a base64url JSON blob carrying one
`(updated_at, id)` position per table, so a row that shares a millisecond
with the cursor's own row is never skipped (row-value comparison). Pass `''`
(or omit `since`) for a first-ever sync, which returns full history.

**`POST /sync/outbox` was not built.** The original design called for a
dedicated batch-replay endpoint with per-mutation idempotency keys. It turned
out to be unnecessary: every mutating endpoint above is already safe to
replay as-is — creates take a client-generated id (`ON CONFLICT DO NOTHING`),
renames/deletes are last-write-wins, and the playlist-items patch is
`ON CONFLICT DO UPDATE`. The client's outbox (`app/src/outbox.js`) just
re-issues the original REST call. See D-018 and D-020.

## Account

| Method | Path | Notes |
|---|---|---|
| `GET` | `/me` | Profile, daily byte budget, concurrency limit. |
| `GET` | `/me/usage` | `{bytes_used_today, daily_byte_budget, remaining_bytes, active_jobs}`. |
| `PUT` | `/me/cookies` | `{cookies}` — Netscape cookie-file text. Encrypted at rest (Fernet); never returned once saved. |
| `GET` | `/me/cookies` | `{configured, updated_at}` only — write-only from the client's perspective. |
| `DELETE` | `/me/cookies` | Clears the jar. |

**`PUT /me/settings` was not built.** There is no per-user setting yet worth
a dedicated endpoint — client-transcode preference is a v1.0 concept, and
default format profile is still just a client-local default. Add this when
a real setting needs to live server-side.

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
