"""FastAPI surface. v0.1: /health and /resolve.

Endpoints are sync `def`, so FastAPI runs them in its threadpool and blocking
SQLite calls never touch the event loop.
"""

import json
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FutureTimeout
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import db
import extract

RESOLVE_TIMEOUT_S = 60

_pool: ProcessPoolExecutor | None = None


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


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _pool
    db.init()
    _pool = ProcessPoolExecutor(max_workers=2)
    try:
        yield
    finally:
        _pool.shutdown(cancel_futures=True)


app = FastAPI(title="Tarmac", version="0.1.0", lifespan=lifespan)


def error(status: int, code: str, message: str, **extra) -> JSONResponse:
    """Messages are written for the user, not the log. Say what happened and
    what to do about it."""
    return JSONResponse({"error": code, "message": message, **extra}, status_code=status)


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
        entry = _pool.submit(extract.resolve, req.url, req.format_profile.audio_bitrate).result(
            timeout=RESOLVE_TIMEOUT_S
        )
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
