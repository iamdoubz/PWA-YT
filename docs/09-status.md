# Build status

**As of:** 2026-08-22 · commit `3156c5f` + uncommitted v0.3 work (playlists, outbox)
**Name:** the project was briefly codenamed *Tarmac*; it is **PWA-YT** everywhere now
**Phases claimed complete:** v0.0 (added), v0.1, v0.2. v0.3 is built and smoke-tested
in a desktop browser (server killed/restarted to simulate reconnect) but **not**
run through the real offline test protocol on a device — same risk position as
v0.1/v0.2, see §1.
**Verified on:** desktop Chrome only

Read this with `06-build-plan.md` open. This file says what is *actually* true;
the build plan says what was *supposed* to happen.

---

## 1. Read this before anything else

> **The device gate has never been run.** Nothing in this repository has been
> opened on a physical iPhone. `navigator.storage.persist()` has never returned
> `true`. The seven-day soak (assertion 15) has not been started.

`06-build-plan.md` says: *"Do not start a phase before the previous phase's
acceptance criteria pass on real hardware."* Three phases were built past that
gate, deliberately and with the owner's agreement. That is a real risk position,
not an oversight, and it is recorded here so nobody rediscovers it by surprise:

- v0.1's acceptance criteria 1–5 are **device criteria**. None have been run.
- Everything from v0.2 onward assumes OPFS media survives eviction and
  backgrounding on iOS. If it does not, the storage design is wrong and the
  work built on top of it needs revisiting.
- Assertion 15 is a **seven-day wall clock**. It cannot be run on demand. Every
  day it is not started is a day added to the critical path.

The cheapest possible action that reduces this risk: put the app on a phone,
add one track, and walk away. The assertions can be run later; the clock cannot
be started retroactively.

---

## 2. What exists

Eight commits, ~22 source files, two processes.

```
app/                          the PWA — owns all durable media
  index.html
  vite.config.js              PWA config, /api proxy, no-media precache
  src/main.js                 mount; installs the fetch counter first
  src/App.svelte              library, playlists, add flow, player, readiness panel
  src/api.js                  every network call; SSE + NDJSON stream helpers
  src/db.svelte.js            IndexedDB v4: items, local_media, playlists,
                               playlist_items, outbox, meta
  src/outbox.js                offline mutation queue; replays on reconnect
  src/id.js                    client uuid7() for offline-created playlists
  src/opfs-worker.js          the ONLY thing that touches OPFS
  src/sha256.js               incremental digest (Web Crypto has none)
  src/net.svelte.js           fetch counter for assertion 12
  scripts/check-no-cdn.js     build gate: absolute URLs in dist/ fail
  scripts/sha256.test.js      NIST vectors + randomised vs node crypto
  scripts/make-fixtures.js    generates the PWA icons

server/                       stateless transformer — never a media library
  main.py                     endpoints, job runner, SSE, canary, reaper, playlists
  db.py                       schema, pragmas, writer lock, uuid7, now()
  extract.py                  yt-dlp probe: single item or flat playlist enum
  pipeline.py                 fetch + ffmpeg; runs in a subprocess
  test_server.py              8 checks, plain asserts, no pytest
  scripts/seed_queue.py       seeds N ready jobs for queue testing
```

### Endpoints implemented

| Method | Path | Notes |
|---|---|---|
| `GET` | `/health` | |
| `GET` | `/health/extractors` | canary, 6-hourly, 200/503 |
| `POST` | `/resolve` | single item (JSON) or playlist (NDJSON stream) |
| `POST` | `/items` | idempotent on (user, source); updates profile; optional `playlist_id` + per-entry `position` |
| `GET` | `/items` | |
| `DELETE` | `/items/{id}` | soft delete |
| `GET` | `/jobs` | kept for curl; the client uses the stream |
| `GET` | `/jobs/stream` | **SSE**, capped at 5 min, client reconnects |
| `GET` | `/jobs/{id}/artifact/{filename}` | Range, token, `X-Artifact-SHA256` |
| `DELETE` | `/jobs/{id}/artifact` | the collection acknowledgement |
| `POST` | `/jobs/{id}/retry` | |
| `POST` | `/playlists` | client-generated id, `ON CONFLICT DO NOTHING` — idempotent |
| `GET` | `/playlists` | with ordered `{item_id, position}` per playlist |
| `PATCH` | `/playlists/{id}` | rename |
| `DELETE` | `/playlists/{id}` | soft delete |
| `PUT` | `/playlists/{id}/items` | upsert/remove; position is opaque, client-generated (D-018) |

### The pipeline, verified end to end

All seven stages run. Evidence from real runs, not assertion:

- **YouTube** → `aac 127999 bps`, embedded cover, tags, `copied`
- **SoundCloud** → `aac 160045 bps`, embedded cover, tags, `copied`
- **MP3 profile** → `mp3 190133 bps`, id3v2, embedded cover
- Client SHA-256 matches the server's Python `hashlib` digest byte for byte
- `Range: bytes=1000000-1000999` → 206, correct bytes
- Bad token → 403; after the `DELETE` ack → 404, scratch empty
- TTL reaper with a backdated expiry → state `expired`, scratch empty

### Acceptance criteria

| Phase | Criterion | State |
|---|---|---|
| v0.1 | 1–5 (all device criteria) | **NOT RUN** |
| v0.2 | 1 · deleted file degrades cleanly | pass |
| v0.2 | 2 · re-download from `missing` restores playback | pass |
| v0.2 | 3 · fifty queued items, UI not blocked | pass, **with a caveat** |
| v0.2 | 4 · no `.part` masquerading as complete | pass |
| v0.3 | 1 · assertions 9–14 of the offline test protocol | **NOT RUN on device** — see below |
| v0.3 | 2 · playlist streams with a running size estimate | pass, **with a caveat** — real 2-track playlist, not 400 entries |
| v0.3 | 3 · 200-track offline reorder, one outbox row per move, replays after 4h | **mechanism** verified (create/rename/delete a playlist and delete an item while the server was down, then reconnected — outbox drained in order, server state matched); reorder specifically and the 4-hour/200-track scale were not exercised |

Criterion 3's caveat (v0.2): the 50 items were **synthetic artifacts** seeded
by `server/scripts/seed_queue.py`, not fifty real fetches. It measured the
client queue (51 items drained, zero long tasks over 50 ms). **The server has
never been run at fifty concurrent real jobs.**

Criterion 2's caveat (v0.3): a real YouTube playlist (`list=PLLCoMbyL17pY`,
titled "Kids") went through `/resolve` end to end — streamed
`playlist_head`/`entry`/`playlist_done`, rendered "2 tracks found", a running
"~11.4 MB (estimate)" that recalculated to "~5.7 MB" on deselecting one entry
(both figures match `duration_s × bitrate ÷ 8` by hand), then imported,
downloaded, and played both tracks correctly. What it does *not* cover: a
playlist anywhere near 400 entries, so the streaming-render benefit (seeing
entries before the last one arrives) and any large-N client rendering cost are
still unverified.

v0.3 was smoke-tested in a desktop Chrome browser only: create playlist →
resolve → download → add to playlist → play from playlist → reorder buttons
present → kill the server process → create/rename a playlist and delete an
item offline → restart the server → reload → outbox drained and the server
confirmed the mutations landed. That is the desktop analogue of the plane
test the same way v0.2's "both origins killed" check was — **not** a
substitute for the real device protocol, which has still never been run (§1).

One thing this run found that wasn't a code bug: the dev server process died
mid-session with no traceback (looks like the sandbox reaping a long-lived
background process, not an application crash — confirmed by restarting it and
replaying the same requests successfully). Worth knowing as noise if it
happens again while testing this way; not evidence of anything wrong in
`main.py`.

### Verified with both origins killed

Library renders from IndexedDB, artwork from OPFS blob URLs, audio decodes to
the correct duration, readiness panel reads `0 ok / 1 failed` network calls.
This is the desktop analogue of the plane test — it is *not* a substitute for it.

---

## 3. What does not exist

### Not built at all

| Phase | Scope | Notes |
|---|---|---|
| v0.4 | Passkeys, invite codes, magic-link | one hardcoded dev user |
| v0.4 | Per-user budgets, concurrency caps, usage ledger | `usage_ledger` table exists and is never written |
| v0.4 | Multi-device sync, tombstones, LWW | `GET /sync` does not exist |
| v0.4 | Encrypted cookie jar | |
| v0.4 | Backoff, jitter, circuit breaker on 429s | |
| v1.0 | Video (`keep_video`), muxing, video view | pipeline rejects `keep_video` |
| v1.0 | `ffmpeg.wasm` client transcode, COEP | |
| v1.0 | Nightly `VACUUM INTO` backup | |

### v0.3, built this session

Playlist resolve-then-confirm (`/resolve` streams NDJSON for a playlist, one
line per flat-enumerated entry, with per-entry deselection and a running
`~X MB (estimate)` total in the confirm UI); local playlists with fractional-
index reordering (`fractional-indexing` npm package, client-side only — see
D-018); a real offline mutation outbox (`app/src/outbox.js`) that playlist
create/rename/delete, playlist-item add/reorder/remove, and item delete all go
through; and playing from a playlist makes next/previous cycle that
playlist's live order instead of the whole downloaded library.

Prefetch of the *next* track's object URL, called out separately in the build
plan, needed no new code: every downloaded item's object URL is already
resolved eagerly at boot (a v0.0-era decision), so the next track in any queue
is never waiting on OPFS when playback reaches it.

### Built but knowingly incomplete

- **No migrations.** The schema is `CREATE TABLE IF NOT EXISTS`. Changing a
  column means deleting `server/pwa-yt.db`. Fine now, not fine once there is
  data worth keeping — which is the moment v0.4 arrives.
- **Artwork is read from OPFS, not mirrored into IndexedDB.** FM-7 suggests a
  blob store. Both are local so the offline property holds; revisit when a
  sweep has thousands of items to open.
- **ffmpeg progress is coarse.** yt-dlp covers 0 → 0.85, the transform is one
  jump to 0.9. See D-017.
- **`/jobs` is capped at 50 rows.** Fine for one user, wrong the moment a
  library is bigger than the queue view.
- **No auth anywhere.** Every endpoint acts as `DEV_USER_ID`. Do not put this
  on a public address.

### Deferred shortcuts (`ponytail:` markers in code)

| Where | Shortcut | Upgrade when |
|---|---|---|
| `app/src/db.svelte.js` | raw IndexedDB, no `idb` package | playlists + outbox arrived in v0.3 on the same module — still no real migration need, just more stores |
| `app/src/outbox.js` | re-reads the whole outbox store every drain loop instead of a cursor | the outbox ever grows unbounded (it shouldn't — it drains on every reconnect) |
| `app/src/App.svelte` | artwork read from OPFS, not an IDB blob store | a sweep has thousands of items |
| `app/src/App.svelte` | system font stack, no webfont | the design calls for a typeface — then woff2-in-bundle, FM-1 |
| `app/src/sha256.js` | hand-written digest instead of a dependency | never, unless it proves wrong; it is pinned by vectors |
| `server/db.py` | `uuid7()` by hand | Python 3.14 ships `uuid.uuid7()` — delete it then |
| `server/db.py` | no read connection pool | a profiler says connection setup is hot |
| `server/pipeline.py` | no ffmpeg-level progress | a long transcode reads as a hang |

---

## 4. Decisions made during the build

Full reasoning in `08-decisions.md`. Ones that changed the design:

| # | Decision |
|---|---|
| D-010 | A v0.0 probe phase inserted before v0.1 |
| D-011 | System font stack, no self-hosted webfont, in early phases |
| D-012 | ~~Byte-length only~~ **CLOSED** — streaming SHA-256 landed in v0.2 |
| D-013 | The download `fetch` runs inside the OPFS worker |
| D-014 | **`prefer_copy` compares the other way round.** The doc's original rule never fired for YouTube and turned every track into a lossy AAC→AAC transcode that was bigger and worse. `05-formats.md` was corrected. |
| D-015 | The artifact is a set of files, not one blob — `04-api.md` refined |
| D-016 | Snapshot `$state` at the structured-clone boundary, not at call sites |
| D-017 | Job progress travels as a file in scratch, not an IPC queue |
| D-018 | Playlist position is opaque to the server (client-only fractional indexing); the outbox replays via existing idempotent REST calls, no idempotency-key ledger |

---

## 5. Bugs found by testing, so they are not reintroduced

Each of these was invisible until something was actually run:

1. **MediaSession `playbackState` never set** — lock screen shows the wrong
   transport button on iOS.
2. **`setActionHandler` called in a straight line** — Safari throws
   `NotSupportedError` for unimplemented actions, which would have skipped
   `seekto` and left the lock-screen scrubber dead.
3. **Playlist URLs timed out at 60s** with "the site may be slow", when the
   truth was "this is a playlist". Now 2.4s and honest.
4. **Every MP3 job died with `StopIteration`** — `_finish` matched `audio.m4a`
   by exact name.
5. **Changing format silently did nothing** — `ON CONFLICT` did not update
   `format_profile`.
6. **Two `DataCloneError`s** — Svelte `$state` proxies cannot be structured-
   cloned; a spread fixes the outer object and leaves nested arrays broken.
7. **Orphaned media on profile change** — re-downloading as MP3 left the old
   m4a forever. One track was occupying 9.4 MB.
8. **Client pulled jobs for unknown items** — media written to OPFS that no
   library row pointed at. Invisible to the library and to the sweep.
9. **Raw `NotFoundError` shown to the user** when a file was simply evicted.

The pattern: every one of these came from running the thing, not from reading
the code. Assume the same is true of whatever is built next.

---

## 6. Running it

Requires `ffmpeg` on `PATH`. Two processes.

```bash
cd server && uv run uvicorn main:app --port 8000
cd app && npm install && npm run build && npm run preview -- --port 4173
```

Open <http://localhost:4173>. The app proxies `/api` to the server, so it is
one origin — no CORS, and a tunnel in front works with no configuration.

```bash
cd server && uv run python test_server.py    # 8 checks
cd app && npm run test:sha                   # sha256 vectors
cd app && npm run check:no-cdn               # fails on absolute URLs in dist/
```

**Three traps that will cost you an hour each:**

- **The service worker serves the previous shell** until a second load. After
  any rebuild, reload twice before believing what you see. The readiness panel
  prints the build stamp precisely so you can tell.
- **Schema changes need `rm server/pwa-yt.db`.** There are no migrations.
- **The IndexedDB database is named `pwa-yt`.** It was `tarmac` until the
  rename, so any browser profile that used the old build has an orphaned
  `tarmac` database and unreferenced OPFS media under it. Clear site data for
  the origin once and re-add your tracks; there is no migration and, pre-release
  with a two-track test library, there should not be one.

---

## 7. Where to pick up

In the order I would actually do them:

1. **Start the device clock.** Tunnel (`cloudflared tunnel --url
   http://localhost:4173`), add to the iPhone home screen, download two tracks,
   force-quit, leave it alone with the device low on free space. Five minutes of
   work; it starts the only test that cannot be hurried. Still true, still not
   done — v0.3 was built on top of the same risk position v0.2 was.
2. **Run the offline protocol** — `02-offline-playback.md` §5, all 14 assertions
   now that v0.3 exists (9–14 need playlists specifically). The readiness panel
   reports `persist()`, OPFS `move()`, and the fetch counter directly on screen,
   so most assertions are readable without a debugger. The two genuinely
   unknown answers are whether `persist()` returns true and whether Safari
   supports OPFS `move()`.
3. ~~Exercise a real playlist import.~~ **Done** — a real 2-track YouTube
   playlist confirmed the streamed size estimate and full import/download
   path. Still open: a multi-hundred-entry playlist, to see the streaming
   render actually earn its keep and check large-N client cost.
4. **Before v0.4, add migrations.** Accounts are the point where the database
   starts holding data that cannot be thrown away. Deliberately not built yet
   — there is still nothing to migrate, and scaffolding for a migration that
   doesn't exist is exactly the kind of speculative code this project's
   working agreement argues against. Build it when v0.4 actually needs it.
5. ~~Decide whether to cascade-delete `playlist_items`~~ **Done** — deleting a
   library item now cascades server-side (same transaction) and client-side
   (`forget()`), pinned by `test_delete_item_cascades_into_playlists`. See
   D-018.

If step 1 fails — media does not survive on the device — stop and re-read
`02-offline-playback.md` §2 before writing any more code. That is the scenario
the phase ordering existed to catch early, and it would still be much cheaper to
find out now than after v0.4.
