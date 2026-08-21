"""yt-dlp, as a library.

Everything here runs inside a ProcessPoolExecutor worker, so a hung extractor
cannot take the event loop with it and a job can be killed cleanly. On Windows
that means spawn, which re-imports this module in the child — so it must stay
free of import-time side effects. No FastAPI, no DB, no pool. Keep it that way.
"""

import yt_dlp

# Preferring an mp4a stream up front is what makes prefer_copy pay off later.
AUDIO_FORMAT = "bestaudio[acodec^=mp4a]/bestaudio/best"


class ResolveError(Exception):
    pass


def _source_key(info: dict) -> str:
    extractor = (info.get("extractor_key") or info.get("extractor") or "").lower()
    return f"{extractor}:{info['id']}"


def estimated_bytes(duration_s: int | None, bitrate_kbps: int) -> int:
    """duration x target bitrate / 8. An estimate, and the UI must label it one —
    but the running total is what stops someone committing 2 GB to a phone with
    900 MB free."""
    return int((duration_s or 0) * bitrate_kbps * 1000 / 8)


def resolve(url: str, bitrate_kbps: int) -> dict:
    """extract_info(download=False). Returns a plan, never a job.

    Stage 2 of the pipeline. Nothing is fetched, nothing is enqueued, nothing is
    written to disk.
    """
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "format": AUDIO_FORMAT,
        "noplaylist": True,  # single items only until v0.3
        # Without this, a /playlist?list= URL resolves every entry in full and
        # blows the request timeout, so the user gets "the site may be slow"
        # when the truth is "this is a playlist". Flat enumeration is cheap and
        # lets the guard below fire with an honest message. It is also the mode
        # v0.3 needs for streaming a 400-entry plan.
        "extract_flat": "in_playlist",
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as err:
        raise ResolveError(str(err)) from err

    if info.get("_type") == "playlist":
        count = info.get("playlist_count") or len(info.get("entries") or [])
        raise ResolveError(
            f"That is a playlist ({count} items). Playlist import arrives in v0.3 — "
            "paste a single track URL for now."
        )

    duration = info.get("duration")
    return {
        "source_key": _source_key(info),
        "extractor": (info.get("extractor_key") or "").lower(),
        "source_id": info["id"],
        "canonical_url": info.get("webpage_url") or url,
        "title": info.get("title"),
        "uploader": info.get("uploader") or info.get("channel"),
        "duration_s": duration,
        # Stored for re-download only. It must never reach an <img src>: a remote
        # thumbnail is a broken tile at 35,000 feet. See FM-7.
        "thumb_url": info.get("thumbnail"),
        "estimated_bytes": estimated_bytes(duration, bitrate_kbps),
    }
