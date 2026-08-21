# Build plan

Phases are ordered so the riskiest assumption is tested first and each phase is
independently useful. **Do not start a phase before the previous phase's
acceptance criteria pass on real hardware.**

---

## v0.1 — Prove the storage assumption

Deliberately ugly, deliberately narrow. One hardcoded user, one text input, one
play button. This phase exists to answer one question:

> *Does a downloaded file survive on a real iPhone home-screen PWA, and play in
> airplane mode, a week later, with the device low on free space?*

If the answer is no, the whole design needs revisiting, and it is enormously
cheaper to learn that in week one than in month three.

**Scope**

- FastAPI + SQLite, no auth, single hardcoded user
- `POST /resolve` and `POST /items` for a single YouTube URL, AAC only
- yt-dlp (library) + ffmpeg → artifact → signed URL
- PWA shell with the full precache manifest and self-hosted fonts
- Download worker: `createSyncAccessHandle()`, `.part` → verify → rename
- One `<audio>` element, object URL from OPFS, MediaSession wired
- Installable, with the home-screen install prompt

**Acceptance criteria**

1. Assertions 1–8 of the offline test protocol pass on a physical iPhone
2. Assertion 12 passes: zero successful network requests while offline
3. `navigator.storage.persist()` returns `true` and is displayed
4. Killing the server entirely does not affect playback of downloaded media
5. Assertion 15 (the 7-day soak) is scheduled and its result recorded here

---

## v0.2 — Library and catalogue

**Scope**

- IndexedDB catalogue, item list, delete, re-download
- Storage meter and the **offline readiness panel** (`02-offline-playback.md` §6)
- Lazy background OPFS verification sweep; `missing` state and recovery
- SoundCloud extractor
- Format profiles: AAC/MP3, bitrate, artwork on/off
- Job queue with SSE progress, retry, TTL reaper
- Nightly extractor canary + `/health/extractors`

**Acceptance criteria**

1. A manually deleted OPFS file is detected on next load and the item degrades
   to "not downloaded" without an error dialog
2. Re-download from a `missing` item restores playback
3. Fifty queued items complete without the UI blocking
4. Killing the server mid-download leaves no `.part` file masquerading as complete

---

## v0.3 — Playlists and offline mutation

**Scope**

- Resolve-then-confirm playlist import with per-entry deselection and a running
  size estimate
- Local playlists, fractional-index reordering
- Full offline mutation with the outbox; replay on reconnect
- Queue playback, next/previous, prefetch of the next track's object URL
- Lock-screen controls incl. `seekto` and `setPositionState`

**Acceptance criteria**

1. Assertions 9–14 of the offline test protocol pass
2. A 400-entry playlist resolves with entries streaming in, and the total size
   estimate is visible before commit
3. Reordering a 200-track playlist offline produces one outbox row per move, and
   replays correctly after four hours offline

---

## v0.4 — Accounts

**Scope**

- Passkeys (WebAuthn) + magic-link fallback, invite-code registration
- Per-user job queues, concurrency caps, daily byte budgets, usage ledger
- Multi-device sync with tombstones and last-write-wins
- Per-user encrypted cookie jar for private content
- Backoff, jitter, and a circuit breaker on repeated upstream 429s

**Acceptance criteria**

1. Two users cannot see or affect each other's items, jobs, scratch, or cookies
2. Session expiry while offline degrades to read-only mode and does **not** clear
   local media (`02-offline-playback.md` FM-2)
3. One user saturating their queue does not delay another user's jobs
4. Same account on two devices converges after both make offline changes

---

## v1.0 — Video and optional client transcode

**Scope**

- `keep_video` with original-stream muxing; video playback view
- Optional `ffmpeg.wasm` transcode path behind cross-origin isolation
  (`Cross-Origin-Embedder-Policy: credentialless`, not `require-corp`)
- Client-transcode as a **per-user setting**, not a per-download toggle
- Backup job: nightly `VACUUM INTO`

**Acceptance criteria**

1. Enabling COEP does not break artwork or any other part of the app
2. Client-transcode produces a byte-comparable result to the server path for the
   same profile
3. The full offline test protocol still passes with video items

**Explicitly deferred beyond v1.0:** sharing, recommendations, social features,
a public signup flow, anything that turns this into a service rather than a tool.

---

## Cross-cutting, from day one

- The offline test protocol runs before every release tag
- No CDN references in the shell — add a build-time check that greps the bundle
- Conventional commits, conservative version bumps with stated rationale
- Every phase ends with the offline readiness panel still telling the truth
