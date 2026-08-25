# PWA-YT

[![CI](https://github.com/iamdoubz/PWA-YT/actions/workflows/ci.yml/badge.svg)](https://github.com/iamdoubz/PWA-YT/actions/workflows/ci.yml)
[![Docker](https://github.com/iamdoubz/PWA-YT/actions/workflows/docker.yml/badge.svg)](https://github.com/iamdoubz/PWA-YT/actions/workflows/docker.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

An offline-first progressive web app that downloads audio and video from YouTube,
SoundCloud, Bandcamp, Mixcloud, and Vimeo, stores it **on your device**, and
plays it back with no network connection at all.

> **Offline playback is not a feature of this app. It is the app.**

Add a link or a playlist while online. A server fetches and transcodes the media,
hands it to the browser once, and forgets it. From that moment the media belongs
to the device — on a plane, in a tunnel, in airplane mode, the app opens and
plays.

---

## Install

### Docker Compose (recommended)

```bash
cp .env.example .env   # then edit it
docker compose up -d --build
```

Open <http://localhost:8080> (the port `.env`'s `APP_PORT` sets).

### Docker, without Compose

```bash
docker network create pwa-yt
docker volume create pwa-yt-db

docker build -t pwa-yt-server ./server
docker build -t pwa-yt-app ./app

docker run -d --name server --network pwa-yt --restart unless-stopped \
  -e PWA_YT_RP_ID=localhost \
  -v pwa-yt-db:/data \
  --tmpfs /app/scratch \
  pwa-yt-server

docker run -d --name app --network pwa-yt --restart unless-stopped \
  -p 8080:80 \
  pwa-yt-app
```

Open <http://localhost:8080>. The app container's nginx expects the server
container to be reachable as `server` — keep that container name if you change
anything else.

### Manual

Two processes. Requires `ffmpeg` on `PATH`.

```bash
# terminal 1 — the server
cd server && uv run uvicorn main:app --port 8000

# terminal 2 — the app
cd app && npm install && npm run build && npm run preview -- --port 4173
```

Open <http://localhost:4173> — use `localhost`, not `127.0.0.1`; WebAuthn
(passkeys) rejects an IP address as a valid domain.

### First login

Registration is invite-only — there's no signup endpoint. Mint a code:

```bash
docker compose exec server uv run python scripts/create_invite.py   # Compose
docker exec server uv run python scripts/create_invite.py           # plain Docker
cd server && uv run python scripts/create_invite.py                 # manual
```

In the app, **"Have an invite code? Register"**, enter it plus an email and
display name, then create the account with a passkey. Every login after that
is just "Sign in with a passkey."

To use it the way it's meant to be used, put it on a phone over real HTTPS
(a `cloudflared` tunnel, or any reverse proxy), add it to the home screen, and
follow the offline test protocol in [`docs/02-offline-playback.md`](docs/02-offline-playback.md).

---

## What it does

- Log in with a passkey (invite-only, multi-user)
- Paste a link from YouTube, SoundCloud, Bandcamp, Mixcloud, or Vimeo (Vimeo needs cookies saved first — see Account) — a single track or a whole playlist
- Preview what a playlist contains and how much space it needs **before** committing
- Download as **AAC** (default) or **MP3**, with optional video and optional artwork
- Media persists in device storage — OPFS, not a server
- Build playlists from downloaded content, entirely offline
- Remove content and free space, with one-tap re-download

## More

- **Full documentation index:** [`docs/00-HANDOFF.md`](docs/00-HANDOFF.md)
- **Current build status, what's verified, what's next:** [`docs/09-status.md`](docs/09-status.md)
- **Architecture, stack, data model:** [`docs/01-architecture.md`](docs/01-architecture.md)
- **Prebuilt images:** [`pwa-yt-server`](https://github.com/iamdoubz/PWA-YT/pkgs/container/pwa-yt-server) · [`pwa-yt-app`](https://github.com/iamdoubz/PWA-YT/pkgs/container/pwa-yt-app) on GHCR
- **Issues / bugs:** [github.com/iamdoubz/PWA-YT/issues](https://github.com/iamdoubz/PWA-YT/issues)

## Personal use

This is a tool for personal, offline access to media. Downloading from these
platforms may conflict with their terms of service, and what is permissible
varies by content and jurisdiction. Registration is invite-only by design; keep
it that way.
