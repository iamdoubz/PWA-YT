# Build status

**As of:** 2026-08-22 · commit `bcfb0e3` + uncommitted v0.4 work (rest of it)
**Name:** the project was briefly codenamed *Tarmac*; it is **PWA-YT** everywhere now
**Phases claimed complete:** v0.0 (added), v0.1, v0.2, v0.3, v0.4. All five
v0.4 subsystems are now built: passkeys + invites + sessions, per-user
budgets + usage ledger, multi-device `/sync`, the encrypted cookie jar, and
429 backoff/circuit-breaker. One thing in it is still genuinely unverified —
see §1a.
**Verified on:** desktop Chrome only. Server-side auth/budgets/cookies/sync
were verified live with **two real accounts** (not just unit tests) by
minting sessions directly and driving the actual HTTP surface — real
multi-user isolation, real cross-device convergence. The one thing *not*
verified live is completing an actual WebAuthn signature — that reaches a
real native passkey prompt (confirmed) but needs a human at an authenticator
to finish, same category of gap as the device test in §1.

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

## 1a. What's real evidence vs. what still needs a human at an authenticator

v0.4 bundles five fairly independent subsystems (`06-build-plan.md`): passkeys
+ invite codes + sessions, per-user budgets + usage ledger, multi-device
`/sync`, the encrypted cookie jar, and 429 backoff/circuit-breaker. Asked up
front how to sequence them, the owner chose **foundation first, then pause
for review**; the foundation landed, got reviewed, and this session finished
the other four.

**The one thing still not verified live is completing an actual WebAuthn
signature.** That needs a real authenticator (Windows Hello, Touch ID, a
security key) responding to a native OS prompt — the same category of gap as
the device test in §1, for the same reason: it needs a human at the
keyboard, not another round of automation. What *was* verified, live:

- The sign-in/register screen renders with zero network calls (FM-2), and is
  usernameless — no username field anywhere, by design (D-019).
- `navigator.credentials.create()` correctly rejects `127.0.0.1` as an invalid
  WebAuthn domain (a real spec rule, not a bug) — retrying through `localhost`
  cleared that and reached the real ceremony.
- Registration begin → the browser's native passkey prompt actually opened
  (`document.visibilityState` flipped to `'hidden'` with `hasFocus()` still
  `true` — the tab yielding to a real OS-level dialog, not a JS error).
  Automation has no way to drive that dialog (it's outside the DOM/CDP
  entirely), so the ceremony was abandoned there rather than forced through,
  and abandoning it left **no trace**: the invite code stayed unused, no
  partial user row was created. `finish_registration`'s transaction working
  as designed, not luck.

**Everything downstream of "have a session token" was verified live anyway**
— by minting real sessions directly (`auth.create_session()`, bypassing only
the WebAuthn signature itself, not any authorization logic) for two real
accounts, Alice and Bob, and driving the actual HTTP surface and the actual
browser client against them:

- **Acceptance criterion v0.4-1** (two users can't see/affect each other's
  data) — Bob's `PATCH /playlists/{alice's-id}` returned `404 not_found` and
  genuinely had no effect; `GET /playlists` as Bob returned `[]` while
  Alice's existed. Not code-reading — an actual cross-account request that
  actually failed the way it's supposed to.
- **Cookie jar isolation** — Alice's `GET /me/cookies` showed `configured:
  true` after saving some; Bob's showed `false`. Different users, different
  encrypted blobs, no leakage.
- **Multi-device sync (criterion v0.4-4)** — created a playlist as Alice,
  synced, got a cursor; renamed the *same playlist* via a second `curl` call
  (simulating a second device) without ever pulling again; re-synced from the
  *first* device's old cursor and got back exactly that one change. Then, in
  an actual browser with a seeded session token and zero prior local state,
  the client's `reconcile()` pulled that server-side rename down into
  IndexedDB and rendered it correctly on first load — the whole client-side
  merge path, not just the server query.
- **Push still works post-sync** — renamed the playlist again from *within*
  that browser session; `GET /playlists` via `curl` immediately showed the
  new name. Round trip confirmed both directions.
- **Usage/budget** — `GET /me/usage` returned correct, isolated figures per
  account; the client's account panel displayed them.
- Every server-side path that doesn't need a real signature — invite
  validation, session lifecycle, ceremony single-use/expiry, usage ledger
  accumulation + budget gating, cookie encrypt/decrypt + key-rotation
  degradation, circuit-breaker backoff math, sync cursor correctness and
  ownership scoping — is pinned by `test_server.py` (18 checks, 0 failures).

So: acceptance criterion v0.4-1 (and -3, -4) are about as verified as they
can be **without** a real passkey completing — the only piece that couldn't
be exercised this way is the cryptographic signature ceremony itself, because
there was no way to fabricate a valid one without an actual authenticator.

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
  src/sync.js                  pull half of multi-device convergence (LWW)
  src/id.js                    client uuid7() for offline-created playlists
  src/opfs-worker.js          the ONLY thing that touches OPFS
  # auth: no separate module — bearer token + auth calls live in api.js,
  # sign-in/register UI lives in App.svelte alongside everything else
  src/sha256.js               incremental digest (Web Crypto has none)
  src/net.svelte.js           fetch counter for assertion 12
  scripts/check-no-cdn.js     build gate: absolute URLs in dist/ fail
  scripts/sha256.test.js      NIST vectors + randomised vs node crypto
  scripts/make-fixtures.js    generates the PWA icons

server/                       stateless transformer — never a media library
  main.py                     endpoints, job runner, SSE, canary, reaper, playlists
  db.py                       schema, pragmas, writer lock, uuid7, now()
  auth.py                     passkeys, invite codes, bearer sessions
  extract.py                  yt-dlp probe: single item or flat playlist enum
  pipeline.py                 fetch + ffmpeg; runs in a subprocess
  test_server.py              18 checks, plain asserts, no pytest
  scripts/seed_queue.py       seeds N ready jobs for queue testing
  scripts/create_invite.py    mints an invite code (operator action, no endpoint)
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
| `POST` | `/auth/register/begin` | `{invite_code, email, display_name?}` → `{ceremony_id, options}` |
| `POST` | `/auth/register/finish` | `{ceremony_id, credential}` → `{token, expires_at, user}` |
| `POST` | `/auth/login/begin` | usernameless — no body, empty `allowCredentials` |
| `POST` | `/auth/login/finish` | `{ceremony_id, credential}` → `{token, expires_at, user}` |
| `POST` | `/auth/logout` | invalidates the session server-side only |
| `GET` | `/me` | profile, `daily_byte_budget`, `max_concurrent` |
| `GET` | `/me/usage` | `{bytes_used_today, daily_byte_budget, remaining_bytes, active_jobs}` |
| `PUT` | `/me/cookies` | `{cookies}` (Netscape format) → Fernet-encrypted at rest |
| `GET` | `/me/cookies` | `{configured, updated_at}` — write-only, never returns the jar |
| `DELETE` | `/me/cookies` | clears it |
| `GET` | `/sync?since={cursor}` | changed rows + tombstones across items/playlists/playlist_items; opaque per-table cursor (D-020) |

Every endpoint above `/auth/*`, `/health`, and `/health/extractors` now
requires a valid bearer session (`Depends(auth.current_user)`) and scopes its
query by that user's id — including the playlist endpoints, which previously
had **no ownership check at all** on `PUT /playlists/{id}/items` (anyone
could patch any playlist by id). Fixed as part of this pass, not a
regression introduced by it — v0.3 had no auth at all yet, so there was
nothing to check against.

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
| v0.4 | 1 · two users can't see/affect each other's items, jobs, scratch | **pass, verified with two real accounts** — Bob's cross-account playlist rename 404'd and had no effect; Bob's `/playlists`, `/me/cookies` both came back empty/unconfigured while Alice's held real data. See §1a. |
| v0.4 | 2 · expired session offline → read-only, not logout | built (`readOnly` flag on a 401 or a locally-expired `expires_at`; `db.remove('meta','session')` is the *only* thing logout touches); not yet exercised against a real expired session in a real browser |
| v0.4 | 3 · one user's full queue doesn't delay another's jobs | **inherited, not new** — the per-user `max_concurrent` correlated subquery in `_claim()` has been there since v0.1; two-account isolation confirmed for reads/writes (criterion 1), not specifically for queue contention under load |
| v0.4 | 4 · same account converges across two devices | **pass, verified live** — a server-side change (simulating device 2) showed up correctly via `/sync` from device 1's old cursor, and a fresh browser client with a seeded session pulled and rendered it correctly on load. See §1a. |

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
| v0.4 | Magic-link fallback | deliberately not built, not just unstarted — see 04-api.md |
| v0.4 | `PUT /me/settings` | no real per-user setting exists yet to justify it — see 04-api.md |
| v1.0 | Video (`keep_video`), muxing, video view | pipeline rejects `keep_video` |
| v1.0 | `ffmpeg.wasm` client transcode, COEP | |
| v1.0 | Nightly `VACUUM INTO` backup | |

All other v0.4 scope — per-user budgets/usage ledger, multi-device sync,
encrypted cookie jar, 429 backoff/circuit-breaker — is built. See below.

### v0.4 auth foundation, built this session

Passkeys (WebAuthn) end to end: usernameless registration and login,
invite-only via a new `invites` table `03-data-model.md` never originally
specified (D-019), bearer sessions (30-day TTL, SHA-256-hashed at rest), and
every existing endpoint now behind `Depends(auth.current_user)` scoped to
the caller's own id — including two playlist ownership checks
(`PUT /playlists/{id}/items`, and the `/items` `playlist_id` attach path)
that had **no check at all** before this pass, v0.3 having shipped with no
auth yet to check against.

Client: a sign-in/register screen (no username field, ever — the
authenticator's passkey picker is the whole identity UI), a bearer token
attached to every request, `/jobs/stream` rewritten from `EventSource` to a
hand-parsed SSE reader (`EventSource` cannot set an `Authorization` header,
and a token in the URL would leak into logs), and a `readOnly` flag that a
401 or a locally-expired session sets — banner shown, library still fully
usable from IndexedDB, **logout never touches** `items`/`local_media`/OPFS
(FM-2).

Also fixed in passing: CORS was missing `PATCH`/`PUT` in `allow_methods`
(latent since v0.3's playlist endpoints landed — never surfaced because dev
traffic is same-origin through the Vite proxy).

### v0.4, the rest of it, built this session

**Budgets + usage ledger.** `_finish()` records actual produced bytes into
`usage_ledger` per user per UTC day; `POST /items` refuses new jobs once
today's recorded usage already meets `daily_byte_budget`, returning the
`quota_exceeded` shape 04-api.md already specified, `retry_after` set to the
next UTC midnight. `GET /me` and `GET /me/usage` expose it; the client's
Account panel shows both, loaded lazily post-first-paint like everything
else optional (FM-2).

**429 backoff + circuit breaker (R-10).** A 429 detected in any job's error
message pauses `_runner()`'s *claiming* entirely — not just that one job's
retry — for an exponentially growing window (30s → 60s → 120s… capped at 10
minutes, plus jitter), because a shared-IP rate limit is everyone's problem
the moment it happens to one job. A completed job resets the backoff.
State rides along on `/health/extractors` as `circuit_breaker`.

**Encrypted cookie jar.** `PUT/GET/DELETE /me/cookies`. Fernet
(`cryptography`, already a transitive dep via `webauthn`, now direct)
encrypts a Netscape-format cookie export at rest; decrypted only for the
lifetime of one resolve or download call, written to a temp file (or the
job's own already-ephemeral scratch dir) and never anywhere else. A key
rotation degrades a user's cookies to "not configured" rather than raising —
verified by a test that swaps the encryption key mid-test and confirms the
old ciphertext just stops decrypting, cleanly. Client: a paste-a-cookies.txt
textarea in the Account panel, Save/Clear, status only ("configured N ago"),
never the plaintext back out.

**Multi-device sync (`GET /sync`).** The pull half only — the push half
needed no new code, since every offline mutation already replays through
idempotent REST calls (D-018). Cursor is an opaque base64url JSON blob
carrying one `(updated_at, id)` position per table (`items`, `playlists`,
`playlist_items`), compared with SQLite row-values so same-millisecond rows
are never skipped. Client (`app/src/sync.js`) applies last-write-wins,
tombstone-wins-ties, and for `items` specifically triggers the same OPFS +
`local_media` purge a manual delete does — a remote delete must clean up
local bytes exactly like a local one does. `04-api.md`'s originally-planned
`POST /sync/outbox` was **not built**; D-020 explains why it turned out to
be redundant with the existing outbox design.

**Verification for all four was live, not just unit tests** — see §1a for
exactly what was exercised with two real accounts and a real browser client.

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

- **No migrations, still.** The schema is `CREATE TABLE IF NOT EXISTS`.
  Changing a column means deleting `server/pwa-yt.db` — including now, with
  real accounts. Deliberately still not built (there is nothing to migrate
  yet); add it the moment a schema change needs to preserve real user data
  rather than a personal test library.
- **Artwork is read from OPFS, not mirrored into IndexedDB.** FM-7 suggests a
  blob store. Both are local so the offline property holds; revisit when a
  sweep has thousands of items to open.
- **ffmpeg progress is coarse.** yt-dlp covers 0 → 0.85, the transform is one
  jump to 0.9. See D-017.
- **`/jobs` is capped at 50 rows; `/sync` has no cap at all.** Both fine at
  this app's actual scale (a handful of invited users, personal libraries);
  both wrong the moment either gets big. See D-020.
- **The budget check is a gate, not a precise ledger projection.** `POST
  /items` refuses a new job once *already-recorded* usage meets the daily
  cap — it doesn't try to project whether this specific batch's estimated
  size would tip it over first. Good enough for a rough daily cap; not
  byte-exact.
- **The client's local catalogue is not namespaced per user.** IndexedDB
  store `pwa-yt` is one shared set of `items`/`playlists`/`local_media` rows
  regardless of which account is signed in. Fine for the app's actual usage
  pattern (one person, one device); a real gap if two different accounts
  ever sign into the *same* browser profile — they'd see each other's cached
  local rows even though the server correctly refuses to serve them.
  Namespacing every store by user id is out of scope for the auth
  foundation pass; revisit if a shared device ever becomes a real scenario.
- **`db.DEV_USER_ID` still exists**, seeded on every `db.init()`, and is what
  `scripts/seed_queue.py` seeds jobs under. It's just a row nothing can log
  in as any more — no credentials point at it. Harmless as a fixture; any
  catalogue rows created under it before auth existed are now orphaned
  (unreachable — nobody can authenticate as `dev@localhost`). Wipe
  `server/pwa-yt.db` for a clean v0.4 state, same as any other schema change.

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
| D-019 | Usernameless (discoverable-credential) passkeys throughout; an `invites` table 03-data-model.md never specified; WebAuthn ceremonies held in an in-memory dict, not a table |
| D-020 | No `/sync/outbox` endpoint — pull-only `/sync` with a per-table opaque cursor; budget check is a gate not a ledger projection; cookie decrypt failures degrade silently; circuit breaker pauses claiming globally, not per-job |

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

Open <http://localhost:4173> — **must be `localhost`, never `127.0.0.1`**,
now that auth exists (see the fourth trap below). The app proxies `/api` to
the server, so it is one origin — no CORS, and a tunnel in front works with
no configuration.

Registration needs an invite code first:

```bash
cd server && uv run python scripts/create_invite.py    # prints a code, e.g. 5ae3ce6f9a1d
```

```bash
cd server && uv run python test_server.py    # 18 checks
cd app && npm run test:sha                   # sha256 vectors
cd app && npm run check:no-cdn               # fails on absolute URLs in dist/
```

Set `PWA_YT_COOKIE_KEY` (a Fernet key) if you want saved cookies to survive a
restart — otherwise one is generated and printed on every startup, and
anything encrypted under the previous run's key silently stops decrypting
(by design, see D-020 — not a bug, but worth knowing before it looks like one).

**Four traps that will cost you an hour each:**

- **The service worker serves the previous shell** until a second load. After
  any rebuild, reload twice before believing what you see. The readiness panel
  prints the build stamp precisely so you can tell.
- **Schema changes need `rm server/pwa-yt.db`.** There are no migrations.
- **The IndexedDB database is named `pwa-yt`.** It was `tarmac` until the
  rename, so any browser profile that used the old build has an orphaned
  `tarmac` database and unreferenced OPFS media under it. Clear site data for
  the origin once and re-add your tracks; there is no migration and, pre-release
  with a two-track test library, there should not be one.
- **`127.0.0.1:4173` and `localhost:4173` are different origins** — separate
  IndexedDB, separate service worker, and as of v0.4, only one of them works
  at all: WebAuthn's spec rejects an IP address as a valid domain outright
  (`"127.0.0.1 is an invalid domain"`, straight from the browser, before any
  server code runs). `PWA_YT_RP_ID` defaults to `localhost` for exactly this
  reason. A passkey is bound to whichever hostname it was created under for
  its entire life — through a `cloudflared` tunnel that means a **named**
  tunnel with a stable hostname, not a quick one that gets a new random
  hostname every run (D-019).

---

## 7. Where to pick up

v0.4 is code-complete. What's left is verification that needs a human, plus
v1.0. In the order I would actually do them:

1. **Start the device clock.** Tunnel (`cloudflared tunnel --url
   http://localhost:4173` — a **named** tunnel now, see the fourth trap in §6),
   add to the iPhone home screen, download two tracks, force-quit, leave it
   alone with the device low on free space. Five minutes of work; it starts
   the only test that cannot be hurried. Still true, still not done — every
   phase since v0.1 was built on top of the same risk position.
2. **Complete a real passkey registration.** The one thing in all of v0.4
   that needs a human, not more code — open the app on an actual device or
   desktop Chrome with Windows Hello / Touch ID set up, mint an invite
   (`uv run python scripts/create_invite.py`), and register. Everything up to
   the native prompt is verified (§1a); this is the last unverified step, and
   completing it with a *second* real account would let assertion v0.4-1
   move from "verified with seeded sessions" to "verified with the actual
   passkey flow a real user goes through."
3. **Run the offline protocol** — `02-offline-playback.md` §5, all 14
   assertions. The readiness panel reports `persist()`, OPFS `move()`, and
   the fetch counter directly on screen, so most assertions are readable
   without a debugger. The two genuinely unknown answers are whether
   `persist()` returns true and whether Safari supports OPFS `move()`. This
   now also needs step 2 done first, since the library only renders signed in.
4. **v1.0**, when it's time: video (`keep_video`), muxing, a video view,
   optional `ffmpeg.wasm` client transcode behind COEP, nightly
   `VACUUM INTO` backups.
5. **Magic-link fallback**, if it turns out to matter in practice — passkeys
   alone have been enough to build and test everything so far. Add it when
   someone actually needs the recovery path, not before.

If step 1 fails — media does not survive on the device — stop and re-read
`02-offline-playback.md` §2 before writing any more code. That is the scenario
the phase ordering existed to catch early, and it would still be much cheaper
to find out now than after v1.0.
