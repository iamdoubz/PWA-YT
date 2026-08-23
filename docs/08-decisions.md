# Decision log

ADR-style. Each entry records what was decided, why, and what would change it.
If you disagree with one, argue it explicitly — do not silently deviate.

---

## D-001 · yt-dlp runs server-side, not in the browser

**Status:** accepted · 2026-08-21

**Context.** The original goal was a pure client-side app with yt-dlp compiled to
WebAssembly, so no server ever touched the media.

**Investigation.** yt-dlp is pure Python with no mandatory C extensions, so
Pyodide genuinely can `import yt_dlp` and execute it. That part works. Everything
downstream does not:

1. **CORS.** Pyodide has no sockets; `urllib`/`requests` are shimmed onto
   `fetch`/XHR and are therefore same-origin-policed. `youtube.com/watch`, the
   InnerTube `youtubei/v1/player` endpoint, and `*.googlevideo.com` do not send
   `Access-Control-Allow-Origin` for an arbitrary origin — YouTube's own player
   works because it *is* the YouTube origin. Every request fails at the browser
   boundary before yt-dlp's logic runs. The standard workaround is a CORS proxy,
   which is a server.
2. **Proof-of-Origin tokens.** YouTube requires PO tokens on streaming (GVS)
   requests for most clients. They are minted by platform attestation (BotGuard
   on web, DroidGuard on Android, iOSGuard on iOS), are not portable between
   platforms, and are usually bound to a specific video id. yt-dlp's own answer
   is a provider plugin such as `bgutil-ytdlp-pot-provider`, which runs a
   headless browser or Node runtime — a server-side sidecar by construction.
3. **SABR.** YouTube's web clients increasingly serve only SABR formats, a
   server-driven adaptive protocol over UMP rather than ranged GETs against a
   static URL. Support changes often.
4. **Update cadence.** A WASM bundle cached in every user's service worker is
   the worst possible place to ship an urgent extractor fix. `docker pull` is
   the best.

**Decision.** Server-side yt-dlp, with the server constrained to be a stateless
transformer (D-002).

**What would change this.** Nothing on the horizon. If YouTube ever published a
CORS-permissive media API, points 1–3 would collapse — but point 4 would still
argue for a server.

**Note.** SoundCloud alone *is* plausible in-browser: its `api-v2` and CDN are
built for embedded widgets on third-party origins. Rejected anyway, because
maintaining a second extraction stack that covers one of two sources and breaks
differently from the first is worse than maintaining one.

---

## D-002 · The server is a stateless transformer, never a media library

**Status:** accepted · 2026-08-21

**Context.** D-001 means bytes transit a server. The requirement worth defending
is not "no server ever touches the bytes" — that is unachievable — but "the
server never accumulates a copy of the library".

**Decision.**
- Per-job scratch on tmpfs, deleted on collection or TTL expiry.
- Database holds metadata only.
- Artifact URLs are signed, single-use, short-TTL.
- The client explicitly `DELETE`s the artifact after committing bytes to OPFS.
- An unswept artifact is a bug with an alarm on it, not an accepted state.

**Consequence.** If the server is compromised, an attacker sees the library
*catalogue* and can observe in-flight jobs. They do not get the collection,
because the collection only exists on user devices.

**Optional stricter mode.** Stream yt-dlp → ffmpeg → HTTP response without a
complete file landing on disk. Costs resumability; offered as a per-user setting,
not the default, because a dropped mobile connection then costs the whole file.

---

## D-003 · Offline playback is the organising principle, not a feature

**Status:** accepted · 2026-08-21

**Context.** Stated directly by the project owner: once the web and server parts
exist, the thing that actually matters is that a user can listen offline.

**Decision.** `02-offline-playback.md` is normative and outranks every other
document. Its definition of done is the acceptance test for the project. v0.1
exists solely to falsify the assumption that media survives on real iOS hardware.

---

## D-004 · SQLite, not Postgres

**Status:** accepted · 2026-08-21 (supersedes the original Postgres choice)

**Context.** The first design pass specified Postgres with a Redis-backed job
queue. At this application's actual scale — a handful of invited users and two
or three concurrent downloads — that is more operational surface than the
workload justifies.

**Decision.** SQLite in WAL mode. The job queue moves into SQLite, removing Redis
from the stack entirely.

**Consequences, good.**
- Entire server state is one file plus one scratch directory. `VACUUM INTO` is a
  complete backup.
- Deployment is two containers and one volume.
- Reinforces D-002: the server holds so little that losing it costs a catalogue,
  not a collection.

**Consequences, to design around.**
- Single writer. One write connection behind an `asyncio.Lock`; separate
  read-only pool. Downloads and transcodes must not hold a connection.
- No `SKIP LOCKED`. Job claim uses `BEGIN IMMEDIATE` + `UPDATE … RETURNING` with
  a correlated subquery enforcing per-user concurrency (`03-data-model.md` §3).
- No `jsonb`. `format_profile` is TEXT with a `json_valid()` check constraint.
- Requires SQLite ≥ 3.35 for `RETURNING`; assert at startup.

**What would change this.** Sustained concurrent write load from many
simultaneous users. The schema is deliberately portable — TEXT timestamps, TEXT
UUIDs, no SQLite-specific types — so a Postgres migration stays cheap if it is
ever warranted.

---

## D-005 · OPFS for media, IndexedDB for the catalogue

**Status:** accepted · 2026-08-21

**Decision.** Media blobs in the Origin Private File System; catalogue, playlists,
outbox, and small artwork blobs in IndexedDB.

**Why not IndexedDB blobs for media.** OPFS gives real file handles, streaming
writes so a 200 MB file never exists in memory as one buffer, sync access handles
for fast range reads while seeking, and better behaviour under quota accounting.

---

## D-006 · The OPFS write path is a Web Worker with `createSyncAccessHandle()`

**Status:** accepted · 2026-08-21

**Context.** There are two ways to write to OPFS and they have very different
support histories.

**Decision.** Primary path is a dedicated Web Worker using
`createSyncAccessHandle()` — baseline widely available since March 2023,
including on older iOS. `createWritable()` only reached baseline availability in
September 2025 and is treated as a nicety for newer browsers, not the foundation.

**Consequence.** One code path to test rather than two. Downloads run in a worker
regardless of browser, which also keeps chunk writes off the main thread.

**What would change this.** Nothing until the minimum supported iOS version is
comfortably past `createWritable()`'s availability — and even then, the worker
path is better for the main thread.

---

## D-007 · AAC default, MP3 optional, video off, artwork on

**Status:** accepted · 2026-08-21 · stated requirement

**Decision.** As stated by the project owner. Implementation detail added during
design: `prefer_copy` stream-copies when the source is already acceptable AAC,
because YouTube itag 140 is 128 kbps AAC and transcoding AAC → AAC costs quality
for nothing.

Also added: profiles are stored **per item**, not only globally, so a single item
can be re-pulled at a different quality without a migration.

---

## D-008 · Multi-user with passkeys, invite-only

**Status:** accepted · 2026-08-21 · stated requirement

**Decision.** Multi-user accounts. Passkeys (WebAuthn) primary with a magic-link
fallback; no passwords. Registration requires an invite code.

**Rationale for invite-only.** yt-dlp reaches third parties from one shared IP,
so per-user isolation and budgets are an operational necessity, not polish. And
an invite-only instance for the owner and people they know is a materially
different proposition from open signup — the blocking problems in the latter
case are not technical ones.

---

## D-009 · Hybrid transcode: server default, client optional, deferred

**Status:** accepted · 2026-08-21 · stated requirement

**Decision.** Server-side ffmpeg by default. Optional client-side `ffmpeg.wasm`
as a **per-user setting**, not a per-download toggle, so there is one code path
per session rather than two interleaved in one queue. Deferred to v1.0.

**Why deferred.** The multithreaded build requires cross-origin isolation, and
COEP applies to the whole origin — it is a whole-app change dressed up as a
feature. `credentialless`, not `require-corp`, so third-party images still load.

**Where it earns its keep.** A shared instance where users would rather the
operator's process never touched decoded media. On a personal box it is largely
ceremony, since the server already ran the extractor.

---

## D-010 · A v0.0 probe phase before v0.1

**Status:** accepted · 2026-08-21 · deviates from `06-build-plan.md`

**Decision.** Insert a phase before v0.1: a static PWA with no server at all,
hardcoded local fixtures instead of downloads, exercising OPFS, the boot path,
the player and MediaSession. v0.1 as written keeps its full scope; it now starts
from a shell that already works.

**Rationale.** The question v0.1 exists to answer — does a file survive on a real
iPhone and play in airplane mode a week later — does not depend on any of the
server half. OPFS does not care that yt-dlp produced the bytes. Assertion 15 is
a **seven-day wall clock**, so starting it on day one instead of day ten takes
nine days off the critical path for the assertion the plan itself calls the one
that determines whether the app is trustworthy. The server gets built while the
soak runs; total work is unchanged.

**What would change it.** Nothing, once the soak has been started. If the probe
fails, the design gets revisited before a line of FastAPI exists, which is the
entire point of ordering the phases this way.

---

## D-011 · System font stack in v0.0, not a self-hosted webfont

**Status:** accepted · 2026-08-21 · revisit at v0.2

**Decision.** `system-ui, -apple-system, sans-serif`. No font files in the
bundle, no subsetting step.

**Rationale.** N2 and FM-1 require that no font is fetched from a CDN. Shipping
zero font files satisfies that more completely than shipping the right ones —
there is nothing to subset, precache, or forget to precache, and the failure mode
being guarded against cannot occur. Revisit when the design calls for a typeface,
at which point the woff2-in-bundle rule in `02-offline-playback.md` §FM-1 applies
in full.

---

## D-012 · Byte-length verification only in v0.0; no SHA-256

**Status:** CLOSED in v0.2 · see the resolution note at the end of this entry

**Decision.** The download worker verifies `Content-Length` against
`accessHandle.getSize()` and nothing else.

**Rationale.** FM-4 requires both length and SHA-256, but `crypto.subtle.digest`
is one-shot — there is no incremental Web Crypto digest — so hashing an 86 MB
file means holding it in memory as one buffer, which is exactly what FM-3
forbids. v0.0 also has no server, so there is no authoritative hash to compare
against; the only available answer would be self-reported. v0.1 emits
`X-Artifact-SHA256`, and the worker gains a streaming hash at the same time.

**What would change it.** Nothing — this is a stub with a known closing date, not
a position.

**Resolution (v0.2).** Closed. `app/src/sha256.js` implements an incremental
SHA-256 and the worker hashes each chunk on its way to disk, so nothing is ever
buffered. The digest is compared against the server's `X-Artifact-SHA256` before
the `.part` rename, and a mismatch deletes the partial file. Pinned by NIST
vectors plus randomised agreement with node's `crypto`; cross-checked in
production against Python's `hashlib` on every download.

---

## D-013 · The download `fetch` runs inside the OPFS worker

**Status:** accepted · 2026-08-21 · refines `01-architecture.md` stage 6

**Decision.** The worker fetches and writes. The main thread posts one message
and receives progress events, rather than reading the response itself and
posting chunks across.

**Rationale.** `01-architecture.md` sketches fetch-on-main → `postMessage`
chunks → worker. Fetching in the worker gives the identical guarantee — still
streamed via `getReader()`, never buffered — with one less hop and no chunk
ping-pong to get wrong. It also keeps every byte of the write path in the one
file that owns OPFS.

**Consequence to remember.** The worker has its own global scope, so the
main-thread fetch counter used for assertion 12 does not see downloads. That is
the desired reading: offline, the count must be zero, and a download is the one
thing that legitimately touches the network.

---

## D-014 · prefer_copy compares the other way round

**Status:** accepted · 2026-08-21 · corrects `05-formats.md`

**Decision.** Stream-copy when the source is already AAC and the requested
bitrate is **not lower** than the source's. Transcode only when the user asked
for something smaller.

**Context.** The original rule was "copy if the source is AAC at or above the
target bitrate". Resolving a reference item shows what YouTube actually offers:

```
   140  mp4a.40.2      129.5 kbps  m4a   <- best AAC available
   251  opus           128.9 kbps  webm
   139  mp4a.40.5       48.8 kbps  m4a
```

With `audio_bitrate` defaulting to 192, `129.5 >= 192` is false, so the copy
path would never have fired for YouTube at all. Every track would take a lossy
AAC → AAC transcode producing a **larger** file containing **worse** audio, and
spending server CPU to do it. Raising a bitrate cannot recover information the
first encoder discarded.

**Rationale for the direction.** The only case where re-encoding AAC is what the
user meant is when they want a *smaller* file — which `05-formats.md` already
cites as the motivating example for storing profiles per item ("re-pull one long
podcast at 128 kbps mono"). Everything else is loss for nothing.

**Verified.** The pipeline reports `copied` for the default profile, and the
output probes as `aac, 127999 bps` — the source stream intact, not a 192k
re-encode. `test_prefer_copy_never_upscales_a_bitrate` pins the rule.

---

## D-015 · The artifact is a set of files, not one blob

**Status:** accepted · 2026-08-21 · refines `04-api.md`

**Decision.** `GET /jobs/{id}/artifact/{filename}` serves one file at a time.
The file list lives in the job row as `artifact_manifest`, carrying per-file
name, byte length and SHA-256, and `GET /jobs` hands the client ready-made URLs.

**Context.** `04-api.md` describes a single `GET /jobs/{id}/artifact` returning
one stream with `X-Artifact-SHA256` and `Content-Length`. But `05-formats.md`
specifies that the pipeline always produces a **set** — `audio.m4a`, `art.jpg`,
`art-sq.jpg`, plus `video.mp4` when `keep_video` — with fixed names that the
client mirrors into OPFS unchanged. One URL cannot carry four files without
inventing a bundle format and a client-side unpacker.

**Rationale.** Per-file URLs mean per-file `Range` resume and per-file length
and hash verification, which is what FM-4 actually wants; a bundle would have to
be fully received before any of it could be checked. It also needs no new client
code — the v0.0 download worker already takes a list of `{name, url}` and writes
each to `.part` before renaming.

**Unchanged.** The `DELETE /jobs/{id}/artifact` acknowledgement is still one call
for the whole set, and still the contract that keeps the server stateless.

---

## D-016 · Snapshot `$state` at the structured-clone boundary, not at call sites

**Status:** accepted · 2026-08-21 · cost two bugs to learn

**Decision.** `db.svelte.js` calls `$state.snapshot()` inside `put()`. Any other
place that hands reactive state to `postMessage` snapshots explicitly.

**Context.** Svelte 5 `$state` values are Proxies, and **structured clone cannot
clone a Proxy**. Both boundaries in this app go through structured clone:

- `worker.postMessage(...)` → `DataCloneError`
- `IDBObjectStore.put(...)` → `DataCloneError`

The verification sweep hit both in succession. Worse, the failure is quiet in
the shapes that matter: a spread like `{ ...row }` produces a plain outer object
whose *nested* arrays are still proxies, so the bug survives the obvious fix and
reappears one layer down. The visible symptom was a button stuck reading
"Checking… 1/1" forever, with the real cause only in the console.

**Rationale for the boundary.** Fixing it per call site means every future
`db.put` is one careless spread away from the same bug. One snapshot inside
`put` makes it structurally impossible, at the cost of forcing that module to be
`.svelte.js` so it can use the rune.

**Also.** `runSweep` wraps its `postMessage` in try/catch and clears the sweep
state on throw. A synchronous failure that leaves the UI claiming work is in
progress is worse than the failure itself.

---

## D-017 · Job progress travels as a file in scratch, not through an IPC queue

**Status:** accepted · 2026-08-21

**Decision.** `pipeline.run` writes `{stage, fraction}` to `progress.json`
inside the job's own scratch directory, debounced to twice a second. The runner
thread polls that file while it waits on the future and republishes to SQLite at
most once per second per job. SSE reads from SQLite.

**Rationale.** The alternative is a `multiprocessing.Manager().Queue()` proxy
passed into the pool worker, which means another process to supervise and a
proxy object to keep alive across a spawn boundary. A file needs neither, is
already inside the directory that gets deleted when the job ends, and survives a
worker that dies mid-job — the last thing it wrote is still there to read.

**Scope.** yt-dlp progress covers 0 → 0.85; the ffmpeg step is one coarse jump
to 0.9. The fetch is the long pole because it moves bytes over someone else's
network, and after D-014 the transform is usually a stream copy that takes
seconds. If a long transcode ever reads as a hang, add `-progress pipe:1` to the
audio encode and parse `out_time_us`. Not before.

---

## D-018 · Playlist ordering lives client-side only; the outbox has no idempotency ledger

**Status:** accepted · 2026-08-22 · v0.3

**Context.** `03-data-model.md` §6 specifies fractional indexing for
`playlist_items.position` and `04-api.md` describes `/sync/outbox` replaying
mutations that "each carry an idempotency key." Both needed a concrete design
for v0.3.

**Decision, part 1 — position is opaque to the server.** The fractional-index
algorithm (base62 midpoint strings, insert-between semantics) is real enough to
get subtly wrong, so it is implemented exactly once, client-side, via the
`fractional-indexing` npm package — not ported to Python too. `playlist_items`
on the server stores whatever string the client sends and never generates or
interprets one itself. This is also why `POST /items` takes a `position` per
entry rather than the server assigning sequential keys during playlist import:
a second implementation of the same algorithm is exactly the kind of subtle
duplication that drifts.

**Decision, part 2 — no idempotency-key ledger.** The outbox (client:
`app/src/outbox.js`) replays queued mutations by re-issuing the *same* REST
call, not a dedicated batch endpoint. This is safe only because every mutation
kind it's used for is already idempotent by construction:

- Creates (`playlist_create`) carry a client-generated UUIDv7 id and the server
  does `INSERT ... ON CONFLICT(id) DO NOTHING`.
- Renames and deletes are last-write-wins by nature — replaying one twice is a
  no-op the second time.
- The playlist-items patch (`playlist_items_patch`) is `ON CONFLICT DO UPDATE`,
  so re-upserting the same `(item_id, position)` twice is harmless.

**What this doesn't cover.** Genuine multi-device conflict resolution — two
devices reordering the same playlist offline and reconciling via `updated_at`
— is still v0.4's `/sync` pull, not this. The outbox only replays this
device's own queued mutations in order; it does not pull anyone else's.

**Known gap, closed.** Deleting a library item now cascades into every
playlist that held it — `DELETE /items/{id}` soft-deletes the matching
`playlist_items` rows in the same transaction, and `forget()` mirrors that
locally so the tombstone exists offline too, not just after a round trip.
Pinned by `test_delete_item_cascades_into_playlists`.

**What would change this.** The idempotency-ledger approach becomes necessary
the moment a mutation kind is *not* naturally idempotent — at that point add
one for that kind specifically, rather than retrofitting a ledger everything
has to carry.

---

## D-019 · Usernameless passkeys, an `invites` table, and in-memory ceremonies

**Status:** accepted · 2026-08-22 · v0.4 (auth foundation)

**Context.** D-008 decided passkeys, multi-user, invite-only. Making that
real required filling in specifics the original design pass left open.

**Decision 1 — usernameless (discoverable/resident) credentials throughout.**
Registration requests `resident_key=REQUIRED`; login sends no
`allow_credentials` at all. The authenticator's own passkey picker is the
entire identity UI — there is no username field anywhere in the client. This
is friendlier than username-first WebAuthn and costs nothing extra to
implement; the alternative (asking for an email/username before every login)
is strictly more UI for no security benefit once passkeys are the only factor.

**Decision 2 — an `invites` table that 03-data-model.md never specified.**
`03-data-model.md` only ever had `users.invited_by`, which records *who*
invited someone after the fact but gives registration nothing to check
*before* creating an account. Added `invites(code, created_by, used_by,
created_at, used_at)` — single-use, no expiry (an operator revoking access is
"disable the user," not "invite codes rot"). Minting one is a script
(`scripts/create_invite.py`), not an endpoint — invites are the operator's
action, not a thing users request from within the app.

**Decision 3 — WebAuthn ceremonies live in an in-memory dict, not a table.**
Registration and login are two-step: `begin` generates a challenge the
browser must sign and `finish` verifies the signature against it. That
challenge has to be held somewhere between the two calls. A `pending_
ceremonies` table would need its own reaper, its own index, and would still
only ever hold rows with a 5-minute lifetime. This app is one process (the
FastAPI layer, not the resolve/job pools), so a `dict` behind a `Lock`,
swept opportunistically on each new ceremony, does the same job with no
schema and nothing to leak across a restart worth caring about — a dropped
ceremony mid-restart is just "try signing in again," identically to what a
5-minute TTL already produces on its own.

**Decision 4 — library: `webauthn` (py_webauthn) server-side,
`@simplewebauthn/browser` client-side.** Same spec, same JSON encoding
conventions (SimpleWebAuthn's ecosystem is why these two interoperate without
either side hand-rolling base64url⇄ArrayBuffer conversions) — exactly the
kind of subtle binary-encoding logic worth a real dependency rather than
reimplementing, on both ends. Client output (`RegistrationResponseJSON` /
`AuthenticationResponseJSON`) is accepted by the server's `verify_*_response`
as a plain dict with zero translation in between.

**Decision 5 — `expected_origin` reuses `PWA_YT_ORIGINS`, but rejects `*`.**
CORS tolerates a wildcard; WebAuthn has no such concept — an origin is exact
or it isn't accepted. `auth.py` reads the same env var main.py's CORS
middleware does and filters out `*`, falling back to
`http://localhost:4173` if nothing concrete is configured. `PWA_YT_RP_ID`
is separate (a bare hostname, no scheme/port) and defaults to `localhost`.

**Consequence to remember when testing through a tunnel.** A passkey is
bound to `RP_ID` for its entire life. A `cloudflared` quick tunnel gets a new
random hostname every run, which would silently orphan every passkey
registered through the previous one. Testing on a phone through a tunnel
needs a stable hostname (a named tunnel, not a quick one) for this reason —
unrelated to, but just as real as, the existing tunnel requirement for OPFS/
service-worker HTTPS.

**What would change this.** Nothing about the resident-key or ceremony-
storage decisions. The origin/RP_ID story gets revisited if this ever runs
behind a fixed production domain instead of a dev tunnel.

---

## D-020 · The rest of v0.4 — sync, budgets, cookies, backoff

**Status:** accepted · 2026-08-22 · v0.4 (continuation of the foundation pass)

**Decision 1 — no `/sync/outbox` endpoint, full stop.** `04-api.md` originally
specified one, with per-mutation idempotency keys. D-018 already established
that the *push* half of offline mutation needs neither: every mutating
endpoint is naturally replay-safe (client-generated ids, last-write-wins,
`ON CONFLICT DO UPDATE`), so the client's outbox just re-issues the original
REST call. Multi-device sync's actual gap was never push — it was *pull*:
nothing let a device learn what another device signed into the same account
did. `GET /sync` is that, and only that.

**Decision 2 — the sync cursor is opaque, per-table, and row-value-compared.**
Three resources (`items`, `playlists`, `playlist_items`) have different key
shapes — a single global timestamp cursor can't serve all three without
either skipping same-millisecond rows or over-fetching. The cursor is a
base64url JSON blob of one `(updated_at, id)` position per table (three
elements for `playlist_items`, whose key is `(playlist_id, item_id)`), and
every query uses SQLite row-value comparison — `(updated_at, id) > (?, ?)` —
which is exactly the guarantee 03-data-model.md's cursor design asked for.
A corrupt or foreign cursor decodes to "sync everything" rather than a 500.

**Decision 3 — no pagination on `/sync`, `/jobs` stays capped at 50.** Both
are documented, deliberate gaps at this app's actual scale (a handful of
invited users, personal libraries). Revisit if a library or an account's
history ever gets big enough for one `/sync` response to matter.

**Decision 4 — the budget check is a gate, not a ledger-precise projection.**
`POST /items` refuses new jobs once today's *already-recorded* usage meets
or exceeds `daily_byte_budget` — it does not try to project whether *this*
batch's estimated size would tip it over first. Getting that exactly right
would mean threading `estimated_bytes` from `/resolve` through every
`ItemEntry`, for a budget that's already just a rough daily cap, not
billing. Actual bytes are recorded in `usage_ledger` only once a job
finishes (`_record_usage`), against what was actually produced — the
`/resolve`-time estimate is what a user sees before committing, never what
counts against them after.

**Decision 5 — cookies are Fernet-encrypted, and a decrypt failure degrades
silently.** `cryptography` was already a transitive dependency (via
`webauthn`) and is now a direct one, since main.py imports it itself.
`PWA_YT_COOKIE_KEY` is generated per-process-start and printed to the log if
unset — convenient for a quick local run, but anything meant to survive a
restart must set it explicitly. If cookies were encrypted under a key that's
since changed, `_user_cookies()` returns `None` rather than raising: a
resolve or download for a user whose private-content cookies no longer
decrypt must still work for everything that isn't gated behind them, not
fail outright over stale secrets.

**Decision 6 — the circuit breaker pauses claiming globally, not just the
one job that hit a 429.** R-10's actual risk is a shared server IP getting
rate-limited on behalf of every user at once. Retrying the one job that got
a 429 (with backoff) protects nothing else already queued behind it —
pausing `_runner()`'s claim loop entirely for an exponentially-growing
window (30s, 60s, 120s… capped at 10 minutes, plus jitter) does. A
successful job resets the counter; the breaker's state rides along on
`/health/extractors`, the same place the canary already reports extractor
health, since both are "is the yt-dlp path OK right now" questions.
Deliberately *not* built: automatic per-job retry-with-backoff for
non-429 failures — that's a different, larger problem (distinguishing
transient from permanent failures) and the manual retry button already
covers it.

**What would change this.** Pagination, the moment either `/sync` or `/jobs`
actually gets slow at real scale. The budget-gate approximation, if daily
overshoot from in-flight jobs ever proves to matter in practice.

---

## D-021 · Two IDOR bugs found by a live cross-account audit, both fixed

**Status:** fixed · 2026-08-23

**Context.** Asked to sanity-check that a logged-out or cross-account
request can't reach someone else's playlist/songs. Every endpoint's
`Depends(auth.current_user)` wiring was already right (confirmed: every
non-public endpoint 401s with no session at all), but two endpoints had an
**authorization** gap despite the **authentication** being solid — the
classic split between "who are you" and "are you allowed to touch this
specific row." Both were reproduced live with two real accounts (not just
read from the code) before being fixed.

**Bug 1 — `DELETE /items/{item_id}` let one account edit another account's
playlist.** The playlist_items cascade added in D-018 ran unconditionally,
keyed only on `item_id`:

```python
conn.execute("UPDATE library_items SET deleted_at=... WHERE id=? AND user_id=?", ...)
conn.execute("UPDATE playlist_items SET deleted_at=... WHERE item_id=?", ...)  # no user check!
```

The first line correctly no-ops when you don't own the item (the `WHERE`
excludes it). The second line doesn't care whose item it is — it fires
regardless. Live repro: Bob, with a real session and knowing Alice's real
`item_id`, called `DELETE /items/{alice's_item_id}`, got back `{"ok": true}`,
and Alice's track was gone from her own playlist — while her
`library_items` row for it was untouched, so nothing about the response
even hinted at what had happened. **Fix:** gate the cascade on
`RETURNING id` actually matching a row, i.e. only cascade if the caller
genuinely owned (and thus actually deleted) the item.

**Bug 2 — `PUT /playlists/{id}/items` let one account attach another
account's item into their own playlist.** `_own_playlist` checked that the
*playlist* belonged to the caller, but nothing checked that the *item_id* in
each upsert did — only playlist ownership gates the call, and the FK to
`library_items` is satisfied by any user's row, not just the caller's. Live
repro: Bob created his own playlist, then `PUT`-upserted Alice's real
`item_id` into it, and it succeeded — his playlist now held a foreign
reference to her private library item. **Fix:** before applying any upserts,
check every `item_id` in the batch belongs to the caller
(`library_items WHERE id=? AND user_id=? AND deleted_at IS NULL`); reject
the whole call with 404 if any doesn't, rather than partially applying.

**Why these two and not others.** Every other mutating endpoint either
scopes its own `WHERE user_id=?` directly on the row being written (safe by
construction — a mismatched id just means 0 rows affected, e.g.
`rename_playlist`, `delete_playlist`, `retry_job`), or gates on an ownership
check with an early return before doing anything (safe by construction, e.g.
`collect_artifact`'s ownership `SELECT` before its `UPDATE`). These two were
different: multi-statement writes where an earlier statement's ownership
check didn't actually bound a *later* statement's blast radius — the general
shape to watch for in anything added on top of this.

**Verification.** Both attacks reproduced live against real HTTP requests
with two real accounts (not read from the code, not just unit tests), fixed,
then re-run against the fix and confirmed blocked — Bug 1's delete now
leaves the other account's playlist untouched, Bug 2's attach now 404s and
attaches nothing. Legitimate same-user operations (owner deleting their own
item and cascading correctly; owner adding their own item to their own
playlist) re-verified unaffected. Two permanent regression tests added:
`test_delete_item_does_not_touch_another_users_playlist`,
`test_patch_playlist_items_rejects_another_users_item_id`.

**What would change this.** Nothing about the fix. The general lesson —
audit every multi-statement write for whether a *later* statement inherits
an *earlier* statement's ownership check, or needs its own — applies to
`/sync` and anything else touching more than one table per call, and is
worth re-running whenever a new multi-row mutation is added.

---

## D-022 · SSRF: restrict yt-dlp to the two supported extractor families

**Status:** fixed · 2026-08-23 · found during the same hardening pass as D-021

**Context.** `/resolve` and the download pipeline both hand a user-supplied
URL straight to `yt_dlp.YoutubeDL.extract_info()`. Neither ever restricted
*which* extractor yt-dlp is allowed to use for it. yt-dlp ships a `generic`
extractor that activates for any URL no specific site extractor claims, and
it will fetch that URL as a webpage — which means, unrestricted, an
authenticated user could ask this server to fetch **any address the server
can reach**: a cloud metadata endpoint, a service on localhost, a `file://`
path. This app supports exactly two sources by design (D-001) — the generic
fallback was never something the product needed, only something nobody had
turned off.

**Reproduced live before fixing**, same as D-021: `POST /resolve` with
`http://169.254.169.254/latest/meta-data/` and with
`http://127.0.0.1:8000/health` both resolved through the *live* server
(before the fix) as far as yt-dlp attempting them via the generic
extractor's normal flow — the class of request a real cloud deployment's
metadata endpoint would answer.

**Fix.** `yt_dlp.YoutubeDL`'s `allowed_extractors` param takes a list of
regexes matched (case-insensitive, full match) against extractor names;
`extract.ALLOWED_EXTRACTORS = ["youtube.*", "soundcloud.*"]` is set on every
`YoutubeDL(...)` construction in both `extract.py` (resolve) and
`pipeline.py` (download) — the restriction has to apply at *every* yt-dlp
call site, not just the first one a URL passes through, since a URL that
looks like YouTube at resolve time isn't re-validated before the download
job actually runs it again. A URL outside the allowlist now fails
`extract_info()` immediately via yt-dlp's own "no suitable extractor
found" error, which is decided by regex-matching the URL against each
extractor's `_VALID_URL` pattern — **no request is made** for a rejected
URL, confirmed by both the near-instant rejection time and by aiming it at
addresses that would otherwise be reachable and observable.

**Verification.** Live, through the real running server with a real bearer
token: a genuine YouTube URL still resolves correctly; a cloud-metadata-
style address and a loopback address to the server's own `/health` both
get cleanly refused with `422 resolve_failed`. A permanent regression test
(`test_ssrf_extractor_allowlist_blocks_out_of_scope_urls`) pins the
behavior without touching the network, since rejection happens before any
request would be sent.

**What would change this.** Adding a third supported source means adding
its extractor name(s) to `ALLOWED_EXTRACTORS` in the same place — not
somewhere new. If this app ever needs truly arbitrary direct-media-URL
support (not currently a stated goal anywhere in the docs), that would need
its own explicit, separately-considered decision — not a side effect of
leaving the default wide open.

---

## D-023 · Cookie jar gets an explicit size cap

**Status:** fixed · 2026-08-23 · found during the same hardening pass

**Context.** `CookiesRequest.cookies` had `min_length=1` and no upper
bound, and it's stored as a `BLOB` that gets read back and decrypted into
memory on every resolve or download call for that user thereafter — unlike
most request fields, this one both persists indefinitely and gets paid for
repeatedly, not just once at request time.

**Fix.** `max_length=256 * 1024` (256 KiB) — real Netscape cookie exports,
even with hundreds of cookies, run a few KB; this leaves generous headroom
without leaving the column truly unbounded. Verified live: a normal-sized
cookie value still saves (`200`); a 300 KB one is rejected (`422`) before
ever reaching the database.

**Scope note, deliberately not addressed here.** This caps one field after
Pydantic parses the body — it does not stop a request with a giant body
from being read into memory in the first place (Starlette has no default
global request-size limit, and none is configured). Closing that fully
would mean a body-size-limiting layer in front of every endpoint, which is
a materially bigger change for a threat this app's own risk register
already prices in: R-12 explicitly keeps registration invite-only precisely
because the user base is "the owner and people they know," not an
adversarial public. Revisit if that scope assumption ever changes.
