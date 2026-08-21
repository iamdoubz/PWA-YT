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
