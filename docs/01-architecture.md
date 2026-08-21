# Architecture

## Components

| Component | Technology | Role |
|---|---|---|
| PWA | Svelte 5 + Vite + `vite-plugin-pwa` | The app. Owns all durable media. |
| Download worker (client) | Dedicated Web Worker | OPFS writes via `createSyncAccessHandle()` |
| API + job runner | FastAPI (Python 3.11+), single process | Import yt-dlp as a library, not a subprocess |
| Database | **SQLite** (WAL) | Users, catalogue, playlists, jobs. Metadata only. |
| Scratch | tmpfs volume, size-capped | Per-job working directory. Deliberately not S3. |
| POT provider | `bgutil-ytdlp-pot-provider` (Node) | Mints YouTube proof-of-origin tokens |

Two containers, one volume for the SQLite file, one tmpfs for scratch. That is
the whole deployment.

## Why yt-dlp as a library, not a subprocess

`yt_dlp.YoutubeDL` gives you structured metadata from `extract_info()` and a
`progress_hooks` callback. Shelling out means parsing progress off stdout, which
is exactly the fragile thing you do not want between you and a good UX. Run it
inside a `ProcessPoolExecutor` so a hung extractor cannot take down the event
loop, and so you can kill a job cleanly.

## The pipeline

| # | Stage | Runs on | Notes |
|---|---|---|---|
| 1 | Submit | client | `POST /resolve` with URL + format profile |
| 2 | Resolve | server | `extract_info(download=False)`. Returns a **plan**, not a job. |
| 3 | Confirm | client | User deselects entries. `POST /items` commits. |
| 4 | Fetch | server | yt-dlp writes into `/scratch/{job_id}/` |
| 5 | Transform | server | ffmpeg: encode, tag, embed art, optional video mux |
| 6 | Pull & commit | client | Stream into OPFS → verify → `DELETE /jobs/{id}/artifact` |
| 7 | Reap | server | TTL sweep removes anything uncollected |

### Stage 2 is not optional

Resolving before enqueueing is the difference between a usable playlist import
and an act of faith. The plan carries per-entry title, duration, thumbnail, and
an estimated output size (duration × target bitrate). The user sees a total
before committing to a 400-track download that will not fit on their phone.

Stream plan entries to the client as they resolve — a 400-entry playlist takes
minutes to enumerate and a spinner for that long reads as a hang.

### Stage 6 is the contract

```
fetch(artifactUrl)                     // ranged, resumable
  → ReadableStream reader
  → worker writes chunks to /media/{itemId}/audio.m4a.part
  → verify byte length and SHA-256 against job metadata
  → rename .part → audio.m4a
  → write IndexedDB local_media row (state: 'present')
  → DELETE /jobs/{id}/artifact
```

Any failure before the rename leaves a `.part` file and a job that can be
retried. Any failure after the `DELETE` is still recoverable, because the item
row carries `source_key` and `format_profile` — the job can simply be re-run.
**At no point is the server the only holder of anything the user needs.**

## Concurrency model (this is where SQLite constrains you)

SQLite allows many concurrent readers and exactly one writer. Design around it
rather than fighting it:

- `PRAGMA journal_mode=WAL` — readers do not block the writer, writer does not
  block readers. Non-negotiable.
- `PRAGMA busy_timeout=5000` — turns most contention into a short wait rather
  than an immediate `SQLITE_BUSY`.
- **One writer connection**, guarded by an `asyncio.Lock`. Every mutation goes
  through it. Reads use a separate read-only connection pool.
- Downloads and transcodes run in a `ProcessPoolExecutor` and must **not** hold
  a DB connection while running. They report progress by posting messages back
  to the main process, which writes.
- Job claim uses an `IMMEDIATE` transaction with `RETURNING` instead of
  Postgres's `SKIP LOCKED`. See `03-data-model.md`.

At the scale this app operates at — a handful of users, two or three concurrent
downloads — SQLite is not a compromise. It is the correct choice, and it removes
Redis from the stack entirely.

### Progress transport

Server-sent events (`GET /jobs/stream`), one connection for all in-flight jobs.
Not WebSockets: progress is one-directional, SSE reconnects on its own, and it
survives mobile network transitions better than a socket you have to babysit.

Debounce progress writes. Do not write a DB row for every ffmpeg progress line —
write at most once per second per job, and push the fine-grained values straight
to the SSE stream from memory.

## Backup and portability

The entire server state is one SQLite file. `VACUUM INTO '/backup/tarmac-{date}.db'`
nightly is a complete backup. This reinforces the central thesis: if the server
is destroyed, you lose the catalogue and job history, not a single second of
anyone's music, because the music was never there.

## What deliberately does not exist

- No object storage. Persistent blob storage is how this design rots into a
  server-side media library.
- No Redis. SQLite is the queue.
- No CDN. See `02-offline-playback.md` failure mode #1.
- No server-side transcoding cache. Re-downloads re-run the pipeline. That is
  cheap and it keeps stage 7 honest.
