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
