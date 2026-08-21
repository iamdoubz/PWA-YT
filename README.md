# PWA-YT  *(working codename: Tarmac)*

An offline-first progressive web app that downloads audio and video from YouTube
and SoundCloud, stores it **on your device**, and plays it back with no network
connection at all.

> **Offline playback is not a feature of this app. It is the app.**

Add a link or a playlist while online. A server fetches and transcodes the media,
hands it to the browser once, and forgets it. From that moment the media belongs
to the device. On a plane, in a tunnel, in airplane mode with wifi and bluetooth
off — the app opens and plays.

---

## Status

**Design complete. Nothing built yet.**

This repository currently contains a handoff specification only. If you are the
session picking this up, start with **[`docs/00-HANDOFF.md`](docs/00-HANDOFF.md)**,
then read `CLAUDE.md`.

---

## What it does

- Log in with a passkey (invite-only, multi-user)
- Paste a YouTube or SoundCloud link, or a whole playlist
- Preview what a playlist contains and how much space it needs **before** committing
- Download as **AAC** (default) or **MP3**, with optional video and optional artwork
- Media persists in device storage — OPFS, not a server
- Build playlists from downloaded content, entirely offline
- Remove content and free space, with one-tap re-download

## How it is built

```
DEVICE                                  SERVER
  IndexedDB   catalogue, playlists        FastAPI + SQLite (metadata only)
  OPFS        the media itself            yt-dlp → ffmpeg → signed artifact
  Web Worker  streaming writes            tmpfs scratch, deleted on collection
```

The server is a **stateless transformer**, never a media library. Media transits
it and is never retained. The client is the only durable holder — and every item
stays re-derivable from its source URL and format profile, so storage eviction is
an inconvenience rather than data loss.

## Documentation

| Document | What it covers |
|---|---|
| [`docs/00-HANDOFF.md`](docs/00-HANDOFF.md) | **Start here.** The brief, the thesis, the non-negotiables |
| [`docs/01-architecture.md`](docs/01-architecture.md) | Components, pipeline, deployment, concurrency |
| [`docs/02-offline-playback.md`](docs/02-offline-playback.md) | **Normative spec.** Failure modes and the device test protocol |
| [`docs/03-data-model.md`](docs/03-data-model.md) | SQLite DDL, IndexedDB stores, sync protocol |
| [`docs/04-api.md`](docs/04-api.md) | Endpoint contracts |
| [`docs/05-formats.md`](docs/05-formats.md) | Format profiles and exact ffmpeg invocations |
| [`docs/06-build-plan.md`](docs/06-build-plan.md) | Phases with acceptance criteria |
| [`docs/07-risks.md`](docs/07-risks.md) | What will bite you |
| [`docs/08-decisions.md`](docs/08-decisions.md) | Why things are the way they are |

## Stack

| Layer | Choice |
|---|---|
| Frontend | Svelte 5 · Vite · `vite-plugin-pwa` |
| Client storage | OPFS (media) · IndexedDB (catalogue) |
| Backend | Python 3.11+ · FastAPI · `uv` |
| Database | SQLite (WAL) — no Postgres, no Redis |
| Media | yt-dlp as a library · ffmpeg · `bgutil-ytdlp-pot-provider` |
| Auth | WebAuthn passkeys, magic-link fallback |

## First task

Build `v0.1` from [`docs/06-build-plan.md`](docs/06-build-plan.md) and nothing
else. It is deliberately ugly and deliberately narrow, and it exists to answer
the one question that invalidates everything else if the answer is bad:

> *Does a downloaded file survive on a real iPhone home-screen PWA, and play in
> airplane mode, a week later, with the device low on free space?*

## Personal use

This is a tool for personal, offline access to media. Downloading from these
platforms may conflict with their terms of service, and what is permissible
varies by content and jurisdiction. Registration is invite-only by design; keep
it that way.
