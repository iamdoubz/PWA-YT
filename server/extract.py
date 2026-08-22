"""yt-dlp, as a library.

Everything here runs inside a ProcessPoolExecutor worker, so a hung extractor
cannot take the event loop with it and a job can be killed cleanly. On Windows
that means spawn, which re-imports this module in the child — so it must stay
free of import-time side effects. No FastAPI, no DB, no pool. Keep it that way.
"""

import os
import tempfile

import yt_dlp

# Preferring an mp4a stream up front is what makes prefer_copy pay off later.
AUDIO_FORMAT = "bestaudio[acodec^=mp4a]/bestaudio/best"


class ResolveError(Exception):
    pass


def _source_key(info: dict) -> str:
    extractor = (info.get("extractor_key") or info.get("extractor") or "").lower()
    return f"{extractor}:{info['id']}"


def estimated_bytes(duration_s: int | None, bitrate_kbps: int) -> int | None:
    """duration x target bitrate / 8. An estimate, and the UI must label it one —
    but the running total is what stops someone committing 2 GB to a phone with
    900 MB free. None when the source did not report a duration at all (flat
    playlist entries sometimes don't); the UI shows those as size-unknown rather
    than silently counting them as zero."""
    if duration_s is None:
        return None
    return int(duration_s * bitrate_kbps * 1000 / 8)


def _thumb(entry: dict) -> str | None:
    if entry.get("thumbnail"):
        return entry["thumbnail"]
    thumbs = entry.get("thumbnails") or []
    return thumbs[-1]["url"] if thumbs else None


def _flat_entry(entry: dict, bitrate_kbps: int, fallback_extractor: str) -> dict:
    extractor = (entry.get("ie_key") or fallback_extractor or "").lower()
    # SoundCloud reports fractional seconds; round for the same reason the
    # single-item path does — the column is INTEGER and SQLite's type affinity
    # would otherwise quietly store a REAL in it.
    duration = entry.get("duration")
    duration = round(duration) if duration is not None else None
    return {
        "source_key": f"{extractor}:{entry['id']}",
        "extractor": extractor,
        "source_id": entry["id"],
        "canonical_url": entry.get("url") or entry.get("webpage_url"),
        "title": entry.get("title"),
        "uploader": entry.get("uploader") or entry.get("channel"),
        "duration_s": duration,
        "thumb_url": _thumb(entry),
        "estimated_bytes": estimated_bytes(duration, bitrate_kbps),
    }


def probe(url: str, bitrate_kbps: int, cookies_text: str | None = None) -> tuple[str, dict]:
    """extract_info(download=False), flat. Returns a plan, never a job.

    One call handles both shapes: a plain video/track URL comes back fully
    resolved (extract_flat only affects enumeration of a playlist's *entries*,
    not a solo item), and a playlist URL comes back as flat entries — which is
    what makes resolving a 400-entry playlist take about as long as one lookup,
    since nothing per-entry is fetched.

    `cookies_text`, if given, is this user's decrypted private-content cookie
    jar — written to a temp file for exactly the duration of this call and
    deleted immediately after, win or lose. This process has no scratch
    directory of its own the way a download job does.
    """
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "format": AUDIO_FORMAT,
        "extract_flat": "in_playlist",
    }
    cookie_file = None
    if cookies_text:
        cookie_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False
        )
        cookie_file.write(cookies_text)
        cookie_file.close()
        opts["cookiefile"] = cookie_file.name
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as err:
        raise ResolveError(str(err)) from err
    finally:
        if cookie_file:
            os.unlink(cookie_file.name)

    if info.get("_type") == "playlist":
        fallback_extractor = (info.get("extractor_key") or "").lower()
        entries = [e for e in (info.get("entries") or []) if e]
        return "playlist", {
            "title": info.get("title") or "Playlist",
            "entries": [_flat_entry(e, bitrate_kbps, fallback_extractor) for e in entries],
        }

    duration = info.get("duration")
    duration = round(duration) if duration is not None else None
    return "item", {
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
