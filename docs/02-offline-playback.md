# Offline playback — normative specification

**This is the most important document in the repository.** The rest of the
system exists to make what follows true. Where this document conflicts with any
other, this document wins.

---

## 1. Definition of done

> With the device in **airplane mode, wifi off, bluetooth off**, cold-booting the
> installed PWA from the home screen with the app not resident in memory, the
> user can: open the app, see their full library rendered in under two seconds,
> pick any downloaded track or playlist, press play, hear audio within two
> seconds, seek within the track, lock the screen, and continue listening with
> working lock-screen controls for the full duration of the item — while **no
> network request succeeds at any point**.

Every clause is load-bearing. "Cold-booting" excludes the case where a warm
in-memory app hides a broken cache. "Wifi off, bluetooth off" excludes the case
where airplane mode left wifi enabled and something quietly reached the network.
"No network request succeeds" is stronger than "the app doesn't crash" — the app
must not be *depending* on requests that happen to be failing gracefully.

---

## 2. The seven failure modes

These are ordered by how often they kill projects like this, not by severity.
Every one of them produces a working app on localhost and a dead app on a plane.

### FM-1 — The app shell is not fully cached

**Symptom:** white screen, or unstyled text, on offline cold boot.

- Precache **every** shell asset at build time: JS, CSS, fonts, icons, manifest,
  splash images. Use a generated precache manifest (`vite-plugin-pwa` /
  Workbox `injectManifest`), never a hand-maintained list — hand lists rot the
  first time someone adds a chunk.
- **Self-host fonts.** Subset them to the glyphs you use and ship woff2 in the
  bundle. A `<link>` to `fonts.googleapis.com` is a guaranteed offline failure,
  and its failure mode is invisible in development.
- **No CDN scripts, no remote images, no external stylesheets anywhere in the
  shell.** Not for analytics, not for icons, not for a "just this once" embed.
- Service worker `navigationFallback` must serve `index.html` for any navigation
  request, or deep links 404 offline.
- Verify by building, deploying, **stopping the server**, and hard-reloading.
  DevTools "Offline" is a weaker test than actually killing the origin.

### FM-2 — The boot path awaits the network

**Symptom:** infinite spinner, or a login screen, with a full library sitting in
storage three feet away.

This is the single most common killer and it is entirely self-inflicted.

- **The app must render a usable library with zero network calls.** Identity,
  session, and the whole catalogue come from IndexedDB.
- On boot: read session + catalogue from IndexedDB → first paint → *then*
  optionally kick off a background sync that is allowed to fail silently.
- **Never `await` a network call before first paint. Never gate rendering on a
  sync result.** If you find yourself writing `await syncCatalogue()` in a
  startup path, that is the bug.
- Every `fetch()` in the app carries `AbortSignal.timeout()`. Airplane mode
  usually fails fast, but captive portals and cabin wifi produce *hangs*, which
  are worse than failures because nothing ever rejects.
- **Token expiry must not lock the user out.** Check the locally stored
  `expires_at`. An expired session offline degrades to read-only offline mode
  with a "sign in when you're back online" banner. It does **not** log out, and
  logging out must **never** clear OPFS media or the catalogue.

```js
// The shape of a correct boot
const session   = await idb.get('meta', 'session');   // local only
const catalogue = await idb.getAll('items');          // local only
render(catalogue, session);                           // first paint, no network
queueMicrotask(() => syncInBackground().catch(() => {})); // optional, failable
```

### FM-3 — The write path uses an API the browser does not have

**Symptom:** downloads appear to succeed and nothing is on disk. iOS only.

- **Primary write path: a dedicated Web Worker using `createSyncAccessHandle()`.**
  Baseline widely available since March 2023; works on older iOS.
- `createWritable()` / `FileSystemWritableFileStream` only reached baseline
  availability in **September 2025**. Treat it as a nicety for new browsers, not
  as the foundation. Prefer the worker path everywhere so there is one code path
  to test rather than two.
- Stream, never buffer: `fetch` → `response.body.getReader()` → post chunks to
  the worker → `accessHandle.write(chunk, { at: offset })` → `flush()` → `close()`.
  A 200 MB file must never exist in memory as one buffer, especially on a phone.

### FM-4 — Partial files masquerade as complete

**Symptom:** a track plays for forty seconds and stops. On a plane.

This deserves its own failure mode because it is the most damaging one: it
destroys trust in the app rather than merely inconveniencing the user.

- Always write to `audio.m4a.part`.
- Verify **both** byte length and SHA-256 against the values the job reported.
- Only then rename to `audio.m4a` and write the `local_media` row with
  `state: 'present'`.
- On startup, sweep for orphaned `.part` files and delete them.
- Never write the catalogue row before the rename. The IndexedDB record claiming
  a file exists must be the *last* thing that happens.

### FM-5 — The playback source is constructed wrongly

**Symptom:** audio plays but will not seek; or playback breaks after the first
track when the screen is locked.

```js
const handle = await dir.getFileHandle('audio.m4a');
const file   = await handle.getFile();
audioEl.src  = URL.createObjectURL(file);   // revoke the previous one first
```

- Do **not** use Media Source Extensions. Unnecessary for local files.
- Do **not** use data URLs. They put the whole file in memory.
- Do **not** route media through the service worker. OPFS media never touches
  the network stack, and the SW should never learn it exists.
- **Revoke the previous object URL** on every track change or you leak the file.
- Keep **one long-lived `<audio>` element** for the app's lifetime and swap
  `src`. Creating a fresh element per track loses the iOS user-gesture audio
  unlock, and playback silently stops working after the first track once the app
  is backgrounded.
- Prefetch the *next* track's `File` and object URL while the current one plays,
  so transitions do not stutter.

### FM-6 — Storage was evicted

**Symptom:** the library renders, the file is gone.

WebKit evicts by least-recently-used under storage pressure and after prolonged
non-interaction. You cannot fully prevent this. Design for it.

- Call `navigator.storage.persist()` after the first successful download. Store
  and display the actual result — a silent `false` is how libraries disappear.
- **Verify lazily, not on the critical path.** First paint uses the catalogue's
  claimed state. A worker then stats each file in batches and downgrades any
  mismatch to `state: 'missing'`. Do not block boot on 2,000 stat calls.
- A `missing` item stays visible in the library, marked "not downloaded", with a
  one-tap re-download. **Local loss must be an inconvenience, never data loss.**
- Because `library_items` carries `source_key` and `format_profile`, every item
  is re-derivable. This is why N4 in the handoff brief is a non-negotiable.

### FM-7 — Something in the render path needs the network

**Symptom:** library renders but every tile is a broken image.

- Artwork in lists comes from IndexedDB blobs. **Never** a remote `thumb_url`.
- `thumb_url` is stored for re-download purposes only; it must not appear in an
  `<img src>` anywhere.
- No analytics beacon, no error-reporting SDK, no remote feature flags, no font
  swap on the critical path.
- Audit rule: grep the built bundle for `https://`. Every hit must be either a
  string used only in a download request, or a comment.

---

## 3. Lock-screen and background audio

Offline playback that stops when the screen locks is not offline playback.

```js
navigator.mediaSession.metadata = new MediaMetadata({
  title: item.title,
  artist: item.uploader,
  album: playlist?.name ?? 'Library',
  artwork: [{ src: localArtObjectUrl, sizes: '512x512', type: 'image/jpeg' }],
});
```

- Artwork **must** be a local object URL. A remote artwork URL blanks the lock
  screen offline.
- Register `play`, `pause`, `previoustrack`, `nexttrack`, **and `seekto`**.
  Without `seekto` the lock-screen scrubber does nothing.
- Call `navigator.mediaSession.setPositionState()` on seek and periodically, or
  the scrubber sits at zero.
- iOS requires a user gesture to start audio. Once started, background audio in
  a home-screen PWA works — but only with the single-element rule from FM-5.

---

## 4. Offline mutation

The user will reorganise their library on the plane. That must work.

- Playlist create, rename, reorder, add, remove, and item delete all work fully
  offline, writing to IndexedDB immediately.
- Each mutation also appends to an `outbox` store with an idempotency key.
- On reconnect, the outbox replays in order. Server applies last-write-wins on
  `updated_at`. See `03-data-model.md`.
- Fractional indexing for playlist position means an offline reorder writes one
  row, not a renumbered tail — which matters a lot when it has to be replayed
  hours later against a server that may have changed.
- Playback position is written locally on pause and on track end, and synced
  opportunistically. Losing it is acceptable; blocking on it is not.

---

## 5. The offline test protocol

Run this before any release. The manual steps require a real device — the iOS
Simulator's storage behaviour differs from hardware and will pass tests that
hardware fails.

**Setup**

1. Build, deploy, install to the iOS home screen. Also install on Android Chrome
   and desktop Chrome.
2. Download five items: a short audio track, a one-hour audio item, one with
   `keep_video` on, one whose artwork came from frame extraction, and one MP3.
3. Force-quit the app (swipe away, not just background).
4. Enable airplane mode. **Then separately turn off wifi and bluetooth** —
   airplane mode alone can leave wifi enabled.

**Assertions, in order**

| # | Assert |
|---|---|
| 1 | Cold boot from home-screen icon renders the library in < 2s, no unresolved spinner |
| 2 | Artwork is visible in the list view |
| 3 | Play a track — audio within 2s |
| 4 | Seek to 80% — plays from there, no stall |
| 5 | Lock the screen — audio continues; controls, title, and artwork present |
| 6 | Lock-screen scrub works; next-track from lock screen works |
| 7 | Play the one-hour item to its end (or seek to 99%) — no truncation |
| 8 | The video item plays with picture |
| 9 | Create a playlist, reorder it, add a track — all offline |
| 10 | Force-quit and cold boot again — those offline changes persisted |
| 11 | Delete an item offline — still gone after force-quit |
| 12 | Throughout: no successful network request (verify in a proxy log or via a `fetch` counter in a debug build) |

**Then reconnect**

| # | Assert |
|---|---|
| 13 | Outbox replays; offline playlist changes appear server-side |
| 14 | Nothing created offline was lost or duplicated |

**The soak test (the one everyone skips)**

| # | Assert |
|---|---|
| 15 | Leave the device seven days without opening the app, with free space low. Reopen **offline**. Media either survives, or every affected item degrades cleanly to "not downloaded" with the library intact. |

Assertion 15 determines whether the app is trustworthy. Schedule it; it cannot
be run on demand.

---

## 6. Offline readiness panel

Ship a user-facing panel in settings — not a debug tool, a real feature. Someone
about to board a plane wants to know it will work:

- App shell cached: ✓ / ✗ (version)
- Library items: N total, M downloaded, K verified
- Storage used: X GB of Y GB available
- Persistent storage granted: yes / no
- Last successful sync: timestamp
- **"Check my library" button** — runs the full OPFS verification sweep and
  reports anything missing while there is still network to re-download it

That last button is the single highest-value piece of UI in the app.
