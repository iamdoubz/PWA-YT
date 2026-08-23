# PWA-YT

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

**v0.0–v0.2 built. Not yet verified on a physical device.**

The pipeline runs end to end for YouTube and SoundCloud: paste a link, see what
it is and what it will cost, download it, and play it back with both the API and
the web server switched off. Verified on desktop Chrome.

The device test protocol in `docs/02-offline-playback.md` — the one that actually
decides whether this works — has **not** been run, and the seven-day soak has not
been started. v0.1's acceptance criteria are all device criteria and all remain
open.

**Resuming? Read [`docs/09-status.md`](docs/09-status.md) first** — built vs not
built, known traps, and where to pick up. Then `docs/00-HANDOFF.md` and
`CLAUDE.md`.

## Running it

Two processes. The app proxies `/api` to the server, so everything is one origin
— no CORS, and a tunnel in front of it works without configuration.

```bash
# terminal 1 — the stateless transformer
cd server && uv run uvicorn main:app --port 8000

# terminal 2 — the app
cd app && npm install && npm run build && npm run preview -- --port 4173
```

Then open <http://localhost:4173>. Requires `ffmpeg` on `PATH`.

```bash
cd server && uv run python test_server.py   # server self-check
cd app && npm run check:no-cdn              # fails if the shell gained a CDN reference
```

### Docker

Two containers, one volume (`docs/01-architecture.md`), `linux/amd64` only.

```bash
cp .env.example .env   # then edit it
docker compose up -d --build
```

The app container serves the built PWA on `$APP_PORT` (default `8080`) and
proxies `/api` to the server container, same as the Vite dev proxy — one
origin, no CORS. Images also publish to `ghcr.io/<owner>/pwa-yt-{server,app}`
via `.github/workflows/docker.yml` on every push to `main` and on version tags.

### Inviting a user

Registration is invite-only by design (`docs/08-decisions.md` D-008) — there
is deliberately no signup endpoint. Minting a code is an operator action:

```bash
# Docker
docker compose exec server uv run python scripts/create_invite.py

# bare processes
cd server && uv run python scripts/create_invite.py
```

Prints a short one-time code. In the app, click **"Have an invite code?
Register"**, enter it plus an email and display name, then create the account
with a passkey — `PWA_YT_RP_ID`/`PWA_YT_ORIGINS` must already match the
hostname you're registering from (see `.env.example`) or the passkey ceremony
fails. Every login after that is just "Sign in with a passkey," no code
needed. Passing an existing user's id as an argument attributes the invite to
them instead of leaving it anonymous — see the script's docstring.

To test it the way it is meant to be used, put it on a phone over real HTTPS
(`cloudflared tunnel --url http://localhost:4173`), add it to the home screen,
then follow the protocol in `docs/02-offline-playback.md`. A localhost check with
DevTools set to offline lies about iOS.

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
| [`docs/09-status.md`](docs/09-status.md) | **Built vs not built.** Start here when resuming |

## Stack

| Layer | Choice |
|---|---|
| Frontend | Svelte 5 · Vite · `vite-plugin-pwa` |
| Client storage | OPFS (media) · IndexedDB (catalogue) |
| Backend | Python 3.11+ · FastAPI · `uv` |
| Database | SQLite (WAL) — no Postgres, no Redis |
| Media | yt-dlp as a library · ffmpeg · `bgutil-ytdlp-pot-provider` |
| Auth | WebAuthn passkeys, magic-link fallback |

## Next task

Put v0.1 on a physical iPhone and run the offline test protocol. Everything in
v0.2 onward assumes a yes to the one question that invalidates the design if the
answer is bad:

> *Does a downloaded file survive on a real iPhone home-screen PWA, and play in
> airplane mode, a week later, with the device low on free space?*

Assertion 15 — the seven-day soak — is a wall clock. Start it early.

## Personal use

This is a tool for personal, offline access to media. Downloading from these
platforms may conflict with their terms of service, and what is permissible
varies by content and jurisdiction. Registration is invite-only by design; keep
it that way.
