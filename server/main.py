"""FastAPI surface plus the in-process job runner.

Endpoints are sync `def`, so FastAPI runs them in its threadpool and blocking
SQLite calls never touch the event loop.
"""

import base64
import json
import multiprocessing
import os
import random
import secrets
import shutil
import threading
import time
from concurrent import futures
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FutureTimeout
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

import auth
import db
import extract
import pipeline

RESOLVE_TIMEOUT_S = 60
JOB_TIMEOUT_S = 30 * 60
ARTIFACT_TTL_S = 60 * 60
RUNNERS = 2
SSE_MAX_S = 5 * 60
CANARY_EVERY_S = 6 * 60 * 60

# One known-good URL per extractor. When one of these starts failing, the
# extractor has broken upstream — which is a when, not an if. This is the early
# warning, and it is why /health/extractors exists.
# Metadata resolve only, no download — four requests a day per extractor.
# Override either if the track goes away, since a deleted canary reads as an
# extractor failure and that is a false alarm you only debug once.
CANARY_URLS = {
    "youtube": os.environ.get(
        "PWA_YT_CANARY_YOUTUBE", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    ),
    "soundcloud": os.environ.get(
        "PWA_YT_CANARY_SOUNDCLOUD", "https://soundcloud.com/bawwww/2-0-2x-1"
    ),
}

_canary: dict[str, dict] = {}

# Deliberately not S3. In deployment this is a size-capped tmpfs mount, so a
# crashed process cannot leave media on a real disk.
SCRATCH = Path(os.environ.get("PWA_YT_SCRATCH", Path(__file__).parent / "scratch"))
ORIGINS = os.environ.get("PWA_YT_ORIGINS", "*").split(",")

_resolve_pool: ProcessPoolExecutor | None = None
_job_pool: ProcessPoolExecutor | None = None
_stop = threading.Event()

# The per-user cookie jar (private/age-gated content) is encrypted at rest
# with this key. Set PWA_YT_COOKIE_KEY explicitly in any deployment that must
# survive a restart — an auto-generated key here is session-only, and cookies
# saved under a since-discarded key just decrypt as "not configured" rather
# than crashing anything (see _user_cookies below).
_COOKIE_KEY_ENV = "PWA_YT_COOKIE_KEY"
_cookie_key = os.environ.get(_COOKIE_KEY_ENV)
if not _cookie_key:
    _cookie_key = Fernet.generate_key().decode()
    print(
        f"[cookies] {_COOKIE_KEY_ENV} not set — generated a session-only key.\n"
        f"          Cookies saved now will stop decrypting after a restart unless\n"
        f"          you set {_COOKIE_KEY_ENV}={_cookie_key} and keep it.",
        flush=True,
    )
_fernet = Fernet(_cookie_key.encode())


class FormatProfile(BaseModel):
    """Stored per item, not only as a global default, so a re-download six
    months from now is exactly reproducible. See 05-formats.md."""

    audio_codec: Literal["aac", "mp3"] = "aac"
    audio_bitrate: Literal[128, 192, 256] = 192
    prefer_copy: bool = True
    keep_video: bool = False
    video_max_height: int = 1080
    save_artwork: bool = True
    artwork_source: Literal["thumbnail", "frame", "auto"] = "auto"


class ResolveRequest(BaseModel):
    url: str = Field(min_length=1)
    format_profile: FormatProfile = FormatProfile()


class ItemEntry(BaseModel):
    source_key: str = Field(min_length=3)
    format_profile: FormatProfile = FormatProfile()
    # Opaque fractional-index string, set only when this entry is also being
    # attached to a playlist. The server never generates or compares these.
    position: str | None = None


class ItemsRequest(BaseModel):
    entries: list[ItemEntry] = Field(min_length=1)
    playlist_id: str | None = None


class PlaylistCreate(BaseModel):
    id: str = Field(min_length=1)  # client-generated UUIDv7 — makes create idempotent
    name: str = Field(min_length=1)


class PlaylistRename(BaseModel):
    name: str = Field(min_length=1)


class PlaylistItemUpsert(BaseModel):
    item_id: str
    position: str


class PlaylistItemsPatch(BaseModel):
    """Either a full ordered replacement (many upserts) or a single-move patch
    (one upsert) — same shape either way, see 04-api.md."""

    upserts: list[PlaylistItemUpsert] = []
    removes: list[str] = []


class RegisterBeginRequest(BaseModel):
    invite_code: str = Field(min_length=1)
    email: str = Field(min_length=3)
    display_name: str = ""


class CeremonyFinishRequest(BaseModel):
    ceremony_id: str = Field(min_length=1)
    credential: dict


def error(status: int, code: str, message: str, **extra) -> JSONResponse:
    """Messages are written for the user, not the log. Say what happened and
    what to do about it."""
    return JSONResponse({"error": code, "message": message, **extra}, status_code=status)


_ERROR_CODES = {401: "unauthorized", 403: "forbidden", 404: "not_found", 422: "invalid_request"}


# ---------------------------------------------------------------- job runner


def _claim() -> dict | None:
    """One job, atomically, respecting the owner's concurrency cap.

    SQLite has no SKIP LOCKED, but BEGIN IMMEDIATE takes the write lock up front
    so two runners cannot claim the same row. The correlated subquery enforces
    per-user concurrency in the same statement that claims, so it cannot race.
    """
    with db.writing() as conn:
        row = conn.execute(
            """UPDATE jobs
                  SET state = 'fetching', attempt = attempt + 1, updated_at = ?
                WHERE id = (
                      SELECT j.id FROM jobs j JOIN users u ON u.id = j.user_id
                       WHERE j.state = 'queued'
                         AND u.disabled_at IS NULL
                         AND (SELECT COUNT(*) FROM jobs r
                               WHERE r.user_id = j.user_id
                                 AND r.state IN ('fetching','transforming')) < u.max_concurrent
                       ORDER BY j.created_at LIMIT 1)
             RETURNING *""",
            (db.now(),),
        ).fetchone()
    return dict(row) if row else None


def _record_usage(user_id: str, num_bytes: int) -> None:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with db.writing() as conn:
        conn.execute(
            """INSERT INTO usage_ledger (user_id, day, bytes) VALUES (?, ?, ?)
               ON CONFLICT(user_id, day) DO UPDATE SET bytes = bytes + excluded.bytes""",
            (user_id, day, num_bytes),
        )


def _usage_today(user_id: str) -> int:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with db.reading() as conn:
        row = conn.execute(
            "SELECT bytes FROM usage_ledger WHERE user_id = ? AND day = ?", (user_id, day)
        ).fetchone()
    return row["bytes"] if row else 0


def _user_cookies(user_id: str) -> str | None:
    """Decrypted only for the lifetime of one resolve/download call — never
    written anywhere except a job's own scratch dir (pipeline.py) or a temp
    file cleaned up immediately after (extract.py). A decrypt failure (key
    rotated since these were saved) is treated as "no cookies configured"
    rather than raised — a resolve or download that doesn't need private
    content must not fail because of one that's now unreadable."""
    with db.reading() as conn:
        row = conn.execute(
            "SELECT cookies_encrypted FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    if not row or not row["cookies_encrypted"]:
        return None
    try:
        return _fernet.decrypt(bytes(row["cookies_encrypted"])).decode()
    except InvalidToken:
        return None


def _finish(job_id: str, result: dict, scratch_dir: Path, user_id: str) -> None:
    # By name prefix, not an exact "audio.m4a": an MP3 profile emits audio.mp3,
    # and the exact-match version raised StopIteration on the first one.
    audio = next(
        (f for f in result["files"] if f["name"].startswith("audio")), result["files"][0]
    )
    total_bytes = sum(f["bytes"] for f in result["files"])
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(seconds=ARTIFACT_TTL_S)).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    with db.writing() as conn:
        conn.execute(
            """UPDATE jobs SET state='ready', progress=1.0, stage_detail=?,
                               artifact_path=?, artifact_bytes=?, artifact_sha256=?,
                               artifact_manifest=?, artifact_token=?,
                               artifact_expires_at=?, updated_at=?
                WHERE id=?""",
            (
                "copied" if result["copied"] else "transcoded",
                str(scratch_dir),
                total_bytes,
                audio["sha256"],
                json.dumps(result["files"]),
                token,
                expires,
                db.now(),
                job_id,
            ),
        )
    # Recorded against actual bytes produced, not the /resolve-time estimate —
    # that estimate is what gates starting a *new* job (see create_items), but
    # what actually counts against the budget is what actually got made.
    _record_usage(user_id, total_bytes)


def _fail(job_id: str, message: str, scratch_dir: Path) -> None:
    shutil.rmtree(scratch_dir, ignore_errors=True)
    with db.writing() as conn:
        conn.execute(
            "UPDATE jobs SET state='failed', error=?, updated_at=? WHERE id=?",
            (message[:1000], db.now(), job_id),
        )


# ------------------------------------------------------------- circuit breaker
#
# R-10: multi-user means yt-dlp reaches YouTube from one shared server IP. A
# 429 hitting one user's job is a signal about that shared IP, not just about
# that job — letting every other queued job keep hammering through right after
# just earns a harder rate limit for everyone. So this pauses *claiming*
# entirely for a backoff window instead of retrying the one job that hit it.

BREAKER_BASE_S = 30
BREAKER_MAX_S = 10 * 60

_breaker_until = 0.0  # monotonic deadline; claiming pauses until this passes
_consecutive_429s = 0


def _is_rate_limited(message: str) -> bool:
    return "429" in message or "Too Many Requests" in message


def _trip_breaker() -> None:
    global _breaker_until, _consecutive_429s
    _consecutive_429s += 1
    backoff = min(BREAKER_BASE_S * (2 ** (_consecutive_429s - 1)), BREAKER_MAX_S)
    _breaker_until = time.monotonic() + backoff + random.random() * backoff * 0.25
    print(
        f"[breaker] 429 detected — pausing the queue for ~{backoff:.0f}s"
        f" (consecutive: {_consecutive_429s})",
        flush=True,
    )


def _reset_breaker() -> None:
    global _consecutive_429s
    _consecutive_429s = 0


def breaker_status() -> dict:
    remaining = max(_breaker_until - time.monotonic(), 0)
    return {
        "tripped": remaining > 0,
        "retry_after_s": round(remaining),
        "consecutive_429s": _consecutive_429s,
    }


def _reap() -> None:
    """Uncollected artifacts are a correctness backstop, not the happy path —
    but the server must never sit on media, so they go on a timer."""
    with db.reading() as conn:
        stale = conn.execute(
            "SELECT id, artifact_path FROM jobs"
            " WHERE artifact_path IS NOT NULL AND artifact_expires_at < ?",
            (db.now(),),
        ).fetchall()
    for row in stale:
        shutil.rmtree(row["artifact_path"], ignore_errors=True)
        with db.writing() as conn:
            conn.execute(
                "UPDATE jobs SET state='expired', artifact_path=NULL, artifact_token=NULL,"
                " artifact_manifest=NULL, updated_at=? WHERE id=?",
                (db.now(), row["id"]),
            )


def _runner() -> None:
    while not _stop.is_set():
        if time.monotonic() < _breaker_until:
            _stop.wait(1)
            continue

        try:
            _reap()
            job = _claim()
        except Exception as err:  # a runner thread must never die quietly
            print(f"[runner] claim failed: {err!r}", flush=True)
            _stop.wait(5)
            continue

        if job is None:
            # Poll with jitter; do not busy-loop.
            _stop.wait(1 + random.random())
            continue

        scratch_dir = SCRATCH / job["id"]
        try:
            with db.reading() as conn:
                row = conn.execute(
                    """SELECT s.canonical_url, i.format_profile
                         FROM library_items i JOIN sources s ON s.source_key = i.source_key
                        WHERE i.id = ?""",
                    (job["item_id"],),
                ).fetchone()
            profile = json.loads(row["format_profile"])
            cookies_text = _user_cookies(job["user_id"])
            future = _job_pool.submit(
                pipeline.run, row["canonical_url"], profile, str(scratch_dir), cookies_text
            )
            _pump_progress(job["id"], future, scratch_dir)
            _finish(job["id"], future.result(timeout=0), scratch_dir, job["user_id"])
            _reset_breaker()  # a completed job is proof the shared IP isn't blocked right now
        except FutureTimeout:
            future.cancel()
            _fail(job["id"], f"Gave up after {JOB_TIMEOUT_S // 60} minutes.", scratch_dir)
        except Exception as err:
            message = f"{type(err).__name__}: {err}"
            _fail(job["id"], message, scratch_dir)
            if _is_rate_limited(message):
                _trip_breaker()


def _pump_progress(job_id: str, future, scratch_dir: Path) -> None:
    """Wait for the job, republishing whatever the worker last wrote.

    At most one DB write per second per job: fine-grained values belong on the
    SSE stream, not in a row that would otherwise be rewritten hundreds of times
    for a single download.
    """
    deadline = time.monotonic() + JOB_TIMEOUT_S
    last = None
    while True:
        done, _ = futures.wait([future], timeout=1)
        if done:
            return
        if time.monotonic() > deadline:
            raise FutureTimeout()
        try:
            report = json.loads((scratch_dir / "progress.json").read_text())
        except (OSError, ValueError):
            continue
        current = (report["stage"], round(report["fraction"], 2))
        if current == last:
            continue
        last = current
        with db.writing() as conn:
            conn.execute(
                "UPDATE jobs SET state=?, progress=?, stage_detail=?, updated_at=? WHERE id=?",
                (report["stage"], report["fraction"], report["stage"], db.now(), job_id),
            )


def _run_canary() -> None:
    for name, url in CANARY_URLS.items():
        started = time.monotonic()
        try:
            _, entry = _resolve_pool.submit(extract.probe, url, 192).result(timeout=90)
            _canary[name] = {
                "ok": True,
                "title": entry["title"],
                "checked_at": db.now(),
                "took_ms": int((time.monotonic() - started) * 1000),
            }
        except Exception as err:
            _canary[name] = {
                "ok": False,
                "error": f"{type(err).__name__}: {err}"[:300],
                "checked_at": db.now(),
                "took_ms": int((time.monotonic() - started) * 1000),
            }


def _canary_loop() -> None:
    while not _stop.is_set():
        _run_canary()
        _stop.wait(CANARY_EVERY_S)


def _sweep_orphan_scratch() -> None:
    """Anything in scratch with no job pointing at it is debris from a crash."""
    if not SCRATCH.exists():
        return
    with db.reading() as conn:
        live = {r[0] for r in conn.execute(
            "SELECT artifact_path FROM jobs WHERE artifact_path IS NOT NULL"
        )}
    for path in SCRATCH.iterdir():
        if str(path) not in live:
            shutil.rmtree(path, ignore_errors=True)


def _reap_missing_artifacts() -> None:
    """The mirror image of _sweep_orphan_scratch: a 'ready' job whose scratch
    directory is gone. Scratch is tmpfs (docs/01-architecture.md) — it does
    not survive a container restart, but the DB row does, so a job finished
    right before a restart is stuck 'ready' pointing at nothing. Left alone,
    every download attempt just 404s forever with no signal to retry."""
    with db.reading() as conn:
        rows = conn.execute(
            "SELECT id, artifact_path FROM jobs WHERE state='ready' AND artifact_path IS NOT NULL"
        ).fetchall()
    stale = [r["id"] for r in rows if not Path(r["artifact_path"]).exists()]
    if not stale:
        return
    with db.writing() as conn:
        conn.executemany(
            """UPDATE jobs SET state='failed', error=?, artifact_path=NULL,
                               artifact_token=NULL, artifact_manifest=NULL,
                               updated_at=? WHERE id=?""",
            [("Artifact lost in a server restart. Retry the job.", db.now(), job_id)
             for job_id in stale],
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _resolve_pool, _job_pool
    db.init()
    SCRATCH.mkdir(parents=True, exist_ok=True)
    _sweep_orphan_scratch()
    _reap_missing_artifacts()
    # Forking a worker from this process is unsafe by the time these pools are
    # first used: _runner/_canary_loop threads (started below) and FastAPI's
    # own threadpool for sync endpoints are live by then, and fork()ing a
    # multi-threaded process can silently deadlock a child that inherits a
    # lock (e.g. logging's) held by some other thread at that instant — the
    # worker never starts, .result(timeout=...) just runs out the clock with
    # no exception. spawn sidesteps that entirely.
    _mp = multiprocessing.get_context("spawn")
    _resolve_pool = ProcessPoolExecutor(max_workers=2, mp_context=_mp)
    _job_pool = ProcessPoolExecutor(max_workers=RUNNERS, mp_context=_mp)
    threads = [threading.Thread(target=_runner, daemon=True) for _ in range(RUNNERS)]
    threads.append(threading.Thread(target=_canary_loop, daemon=True))
    for t in threads:
        t.start()
    try:
        yield
    finally:
        _stop.set()
        _resolve_pool.shutdown(cancel_futures=True)
        _job_pool.shutdown(cancel_futures=True)


# FastAPI serves interactive docs (Swagger UI, ReDoc) and the raw OpenAPI
# schema publicly, with no auth, by default. For most APIs that's a feature;
# for an invite-only app whose whole security posture is "a small trusted
# group," anonymously handing out a complete, precise map of every endpoint,
# field name, and validation rule — plus a console to try requests directly
# from a browser — is free reconnaissance this app has no reason to offer.
# Off by default; set PWA_YT_ENABLE_DOCS=1 for local development.
_enable_docs = os.environ.get("PWA_YT_ENABLE_DOCS", "").lower() in ("1", "true", "yes")

app = FastAPI(
    title="PWA-YT",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if _enable_docs else None,
    redoc_url="/redoc" if _enable_docs else None,
    openapi_url="/openapi.json" if _enable_docs else None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGINS,
    allow_methods=["GET", "POST", "DELETE", "PATCH", "PUT"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _security_headers(request, call_next):
    """Cheap, standard, no-downside hardening on every response. Most
    relevant to /jobs/{id}/artifact/{filename}: it already sets an explicit
    media_type, but nosniff means a browser given a direct artifact link
    can never be talked into treating it as something else — e.g. HTML —
    based on sniffing the bytes instead of trusting the declared type."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.exception_handler(HTTPException)
async def _http_exception_handler(_, exc: HTTPException):
    """auth.py (and anything else) raises plain HTTPException; this reshapes
    it into the same {error, message} contract every other endpoint uses,
    rather than FastAPI's default {"detail": ...}."""
    return error(exc.status_code, _ERROR_CODES.get(exc.status_code, "error"), str(exc.detail))


# ----------------------------------------------------------------- endpoints


@app.get("/health")
def health() -> dict:
    return {"ok": True, "version": app.version}


@app.get("/health/extractors")
def health_extractors():
    """Result of the periodic canary — your early warning for extractor
    breakage, which is the single most likely thing to take this app down.
    Also carries the 429 circuit breaker's state (R-10) — the two are related
    monitoring concerns (is the extractor path healthy right now) even though
    the breaker reacts to real jobs, not the canary."""
    breaker = breaker_status()
    if not _canary:
        return {"extractors": {}, "circuit_breaker": breaker, "note": "no canary run has completed yet"}
    status = 200 if all(r["ok"] for r in _canary.values()) and not breaker["tripped"] else 503
    return JSONResponse({"extractors": _canary, "circuit_breaker": breaker}, status_code=status)


# --------------------------------------------------------------------- auth
#
# Usernameless passkeys throughout: registration requires a resident/
# discoverable credential, and login never sends `allow_credentials`, so the
# authenticator's own picker is the entire identity UI. See auth.py.


@app.post("/auth/register/begin")
def register_begin(req: RegisterBeginRequest):
    ceremony_id, options_json = auth.begin_registration(
        req.email.strip().lower(), req.display_name.strip(), req.invite_code.strip()
    )
    return {"ceremony_id": ceremony_id, "options": json.loads(options_json)}


@app.post("/auth/register/finish")
def register_finish(req: CeremonyFinishRequest):
    return auth.finish_registration(req.ceremony_id, req.credential)


@app.post("/auth/login/begin")
def login_begin():
    ceremony_id, options_json = auth.begin_login()
    return {"ceremony_id": ceremony_id, "options": json.loads(options_json)}


@app.post("/auth/login/finish")
def login_finish(req: CeremonyFinishRequest):
    return auth.finish_login(req.ceremony_id, req.credential)


@app.post("/auth/logout")
def logout(authorization: str | None = Header(default=None)):
    # Server-side only — see 04-api.md. The client separately never clears
    # the local catalogue/OPFS on logout; see FM-2.
    auth.delete_session(authorization)
    return {"ok": True}


@app.get("/me")
def me(user: dict = Depends(auth.current_user)):
    with db.reading() as conn:
        row = conn.execute(
            "SELECT id, email, display_name, daily_byte_budget, max_concurrent, created_at"
            " FROM users WHERE id = ?",
            (user["id"],),
        ).fetchone()
    return dict(row)


@app.get("/me/usage")
def me_usage(user: dict = Depends(auth.current_user)):
    with db.reading() as conn:
        budget = conn.execute(
            "SELECT daily_byte_budget FROM users WHERE id = ?", (user["id"],)
        ).fetchone()["daily_byte_budget"]
        active_jobs = conn.execute(
            "SELECT COUNT(*) AS c FROM jobs"
            " WHERE user_id = ? AND state IN ('queued','fetching','transforming')",
            (user["id"],),
        ).fetchone()["c"]
    used = _usage_today(user["id"])
    return {
        "bytes_used_today": used,
        "daily_byte_budget": budget,
        "remaining_bytes": max(budget - used, 0),
        "active_jobs": active_jobs,
    }


class CookiesRequest(BaseModel):
    # Netscape cookie-file format — exactly what yt-dlp's `cookiefile` option
    # (and browser extensions that export cookies) already produce. Real
    # exports are a few KB even with hundreds of cookies; 256 KiB is
    # generous headroom without leaving the column an unbounded BLOB.
    cookies: str = Field(min_length=1, max_length=256 * 1024)


@app.put("/me/cookies")
def put_cookies(req: CookiesRequest, user: dict = Depends(auth.current_user)):
    encrypted = _fernet.encrypt(req.cookies.encode())
    with db.writing() as conn:
        conn.execute(
            "UPDATE users SET cookies_encrypted = ?, cookies_updated_at = ? WHERE id = ?",
            (encrypted, db.now(), user["id"]),
        )
    return {"ok": True}


@app.get("/me/cookies")
def get_cookies_status(user: dict = Depends(auth.current_user)):
    # Write-only from the client's perspective — this reports whether cookies
    # are configured and when, never the plaintext (or ciphertext) back out.
    with db.reading() as conn:
        row = conn.execute(
            "SELECT cookies_updated_at FROM users WHERE id = ?", (user["id"],)
        ).fetchone()
    return {"configured": row["cookies_updated_at"] is not None, "updated_at": row["cookies_updated_at"]}


@app.delete("/me/cookies")
def delete_cookies(user: dict = Depends(auth.current_user)):
    with db.writing() as conn:
        conn.execute(
            "UPDATE users SET cookies_encrypted = NULL, cookies_updated_at = NULL WHERE id = ?",
            (user["id"],),
        )
    return {"ok": True}


def _upsert_source(entry: dict) -> None:
    with db.writing() as conn:
        conn.execute(
            """INSERT INTO sources (source_key, extractor, source_id, canonical_url,
                                    title, uploader, duration_s, thumb_url, refreshed_at)
               VALUES (:source_key, :extractor, :source_id, :canonical_url,
                       :title, :uploader, :duration_s, :thumb_url, :refreshed_at)
               ON CONFLICT(source_key) DO UPDATE SET
                 title=excluded.title, uploader=excluded.uploader,
                 duration_s=excluded.duration_s, thumb_url=excluded.thumb_url,
                 canonical_url=excluded.canonical_url, refreshed_at=excluded.refreshed_at""",
            {**entry, "refreshed_at": db.now()},
        )


def _already_in_library(user_id: str, source_key: str) -> bool:
    with db.reading() as conn:
        row = conn.execute(
            "SELECT 1 FROM library_items"
            " WHERE user_id = ? AND source_key = ? AND deleted_at IS NULL",
            (user_id, source_key),
        ).fetchone()
    return row is not None


def _resolved_entry(user_id: str, entry: dict) -> dict:
    return {
        "source_key": entry["source_key"],
        "title": entry["title"],
        "uploader": entry["uploader"],
        "duration_s": entry["duration_s"],
        "thumb_url": entry["thumb_url"],
        "estimated_bytes": entry["estimated_bytes"],
        "already_in_library": _already_in_library(user_id, entry["source_key"]),
    }


@app.post("/resolve")
def resolve(req: ResolveRequest, user: dict = Depends(auth.current_user)):
    """Stage 2. Returns a plan, not a job. Nothing is downloaded or enqueued.

    Resolving before enqueueing is the difference between a usable import and an
    act of faith — the user sees title, duration and estimated size before
    committing anything to their phone. A single item comes back as one JSON
    object; a playlist streams as NDJSON so entries appear as they're enumerated
    rather than all at once after a 400-entry wait.

    This shares the circuit breaker with the job runner (R-10): resolving also
    reaches the shared server IP, so a 429 here is the same signal a 429
    mid-download is, and the reverse — if the breaker is already tripped from
    a download job's 429, hammering /resolve while it cools down would just
    make that worse. Neither path should be the one that doesn't know about
    the other.
    """
    breaker = breaker_status()
    if breaker["tripped"]:
        return error(
            503,
            "rate_limited",
            "Temporarily backing off after a rate limit from the source site. Try again shortly.",
            retry_after_s=breaker["retry_after_s"],
        )

    try:
        kind, payload = _resolve_pool.submit(
            extract.probe, req.url, req.format_profile.audio_bitrate, _user_cookies(user["id"])
        ).result(timeout=RESOLVE_TIMEOUT_S)
    except FutureTimeout:
        return error(
            504,
            "resolve_timeout",
            f"That URL took more than {RESOLVE_TIMEOUT_S}s to look up. "
            "The site may be slow or blocking us; try again in a minute.",
        )
    except extract.ResolveError as err:
        if _is_rate_limited(str(err)):
            _trip_breaker()
        return error(422, "resolve_failed", str(err))

    _reset_breaker()  # a successful resolve is proof the shared IP isn't blocked right now

    if kind == "item":
        _upsert_source(payload)
        return {"kind": "item", "entry": _resolved_entry(user["id"], payload)}

    def events():
        entries = payload["entries"]
        yield json.dumps(
            {"kind": "playlist_head", "title": payload["title"], "entry_count": len(entries)}
        ) + "\n"
        total = 0
        for i, entry in enumerate(entries):
            _upsert_source(entry)
            total += entry["estimated_bytes"] or 0
            yield json.dumps({"kind": "entry", "index": i, **_resolved_entry(user["id"], entry)}) + "\n"
        yield json.dumps({"kind": "playlist_done", "total_estimated_bytes": total}) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson")


def _next_utc_midnight() -> str:
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date()
    return f"{tomorrow.isoformat()}T00:00:00.000Z"


@app.post("/items")
def create_items(req: ItemsRequest, user: dict = Depends(auth.current_user)):
    """Stage 3. Creates items and enqueues jobs. Idempotent on (user, source)."""
    # Checked once per call, against today's already-recorded usage — not a
    # precise projection of what this batch would add (that would need every
    # entry's estimated size threaded through from /resolve). Good enough to
    # stop someone starting new downloads once they're already over budget for
    # the day; the budget is a rough daily cap, not a hard byte-exact ledger.
    with db.reading() as conn:
        daily_budget = conn.execute(
            "SELECT daily_byte_budget FROM users WHERE id = ?", (user["id"],)
        ).fetchone()["daily_byte_budget"]
    if _usage_today(user["id"]) >= daily_budget:
        return error(
            429,
            "quota_exceeded",
            f"This would exceed today's {daily_budget / 1e9:.1f} GB limit.",
            retry_after=_next_utc_midnight(),
        )

    created = []
    for entry in req.entries:
        with db.reading() as conn:
            known = conn.execute(
                "SELECT 1 FROM sources WHERE source_key = ?", (entry.source_key,)
            ).fetchone()
        if not known:
            return error(
                422,
                "unknown_source",
                f"{entry.source_key} has not been resolved. Call /resolve first.",
            )

        profile = entry.format_profile.model_dump_json()
        with db.writing() as conn:
            row = conn.execute(
                """INSERT INTO library_items
                       (id, user_id, source_key, format_profile, added_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, source_key) DO UPDATE
                       SET deleted_at = NULL,
                           updated_at = excluded.updated_at,
                           -- The profile is per item precisely so that re-pulling
                           -- the same track at a different bitrate or codec is a
                           -- button, not a migration. Without this the second
                           -- request silently reuses the first one's format.
                           format_profile = excluded.format_profile
                   RETURNING id, (added_at != updated_at) AS existed""",
                (db.uuid7(), user["id"], entry.source_key, profile, db.now(), db.now()),
            ).fetchone()
            item_id, existed = row["id"], bool(row["existed"])

            job = conn.execute(
                """INSERT INTO jobs (id, user_id, item_id, state, created_at, updated_at)
                   VALUES (?, ?, ?, 'queued', ?, ?) RETURNING id""",
                (db.uuid7(), user["id"], item_id, db.now(), db.now()),
            ).fetchone()

            if req.playlist_id and entry.position:
                owns = conn.execute(
                    "SELECT 1 FROM playlists WHERE id=? AND user_id=?",
                    (req.playlist_id, user["id"]),
                ).fetchone()
                if not owns:
                    raise HTTPException(404, "No such playlist.")
                conn.execute(
                    """INSERT INTO playlist_items (playlist_id, item_id, position, updated_at, deleted_at)
                       VALUES (?, ?, ?, ?, NULL)
                       ON CONFLICT(playlist_id, item_id) DO UPDATE SET
                         position=excluded.position, updated_at=excluded.updated_at, deleted_at=NULL""",
                    (req.playlist_id, item_id, entry.position, db.now()),
                )

        created.append(
            {
                "item_id": item_id,
                "job_id": job["id"],
                "source_key": entry.source_key,
                "already_existed": existed,
            }
        )
    return {"items": created}


@app.get("/items")
def list_items(user: dict = Depends(auth.current_user)):
    with db.reading() as conn:
        rows = conn.execute(
            """SELECT i.id, i.source_key, i.format_profile, i.added_at,
                      s.title, s.uploader, s.duration_s
                 FROM library_items i JOIN sources s ON s.source_key = i.source_key
                WHERE i.user_id = ? AND i.deleted_at IS NULL
                ORDER BY i.added_at DESC""",
            (user["id"],),
        ).fetchall()
    return {
        "items": [
            {**dict(r), "format_profile": json.loads(r["format_profile"])} for r in rows
        ]
    }


@app.delete("/items/{item_id}")
def delete_item(item_id: str, user: dict = Depends(auth.current_user)):
    # Cascades into every playlist that held this item, in the same
    # transaction — otherwise a deleted item keeps a dangling playlist_items
    # row that only the UI's join hides. See D-018.
    #
    # Gated on `RETURNING id` actually matching a row: without this, calling
    # delete on an item_id you don't own is a no-op for library_items (the
    # WHERE excludes it) but the cascade below used to run unconditionally
    # against *any* item_id regardless of whose it was — letting one account
    # rip a track out of another account's playlist just by knowing its item
    # id. Confirmed and fixed 2026-08-23; see 08-decisions.md D-021.
    with db.writing() as conn:
        owned = conn.execute(
            "UPDATE library_items SET deleted_at=?, updated_at=? WHERE id=? AND user_id=? RETURNING id",
            (db.now(), db.now(), item_id, user["id"]),
        ).fetchone()
        if owned:
            conn.execute(
                "UPDATE playlist_items SET deleted_at=?, updated_at=?"
                " WHERE item_id=? AND deleted_at IS NULL",
                (db.now(), db.now(), item_id),
            )
    return {"ok": True}


# --------------------------------------------------------------- playlists
#
# Every mutation below is safe to replay: create takes a client-generated id
# (INSERT ... DO NOTHING), rename/delete are idempotent by nature (last write
# always wins), and the items upsert is ON CONFLICT DO UPDATE. That is what
# lets the client's offline outbox just retry the same call rather than
# needing a separate idempotency-key ledger. See 08-decisions.md D-018.


@app.post("/playlists")
def create_playlist(req: PlaylistCreate, user: dict = Depends(auth.current_user)):
    with db.writing() as conn:
        conn.execute(
            """INSERT INTO playlists (id, user_id, name, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(id) DO NOTHING""",
            (req.id, user["id"], req.name, db.now(), db.now()),
        )
    return {"ok": True, "id": req.id}


@app.get("/playlists")
def list_playlists(user: dict = Depends(auth.current_user)):
    with db.reading() as conn:
        playlists = conn.execute(
            "SELECT * FROM playlists WHERE user_id=? AND deleted_at IS NULL ORDER BY created_at",
            (user["id"],),
        ).fetchall()
        pl_items = conn.execute(
            """SELECT pi.playlist_id, pi.item_id, pi.position
                 FROM playlist_items pi JOIN playlists p ON p.id = pi.playlist_id
                WHERE p.user_id = ? AND pi.deleted_at IS NULL
                ORDER BY pi.playlist_id, pi.position""",
            (user["id"],),
        ).fetchall()
    by_playlist: dict[str, list] = {}
    for row in pl_items:
        by_playlist.setdefault(row["playlist_id"], []).append(
            {"item_id": row["item_id"], "position": row["position"]}
        )
    return {
        "playlists": [
            {**dict(p), "items": by_playlist.get(p["id"], [])} for p in playlists
        ]
    }


def _own_playlist(conn, playlist_id: str, user_id: str) -> None:
    if not conn.execute(
        "SELECT 1 FROM playlists WHERE id=? AND user_id=? AND deleted_at IS NULL",
        (playlist_id, user_id),
    ).fetchone():
        raise HTTPException(404, "No such playlist.")


@app.patch("/playlists/{playlist_id}")
def rename_playlist(playlist_id: str, req: PlaylistRename, user: dict = Depends(auth.current_user)):
    with db.writing() as conn:
        _own_playlist(conn, playlist_id, user["id"])
        conn.execute(
            "UPDATE playlists SET name=?, updated_at=? WHERE id=? AND user_id=?",
            (req.name, db.now(), playlist_id, user["id"]),
        )
    return {"ok": True}


@app.delete("/playlists/{playlist_id}")
def delete_playlist(playlist_id: str, user: dict = Depends(auth.current_user)):
    with db.writing() as conn:
        conn.execute(
            "UPDATE playlists SET deleted_at=?, updated_at=? WHERE id=? AND user_id=?",
            (db.now(), db.now(), playlist_id, user["id"]),
        )
    return {"ok": True}


@app.put("/playlists/{playlist_id}/items")
def patch_playlist_items(
    playlist_id: str, req: PlaylistItemsPatch, user: dict = Depends(auth.current_user)
):
    """Full ordered replacement (many upserts) or a single-move patch (one
    upsert) — same endpoint, see 04-api.md. A reorder writes exactly the rows
    that moved, never the whole list, which is what keeps a 200-track offline
    reorder to one outbox row per move rather than a renumbered tail."""
    with db.writing() as conn:
        _own_playlist(conn, playlist_id, user["id"])
        # Every upserted item_id must belong to this same user — otherwise
        # one account could attach another account's item into their own
        # playlist just by knowing its id (only playlist ownership was
        # checked before; the FK to library_items is satisfied by *any*
        # user's item). Checked as a batch, up front: fail the whole call
        # rather than partially applying. Confirmed and fixed 2026-08-23;
        # see 08-decisions.md D-021.
        for u in req.upserts:
            if not conn.execute(
                "SELECT 1 FROM library_items WHERE id=? AND user_id=? AND deleted_at IS NULL",
                (u.item_id, user["id"]),
            ).fetchone():
                raise HTTPException(404, f"No such item: {u.item_id}")
        for u in req.upserts:
            conn.execute(
                """INSERT INTO playlist_items (playlist_id, item_id, position, updated_at, deleted_at)
                   VALUES (?, ?, ?, ?, NULL)
                   ON CONFLICT(playlist_id, item_id) DO UPDATE SET
                     position=excluded.position, updated_at=excluded.updated_at, deleted_at=NULL""",
                (playlist_id, u.item_id, u.position, db.now()),
            )
        for item_id in req.removes:
            conn.execute(
                """UPDATE playlist_items SET deleted_at=?, updated_at=?
                    WHERE playlist_id=? AND item_id=?""",
                (db.now(), db.now(), playlist_id, item_id),
            )
    return {"ok": True}


# ------------------------------------------------------------------- sync
#
# Multi-device convergence, pull half. The push half is already done: every
# offline mutation replays through the same idempotent REST endpoints above
# (D-018) — a dedicated /sync/outbox batch endpoint would just be a second,
# redundant way to call code that already exists. What was actually missing
# is a way for a device to learn what *another* device did, which is this.
#
# Cursor is opaque to the client: a base64url JSON blob of one (updated_at,
# id) position per table. Row-value comparison — `(updated_at, id) > (?, ?)`
# — is what 03-data-model.md's cursor design is for: it can't skip a row that
# shares a millisecond with the cursor's own row, which plain `updated_at > ?`
# would.


def _encode_cursor(positions: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(positions).encode()).decode()


def _decode_cursor(raw: str) -> dict:
    if not raw:
        return {}
    try:
        decoded = json.loads(base64.urlsafe_b64decode(raw.encode()))
    except Exception:
        return {}  # a corrupt/foreign cursor degrades to "sync everything", not a 500
    return decoded if isinstance(decoded, dict) else {}


def _cursor_position(cursor: dict, key: str, arity: int) -> tuple:
    """A cursor is client-supplied and round-tripped opaquely — decoding
    cleanly as JSON doesn't mean its *shape* is what this endpoint expects
    (found live 2026-08-23: `{"items": "oops"}` decodes fine but isn't a
    2-element position, and unpacking it crashed the whole request with an
    unhandled 500). Anything that isn't exactly `arity` strings degrades to
    the same "sync everything" fallback a corrupt cursor already gets,
    rather than being trusted enough to crash on."""
    value = cursor.get(key)
    if isinstance(value, list) and len(value) == arity and all(isinstance(v, str) for v in value):
        return tuple(value)
    return ("",) * arity


@app.get("/sync")
def sync(since: str = "", user: dict = Depends(auth.current_user)):
    cursor = _decode_cursor(since)
    uid = user["id"]

    items_ts, items_id = _cursor_position(cursor, "items", 2)
    playlists_ts, playlists_id = _cursor_position(cursor, "playlists", 2)
    pi_ts, pi_pid, pi_iid = _cursor_position(cursor, "playlist_items", 3)

    with db.reading() as conn:
        items = conn.execute(
            """SELECT i.id, i.source_key, i.format_profile, i.added_at,
                      i.updated_at, i.deleted_at, s.title, s.uploader, s.duration_s
                 FROM library_items i JOIN sources s ON s.source_key = i.source_key
                WHERE i.user_id = ? AND (i.updated_at, i.id) > (?, ?)
                ORDER BY i.updated_at, i.id""",
            (uid, items_ts, items_id),
        ).fetchall()
        playlists = conn.execute(
            """SELECT id, name, created_at, updated_at, deleted_at FROM playlists
                WHERE user_id = ? AND (updated_at, id) > (?, ?)
                ORDER BY updated_at, id""",
            (uid, playlists_ts, playlists_id),
        ).fetchall()
        playlist_items = conn.execute(
            """SELECT pi.playlist_id, pi.item_id, pi.position, pi.updated_at, pi.deleted_at
                 FROM playlist_items pi JOIN playlists p ON p.id = pi.playlist_id
                WHERE p.user_id = ? AND (pi.updated_at, pi.playlist_id, pi.item_id) > (?, ?, ?)
                ORDER BY pi.updated_at, pi.playlist_id, pi.item_id""",
            (uid, pi_ts, pi_pid, pi_iid),
        ).fetchall()

    new_cursor = {
        "items": [items[-1]["updated_at"], items[-1]["id"]] if items else [items_ts, items_id],
        "playlists": [playlists[-1]["updated_at"], playlists[-1]["id"]]
        if playlists
        else [playlists_ts, playlists_id],
        "playlist_items": [
            playlist_items[-1]["updated_at"],
            playlist_items[-1]["playlist_id"],
            playlist_items[-1]["item_id"],
        ]
        if playlist_items
        else [pi_ts, pi_pid, pi_iid],
    }
    return {
        "items": [
            {**dict(r), "format_profile": json.loads(r["format_profile"])} for r in items
        ],
        "playlists": [dict(r) for r in playlists],
        "playlist_items": [dict(r) for r in playlist_items],
        "cursor": _encode_cursor(new_cursor),
    }


def _job_json(row) -> dict:
    files = json.loads(row["artifact_manifest"]) if row["artifact_manifest"] else []
    return {
        "id": row["id"],
        "item_id": row["item_id"],
        "state": row["state"],
        "progress": row["progress"],
        "stage_detail": row["stage_detail"],
        "error": row["error"],
        # Paths are relative: the server owns the shape, the client owns the
        # origin. An absolute URL built from request.base_url would name the
        # server's own host and so bypass the dev proxy and any tunnel in front
        # of it — which is precisely the setup the phone is tested through.
        "files": [
            {**f, "path": f"/jobs/{row['id']}/artifact/{f['name']}?token={row['artifact_token']}"}
            for f in files
        ],
    }


@app.get("/jobs")
def list_jobs(user: dict = Depends(auth.current_user)):
    # The client uses /jobs/stream, not this. Kept because it is the documented
    # contract in 04-api.md and it is what you reach for with curl when the
    # stream is misbehaving.
    with db.reading() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT 50",
            (user["id"],),
        ).fetchall()
    return {"jobs": [_job_json(r) for r in rows]}


@app.get("/jobs/stream")
def stream_jobs(user: dict = Depends(auth.current_user)):
    """One SSE connection for all in-flight jobs.

    Not WebSockets: progress is one-directional, SSE reconnects on its own, and
    it survives mobile network transitions better than a socket you have to
    babysit. Connections are capped so a client that wanders off does not hold
    a threadpool worker forever — EventSource reconnects by itself.
    """
    user_id = user["id"]  # captured now — `Depends` doesn't re-run inside the generator

    def events():
        last = None
        deadline = time.monotonic() + SSE_MAX_S
        while time.monotonic() < deadline:
            with db.reading() as conn:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT 50",
                    (user_id,),
                ).fetchall()
            payload = json.dumps({"jobs": [_job_json(r) for r in rows]})
            if payload != last:
                last = payload
                yield f"data: {payload}\n\n"
            time.sleep(1)
        yield "event: reconnect\ndata: {}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/jobs/{job_id}/artifact/{filename}")
def get_artifact(job_id: str, filename: str, token: str = "", user: dict = Depends(auth.current_user)):
    """Signed, short-TTL, resumable. Starlette handles Range on FileResponse."""
    if filename not in {"audio.m4a", "audio.mp3", "video.mp4", "art.jpg", "art-sq.jpg"}:
        return error(404, "not_found", "No such artifact file.")

    with db.reading() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ? AND user_id = ?",
                           (job_id, user["id"])).fetchone()
    if row is None or not row["artifact_path"] or not row["artifact_token"]:
        return error(404, "not_found", "That artifact is gone. Retry the job to rebuild it.")
    if not secrets.compare_digest(token, row["artifact_token"]):
        return error(403, "bad_token", "That download link is not valid.")
    if row["artifact_expires_at"] < db.now():
        return error(410, "expired", "That download link expired. Retry the job to rebuild it.")

    path = Path(row["artifact_path"]) / filename
    if not path.exists():
        return error(404, "not_found", "No such artifact file.")

    manifest = {f["name"]: f for f in json.loads(row["artifact_manifest"])}
    return FileResponse(
        path,
        headers={"X-Artifact-SHA256": manifest[filename]["sha256"]},
        media_type="application/octet-stream",
    )


@app.delete("/jobs/{job_id}/artifact")
def collect_artifact(job_id: str, user: dict = Depends(auth.current_user)):
    """The collection acknowledgement — the contract that keeps the server
    stateless. Scratch is deleted immediately."""
    with db.reading() as conn:
        row = conn.execute("SELECT artifact_path FROM jobs WHERE id = ? AND user_id = ?",
                           (job_id, user["id"])).fetchone()
    if row is None:
        return error(404, "not_found", "No such job.")
    if row["artifact_path"]:
        shutil.rmtree(row["artifact_path"], ignore_errors=True)
    with db.writing() as conn:
        conn.execute(
            "UPDATE jobs SET state='collected', artifact_path=NULL, artifact_token=NULL,"
            " artifact_manifest=NULL, updated_at=? WHERE id=?",
            (db.now(), job_id),
        )
    return {"ok": True}


@app.post("/jobs/{job_id}/retry")
def retry_job(job_id: str, user: dict = Depends(auth.current_user)):
    with db.writing() as conn:
        conn.execute(
            "UPDATE jobs SET state='queued', error=NULL, progress=0, updated_at=?"
            " WHERE id=? AND user_id=? AND state IN ('failed','expired')",
            (db.now(), job_id, user["id"]),
        )
    return {"ok": True}
