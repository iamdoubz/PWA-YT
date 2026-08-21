"""FastAPI surface plus the in-process job runner.

Endpoints are sync `def`, so FastAPI runs them in its threadpool and blocking
SQLite calls never touch the event loop.
"""

import json
import os
import random
import secrets
import shutil
import threading
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FutureTimeout
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

import db
import extract
import pipeline

RESOLVE_TIMEOUT_S = 60
JOB_TIMEOUT_S = 30 * 60
ARTIFACT_TTL_S = 60 * 60
RUNNERS = 2

# Deliberately not S3. In deployment this is a size-capped tmpfs mount, so a
# crashed process cannot leave media on a real disk.
SCRATCH = Path(os.environ.get("TARMAC_SCRATCH", Path(__file__).parent / "scratch"))
ORIGINS = os.environ.get("TARMAC_ORIGINS", "*").split(",")

_resolve_pool: ProcessPoolExecutor | None = None
_job_pool: ProcessPoolExecutor | None = None
_stop = threading.Event()


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


class ItemsRequest(BaseModel):
    entries: list[ItemEntry] = Field(min_length=1)


def error(status: int, code: str, message: str, **extra) -> JSONResponse:
    """Messages are written for the user, not the log. Say what happened and
    what to do about it."""
    return JSONResponse({"error": code, "message": message, **extra}, status_code=status)


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


def _finish(job_id: str, result: dict, scratch_dir: Path) -> None:
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
                sum(f["bytes"] for f in result["files"]),
                next(f["sha256"] for f in result["files"] if f["name"] == "audio.m4a"),
                json.dumps(result["files"]),
                token,
                expires,
                db.now(),
                job_id,
            ),
        )


def _fail(job_id: str, message: str, scratch_dir: Path) -> None:
    shutil.rmtree(scratch_dir, ignore_errors=True)
    with db.writing() as conn:
        conn.execute(
            "UPDATE jobs SET state='failed', error=?, updated_at=? WHERE id=?",
            (message[:1000], db.now(), job_id),
        )


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
            result = _job_pool.submit(
                pipeline.run, row["canonical_url"], profile, str(scratch_dir)
            ).result(timeout=JOB_TIMEOUT_S)
            _finish(job["id"], result, scratch_dir)
        except FutureTimeout:
            _fail(job["id"], f"Gave up after {JOB_TIMEOUT_S // 60} minutes.", scratch_dir)
        except Exception as err:
            _fail(job["id"], f"{type(err).__name__}: {err}", scratch_dir)


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


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _resolve_pool, _job_pool
    db.init()
    SCRATCH.mkdir(parents=True, exist_ok=True)
    _sweep_orphan_scratch()
    _resolve_pool = ProcessPoolExecutor(max_workers=2)
    _job_pool = ProcessPoolExecutor(max_workers=RUNNERS)
    threads = [threading.Thread(target=_runner, daemon=True) for _ in range(RUNNERS)]
    for t in threads:
        t.start()
    try:
        yield
    finally:
        _stop.set()
        _resolve_pool.shutdown(cancel_futures=True)
        _job_pool.shutdown(cancel_futures=True)


app = FastAPI(title="Tarmac", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGINS,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)


# ----------------------------------------------------------------- endpoints


@app.get("/health")
def health() -> dict:
    return {"ok": True, "version": app.version}


@app.post("/resolve")
def resolve(req: ResolveRequest):
    """Stage 2. Returns a plan, not a job. Nothing is downloaded or enqueued.

    Resolving before enqueueing is the difference between a usable import and an
    act of faith — the user sees title, duration and estimated size before
    committing anything to their phone.
    """
    try:
        entry = _resolve_pool.submit(
            extract.resolve, req.url, req.format_profile.audio_bitrate
        ).result(timeout=RESOLVE_TIMEOUT_S)
    except FutureTimeout:
        return error(
            504,
            "resolve_timeout",
            f"That URL took more than {RESOLVE_TIMEOUT_S}s to look up. "
            "The site may be slow or blocking us; try again in a minute.",
        )
    except extract.ResolveError as err:
        return error(422, "resolve_failed", str(err))

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

    with db.reading() as conn:
        existing = conn.execute(
            "SELECT 1 FROM library_items"
            " WHERE user_id = ? AND source_key = ? AND deleted_at IS NULL",
            (db.DEV_USER_ID, entry["source_key"]),
        ).fetchone()

    return {
        "kind": "item",
        "entry": {
            "source_key": entry["source_key"],
            "title": entry["title"],
            "uploader": entry["uploader"],
            "duration_s": entry["duration_s"],
            "thumb_url": entry["thumb_url"],
            "estimated_bytes": entry["estimated_bytes"],
            "already_in_library": existing is not None,
        },
    }


@app.post("/items")
def create_items(req: ItemsRequest):
    """Stage 3. Creates items and enqueues jobs. Idempotent on (user, source)."""
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
                       SET deleted_at = NULL, updated_at = excluded.updated_at
                   RETURNING id, (added_at != updated_at) AS existed""",
                (db.uuid7(), db.DEV_USER_ID, entry.source_key, profile, db.now(), db.now()),
            ).fetchone()
            item_id, existed = row["id"], bool(row["existed"])

            job = conn.execute(
                """INSERT INTO jobs (id, user_id, item_id, state, created_at, updated_at)
                   VALUES (?, ?, ?, 'queued', ?, ?) RETURNING id""",
                (db.uuid7(), db.DEV_USER_ID, item_id, db.now(), db.now()),
            ).fetchone()

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
def list_items():
    with db.reading() as conn:
        rows = conn.execute(
            """SELECT i.id, i.source_key, i.format_profile, i.added_at,
                      s.title, s.uploader, s.duration_s
                 FROM library_items i JOIN sources s ON s.source_key = i.source_key
                WHERE i.user_id = ? AND i.deleted_at IS NULL
                ORDER BY i.added_at DESC""",
            (db.DEV_USER_ID,),
        ).fetchall()
    return {
        "items": [
            {**dict(r), "format_profile": json.loads(r["format_profile"])} for r in rows
        ]
    }


@app.delete("/items/{item_id}")
def delete_item(item_id: str):
    with db.writing() as conn:
        conn.execute(
            "UPDATE library_items SET deleted_at=?, updated_at=? WHERE id=? AND user_id=?",
            (db.now(), db.now(), item_id, db.DEV_USER_ID),
        )
    return {"ok": True}


def _job_json(row) -> dict:
    files = json.loads(row["artifact_manifest"]) if row["artifact_manifest"] else []
    return {
        "id": row["id"],
        "item_id": row["item_id"],
        "state": row["state"],
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
def list_jobs():
    # ponytail: the client polls this. SSE (GET /jobs/stream) is v0.2, and it
    # needs progress values that only exist once the worker reports them back.
    with db.reading() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT 50",
            (db.DEV_USER_ID,),
        ).fetchall()
    return {"jobs": [_job_json(r) for r in rows]}


@app.get("/jobs/{job_id}/artifact/{filename}")
def get_artifact(job_id: str, filename: str, token: str = ""):
    """Signed, short-TTL, resumable. Starlette handles Range on FileResponse."""
    if filename not in {"audio.m4a", "audio.mp3", "video.mp4", "art.jpg", "art-sq.jpg"}:
        return error(404, "not_found", "No such artifact file.")

    with db.reading() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ? AND user_id = ?",
                           (job_id, db.DEV_USER_ID)).fetchone()
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
def collect_artifact(job_id: str):
    """The collection acknowledgement — the contract that keeps the server
    stateless. Scratch is deleted immediately."""
    with db.reading() as conn:
        row = conn.execute("SELECT artifact_path FROM jobs WHERE id = ? AND user_id = ?",
                           (job_id, db.DEV_USER_ID)).fetchone()
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
def retry_job(job_id: str):
    with db.writing() as conn:
        conn.execute(
            "UPDATE jobs SET state='queued', error=NULL, progress=0, updated_at=?"
            " WHERE id=? AND user_id=? AND state IN ('failed','expired')",
            (db.now(), job_id, db.DEV_USER_ID),
        )
    return {"ok": True}
