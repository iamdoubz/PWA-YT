# Handoff brief

**Project:** PWA-YT — an offline-first media PWA
**Status:** v0.0–v0.2 built; **device gate never run** — see `09-status.md`
**Date:** 2026-08-21 (status updated 2026-08-22)
**Audience:** the engineer or agent session picking this up

> **If you are resuming work, read [`09-status.md`](09-status.md) first.** This
> brief still describes the design correctly, but it was written before any code
> existed. `09-status.md` says what is actually built, what is not, and which
> acceptance gate is still open.

---

## 1. What we are building

A logged-in progressive web app that downloads audio (and optionally video) from
YouTube and SoundCloud, stores it **on the user's device**, and plays it back
with **no network connection at all**.

The user adds a link or a playlist while online. A server fetches and transcodes
the media, hands it to the browser once, and forgets it. From that moment the
media belongs to the device. On a plane, in a tunnel, in airplane mode with wifi
and bluetooth off, the app opens and plays.

## 2. The thesis

> **Offline playback is not a feature of this app. It is the app.**

Everything else — the downloader, the accounts, the playlists, the format
options — is scaffolding around that one property. A version of this app that
downloads beautifully and then fails to play on a plane is a total failure. A
version that plays reliably offline and has an ugly download UI is a success
with a to-do list.

Read `02-offline-playback.md` before you write any code. Its "Definition of
done" is the acceptance test for the entire project, and the seven failure modes
it lists are the seven ways projects like this actually die.

## 3. Definition of done (repeated here because it is the point)

> With the device in **airplane mode, wifi off, bluetooth off**, cold-booting the
> installed PWA from the home screen with the app not in memory, the user can:
> open the app, see their full library rendered in under two seconds, pick any
> downloaded track or playlist, press play, hear audio within two seconds, seek
> within the track, lock the screen, and keep listening with working lock-screen
> controls for the full duration — while **no network request succeeds at any
> point**.

If that sentence is true, the project works. If any clause is false, it doesn't.

## 4. Shape of the system

```
  ┌─────────────────────────────────────────────┐
  │  DEVICE  (the only durable holder of media) │
  │  ┌────────────┐   ┌──────────────────────┐  │
  │  │ IndexedDB  │   │ OPFS                 │  │
  │  │ catalogue  │   │ /media/{itemId}/...  │  │
  │  │ playlists  │   │ audio · video · art  │  │
  │  │ outbox     │   └──────────────────────┘  │
  │  └────────────┘                             │
  └───────────────┬─────────────────────────────┘
                  │ HTTPS, only when online
  ┌───────────────▼─────────────────────────────┐
  │  SERVER  (stateless transformer)            │
  │  FastAPI + in-process worker                │
  │  SQLite (metadata only)  ·  tmpfs scratch   │
  │  yt-dlp (library) → ffmpeg → signed artifact│
  │  + bgutil POT provider sidecar              │
  └─────────────────────────────────────────────┘
```

Seven-stage pipeline, detailed in `01-architecture.md`:

1. **Submit** — client posts URL + format profile
2. **Resolve** — `extract_info(download=False)`, returns a *plan* (no download yet)
3. **Confirm** — user deselects unwanted playlist entries
4. **Fetch** — yt-dlp into per-job tmpfs scratch
5. **Transform** — ffmpeg: encode, tag, embed artwork, optional video mux
6. **Pull & commit** — client streams into OPFS, verifies, then `DELETE`s the artifact
7. **Reap** — TTL sweep; scratch is gone either way

Stage 6 is the contract that makes the server stateless. Stage 2 is what makes
importing a 400-track playlist survivable.

## 5. Non-negotiables

| # | Constraint | Why |
|---|---|---|
| N1 | The app boots to a usable library with **zero network calls** | Otherwise airplane mode shows a login wall. This is failure mode #2 and the most commonly missed. |
| N2 | No CDN references anywhere in the app shell | A Google Fonts link is a white screen at 35,000 feet. Self-host and precache. |
| N3 | OPFS writes go through a dedicated Web Worker with `createSyncAccessHandle()` | `createWritable()` only reached baseline availability in Sept 2025; the worker path has been widely available since March 2023 and works on older iOS. |
| N4 | Every item is re-derivable from `source_key` + `format_profile` | WebKit evicts storage. Design for eviction, don't pretend to prevent it. Local loss must be an inconvenience, never data loss. |
| N5 | The server never retains media | Scratch is tmpfs, artifacts are signed/single-use/short-TTL, client acknowledges collection. |
| N6 | Partial downloads can never masquerade as complete | Write to `.part`, verify size + SHA-256, then rename. A file that plays for 40 seconds and stops — on a plane — is the worst bug this app can ship. |
| N7 | Auth expiry degrades to offline-read-only, never to logout | Logging out must never clear local media. |

## 6. What changed from the first design pass

- **Postgres → SQLite.** See `08-decisions.md` D-004. This simplifies more than
  it costs: the entire server becomes one file plus one scratch directory, and
  the job queue no longer needs Redis. It does impose a single-writer discipline
  — read the concurrency section of `03-data-model.md` before writing DB code.
- Offline playback promoted from a section to **the organising principle**, with
  its own normative spec and test protocol.

## 7. Where to start

Build `06-build-plan.md` **v0.1** and nothing else. It is deliberately ugly and
deliberately narrow: one hardcoded user, one pasted URL, one file, one play
button. Its entire purpose is to answer the question that invalidates everything
else if the answer is bad —

> *Does a downloaded file actually survive on a real iPhone home-screen PWA, and
> actually play in airplane mode, a week later, with the device low on space?*

Prove that first. Everything in phases 0.2 through 1.0 assumes the answer is yes.

## 8. Document map

| File | What it is |
|---|---|
| `00-HANDOFF.md` | This brief. Start here. |
| `01-architecture.md` | Components, pipeline, deployment, concurrency model |
| `02-offline-playback.md` | **Normative spec for the core property.** Failure modes + test protocol |
| `03-data-model.md` | SQLite DDL, IndexedDB stores, sync protocol |
| `04-api.md` | Endpoint contracts |
| `05-formats.md` | Format profiles and exact ffmpeg invocations |
| `06-build-plan.md` | Phases with acceptance criteria |
| `07-risks.md` | What will bite you, with mitigations |
| `08-decisions.md` | ADR-style log of decisions already made and why |
| `09-status.md` | **Built vs not built.** Start here when resuming. |
