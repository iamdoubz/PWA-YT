# CLAUDE.md — working agreement for this repository

You are picking up a project that has been designed but not built. Read
`docs/00-HANDOFF.md` first. It is the brief. Everything else in `docs/` is
reference material it points at.

## The one thing that matters

This app exists so that a person on a plane, with no network of any kind, can
open it and listen to music they downloaded earlier. **Offline playback is not a
feature of this app. It is the app.**

Every architectural choice is judged against that. If a change makes the app
faster, prettier, or easier to deploy but adds a network dependency to the boot
or playback path, the change is wrong. `docs/02-offline-playback.md` is the
normative spec for this, and its "Definition of done" section is the acceptance
test for the whole project.

## Decisions already made — do not relitigate without saying so

These were settled during design. `docs/08-decisions.md` records why. If you
believe one is wrong, say so explicitly and argue it; do not silently deviate.

- yt-dlp runs **server-side**. It cannot run in the browser. The reasoning is in
  `docs/08-decisions.md` D-001 and it is not a close call.
- The server is a **stateless transformer**, never a media library. Media lives
  on client devices only.
- **SQLite**, not Postgres. One file, WAL mode, single writer.
- **OPFS** for media blobs, **IndexedDB** for the catalogue.
- The OPFS write path is a **dedicated Web Worker using `createSyncAccessHandle()`**,
  not `createWritable()`. This is a compatibility requirement, not a preference.
- Audio defaults to **AAC**; MP3 optional. Video off by default. Artwork on by default.
- Auth is **passkeys**, multi-user, invite-only registration.
- Transcoding is **server-side by default**, client-side `ffmpeg.wasm` optional
  and deferred to v1.0.

## How to work here

- Build in the phase order in `docs/06-build-plan.md`. Each phase has explicit
  acceptance criteria. Do not start a phase before the previous one's criteria pass.
- **v0.1 exists to falsify the riskiest assumption** (that media survives on a real
  iPhone). Do not build UI polish before that is proven. If it fails, the whole
  design needs revisiting and it is much cheaper to learn that in week one.
- Prefer boring, well-supported technology. This app has to keep working when
  nobody is maintaining it for six months.
- Write the offline test protocol (`docs/02-offline-playback.md`) into CI-adjacent
  form as early as you can. The manual parts still need a real device.

## Conventions

- Python 3.11+, FastAPI, `uv` for dependency management.
- Frontend: Svelte 5 + Vite + `vite-plugin-pwa`. If you swap frameworks, note it
  in `docs/08-decisions.md` — but read the self-hosted-fonts note in
  `docs/02-offline-playback.md` first, it constrains the build regardless.
- No CDN references anywhere in the app shell. Ever. Fonts, icons, and scripts
  are self-hosted and precached, or they are not used.
- Timestamps are ISO 8601 UTC strings (`YYYY-MM-DDTHH:MM:SS.sssZ`) everywhere,
  including in SQLite. IDs are UUIDv7 as TEXT.
- Conventional commits. Version bumps are conservative and explained.
- The version number is kept in three places and all three move together:
  `server/pyproject.toml` (`[project].version`), the FastAPI `app.version` in
  `server/main.py` (what `/health` reports, and what the Account sheet's
  footer displays), and `app/package.json` (`.version`). There is no build
  step that derives one from another — bump all three by hand in the same
  commit.

## Verifying your work

Screenshots and file diffs beat assertions. For anything touching offline
behaviour, the only acceptable evidence is the device test protocol in
`docs/02-offline-playback.md` § "The offline test protocol" — not a unit test,
not a localhost check with DevTools set to offline (that lies about iOS).
