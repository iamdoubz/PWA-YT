# What will bite you

Ordered by likelihood, not severity.

## R-1 · Extractor breakage — not if, when

YouTube changes something and downloads stop. It will happen during the build.

**Mitigation:** pin yt-dlp but update weekly. Run a nightly canary that downloads
one known-good URL from each source and surface it at `/health/extractors`. You
want to hear about breakage from a cron job, not from a user standing at a gate.

## R-2 · Silent OPFS eviction on iOS

The library renders, the file is gone, playback fails with an opaque error.

**Mitigation:** background verification sweep on catalogue load; `missing` state;
one-tap re-download. Never let a play attempt be the thing that discovers the
file is absent. `02-offline-playback.md` FM-6.

**CRITICAL — confirmed on real hardware, worse than the model above (2026-08-23).**
During v0.1 device testing (iPhone, added to home screen, `navigator.storage
.persist()` had reported `granted`), the app was closed from RAM and the phone
was used normally for a few hours — the one thing running was Instagram Reels,
which is known to cache large amounts of video. On reopening PWA-YT: the entire
origin's storage was gone — not just OPFS media, but IndexedDB too (session
*and* catalogue), and "Persistent storage" now read **DENIED**. No low-storage
warning, no reboot, no OS update, ~41 GB free reported shortly before.

This is not the LRU-one-file-at-a-time eviction R-2 was written to describe —
it's a full-origin wipe, and it happened in hours, not the 7-day non-interaction
window most iOS storage-eviction writeups describe (that specific ITP timer
resets on home-screen-app use and isn't what fired here). Per WebKit's own
"[Updates to Storage Policy](https://webkit.org/blog/14403/updates-to-storage-policy/)"
post, eviction is independently triggered by "the system is under storage
pressure" — a separate condition from the 7-day timer — and a `persist()` grant
only makes an origin *less likely* to be picked, via heuristics, not exempt.
**`navigator.storage.persist()` returning `true` is not the durable guarantee
its name implies on iOS Safari.**

Mitigated so far, neither of which fixes the underlying platform behavior:
- `App.svelte` now calls `persist()` on every boot, not only after a download,
  so the app at least keeps re-asking rather than silently staying unprotected
  once revoked.
- A fresh login now re-triggers `reconcile()` (full `/sync` pull), so losing
  local storage costs a re-download of media, not the appearance of a wiped
  account — the catalogue metadata comes back immediately since the server
  never lost it (D-002).

**Not yet answered, and load-bearing for whether the current design holds:**
is this reliably reproducible (same conditions, same result), what specifically
triggers it (another app's storage/video-cache pressure vs. elapsed backgrounded
time vs. something else), and does it also happen mid-flight with no network to
re-download from — which would be the actual failure the whole project is built
to avoid. Needs a controlled repro (grant persist, download, force-quit, run a
video-heavy app for a set interval, reopen and check) before concluding whether
this is "an inconvenience" (D-002's framing) or something that requires
revisiting the architecture per `CLAUDE.md`.

## R-3 · Partial files that look complete

A track plays for forty seconds and stops. On a plane. This is the bug that
destroys trust in the app rather than merely annoying someone.

**Mitigation:** `.part` naming, size **and** SHA-256 verification, rename last,
catalogue row written last, orphan sweep on startup. FM-4.

## R-4 · A network dependency creeping into the boot path

Someone adds an analytics SDK, a remote feature flag, a font `<link>`, or an
`await` on a sync call. Everything works in development. The app is dead at
35,000 feet.

**Mitigation:** build-time grep for `https://` in the bundle; the offline test
protocol before every release; a lint rule against bare `fetch()` without
`AbortSignal.timeout`.

## R-5 · Playlists of 500 items

Someone will paste one. Resolve takes minutes and the byte estimate is alarming.

**Mitigation:** stream plan entries as they resolve, show a running total, make
the estimate impossible to miss, and cap the number of entries a single import
can commit.

## R-6 · Scratch disk exhaustion

One 4K video job consumes gigabytes and the tmpfs fills, failing every other job.

**Mitigation:** cap the tmpfs, cap per-job estimated output, and **reject jobs
above the cap up front** rather than failing them at 90%.

## R-7 · SQLite write contention

Two workers, a long transaction, and `SQLITE_BUSY` under load.

**Mitigation:** WAL, `busy_timeout`, one writer connection behind an
`asyncio.Lock`, and never hold a connection across a download or transcode.
Debounce progress writes to at most one per second per job.

## R-8 · Cross-origin isolation is contagious

Turning on COEP for `ffmpeg.wasm` affects the entire origin, including every
third-party image and embed.

**Mitigation:** use `credentialless`, not `require-corp`, and test against the
whole app rather than just the transcode screen. This is why client transcode is
deferred to v1.0 — it is a whole-origin change dressed up as a feature.

## R-9 · Progress that lies

yt-dlp progress for HLS and SABR sources is unreliable and will sit at 97%.

**Mitigation:** an honest indeterminate state beats a fake bar. Show stage names
("fetching", "converting") when byte counts are not trustworthy.

## R-10 · Shared IP reputation

Multi-user means yt-dlp reaches YouTube from one IP for everyone. Rate limiting
finds you sooner than expected.

**Mitigation:** per-user concurrency caps and byte budgets from v0.4, exponential
backoff with jitter, and a circuit breaker that pauses the queue on repeated
429s rather than hammering through them.

## R-11 · Duplicate identity across sources

The same song on YouTube and SoundCloud is two `source_key`s and two files.

**Mitigation:** this is correct behaviour — surface it as a "you may already have
this" hint at resolve time rather than silently deduping or silently duplicating.

## R-12 · Scope creep toward being a service

Open signup changes this from a tool you and people you know use into something
with a very different risk profile, and the blocking problems there are not
technical ones.

**Mitigation:** invite-only registration is in the schema from v0.4. Keep it.
