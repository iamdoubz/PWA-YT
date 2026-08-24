"""Stages 4 and 5: fetch, then transform.

Runs inside a ProcessPoolExecutor worker. Windows spawns rather than forks, so
this module is re-imported in the child — keep it free of import-time side
effects. No FastAPI, no DB connection, no executor.

Everything lands in a per-job scratch directory and nothing outlives the job.
The output set has fixed names so the client never has to parse or guess:

    audio.m4a
    art.jpg      (original aspect, for the now-playing view)
    art-sq.jpg   (512x512 centre crop, for lists and the lock screen)
"""

import hashlib
import json
import subprocess
import time
from pathlib import Path

import yt_dlp

from extract import ALLOWED_EXTRACTORS, AUDIO_FORMAT

CHUNK = 1024 * 1024

# The fetch is the long pole — it moves bytes over someone else's network. The
# ffmpeg step is usually a stream copy (D-014) and takes seconds, so it gets a
# single coarse step rather than parsed -progress output.
#
# ponytail: if long transcodes ever feel like a hang, add `-progress pipe:1` to
# the audio encode and parse out_time_us. Not before.
FETCH_SHARE = 0.85


def _report(scratch: Path, stage: str, fraction: float) -> None:
    """Progress goes out as a file in the job's own scratch directory.

    The runner reads it. No multiprocessing.Manager, no queue proxy, no extra
    process to supervise — and it survives a worker that dies mid-job, because
    the last thing it wrote is still on disk.
    """
    try:
        (scratch / "progress.json").write_text(
            json.dumps({"stage": stage, "fraction": round(min(fraction, 1.0), 4)})
        )
    except OSError:
        pass  # progress is never worth failing a download over


class TransformError(Exception):
    pass


def _ffmpeg(*args: str) -> None:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise TransformError(f"ffmpeg failed: {proc.stderr.strip()[:500]}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(CHUNK):
            digest.update(block)
    return digest.hexdigest()


def should_copy(profile: dict, acodec: str | None, abr: float | None) -> bool:
    """Stream-copy when the source is already the best AAC we are going to get.

    05-formats.md originally said "copy if the source is AAC at or above the
    target bitrate". YouTube's best AAC is itag 140 at ~129 kbps and the default
    target is 192, so that rule never fired: every track took a lossy AAC->AAC
    transcode that produced a BIGGER file containing WORSE audio, and burned CPU
    doing it. Raising a bitrate cannot add information that was discarded.

    So: copy unless the user actually asked for something smaller than what the
    source provides — which is the "re-pull this podcast at 128 mono" case the
    doc cites as the reason profiles are per-item. Recorded as D-014.
    """
    if not profile.get("prefer_copy", True):
        return False
    if profile.get("audio_codec", "aac") != "aac":
        return False
    if not (acodec or "").startswith("mp4a"):
        return False
    if abr is None:
        return True
    return profile["audio_bitrate"] >= abr


def _tags(info: dict) -> list[str]:
    upload_date = info.get("upload_date") or ""
    return [
        "-metadata", f"title={info.get('title') or ''}",
        # artist falls back to the uploader when the extractor gives no artist
        "-metadata", f"artist={info.get('artist') or info.get('uploader') or ''}",
        "-metadata", f"album={info.get('album') or 'Library'}",
        "-metadata", f"date={upload_date[:4]}",
    ]


def _artwork(scratch: Path, info: dict, source: Path) -> bool:
    """auto: use the extractor's thumbnail, else pull a frame 10% in.

    Ten percent avoids both black lead-ins and title cards, which is where a
    naive -ss 0 lands.
    """
    art = scratch / "art.jpg"
    thumb = next(
        (
            p
            for p in scratch.glob(f"{info['id']}.*")
            if p != source and p.suffix.lower() in {".webp", ".jpg", ".jpeg", ".png"}
        ),
        None,
    )
    if thumb is not None:
        _ffmpeg("-i", str(thumb), "-q:v", "3", str(art))
        thumb.unlink(missing_ok=True)
    elif info.get("duration"):
        _ffmpeg("-ss", str(info["duration"] * 0.10), "-i", str(source),
                "-frames:v", "1", "-q:v", "3", str(art))
    if not art.exists():
        return False

    # YouTube thumbnails are 16:9 and look wrong in a square grid.
    _ffmpeg("-i", str(art), "-vf",
            "crop='min(iw,ih)':'min(iw,ih)',scale=512:512", str(scratch / "art-sq.jpg"))
    return True


def run(url: str, profile: dict, scratch_dir: str, cookies_text: str | None = None) -> dict:
    """Fetch and transform. Returns the manifest the client will pull."""
    if profile.get("keep_video"):
        raise TransformError("video arrives in v1.0; keep_video must be false")
    codec = profile.get("audio_codec", "aac")
    if codec not in ("aac", "mp3"):
        raise TransformError(f"unsupported audio_codec {codec!r}")

    scratch = Path(scratch_dir)
    scratch.mkdir(parents=True, exist_ok=True)
    _report(scratch, "fetching", 0.0)

    # Written into the job's own scratch dir, which is already tmpfs and
    # already deleted on completion or failure — no separate cleanup needed.
    # Decrypted cookies never exist anywhere but here and for exactly as long
    # as this job runs.
    cookie_path = scratch / "cookies.txt"
    if cookies_text:
        cookie_path.write_text(cookies_text)

    last = [0.0]

    def on_progress(d):
        if d.get("status") != "downloading":
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if not total:
            return
        # Debounced: do not write a file for every progress callback.
        if time.monotonic() - last[0] < 0.5:
            return
        last[0] = time.monotonic()
        _report(scratch, "fetching", d.get("downloaded_bytes", 0) / total * FETCH_SHARE)

    opts = {
        "quiet": True,
        "no_warnings": True,
        "format": AUDIO_FORMAT,
        "noplaylist": True,
        "outtmpl": str(scratch / "%(id)s.%(ext)s"),
        "writethumbnail": profile.get("save_artwork", True),
        "progress_hooks": [on_progress],
        # Same SSRF restriction as extract.py's probe() — this URL already
        # went through /resolve, but that's no reason to trust it twice as
        # hard down here instead of enforcing it at every yt-dlp call site.
        "allowed_extractors": ALLOWED_EXTRACTORS,
    }
    if cookies_text:
        opts["cookiefile"] = str(cookie_path)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as err:
        raise TransformError(str(err)) from err
    finally:
        # Gone the moment yt-dlp is done with it, not just whenever scratch
        # eventually gets swept — the ffmpeg stages below don't need it.
        cookie_path.unlink(missing_ok=True)

    source = Path(info["requested_downloads"][0]["filepath"])
    _report(scratch, "transforming", FETCH_SHARE)

    ext = "m4a" if codec == "aac" else "mp3"
    raw = scratch / f"audio.raw.{ext}"
    copied = should_copy(profile, info.get("acodec"), info.get("abr"))

    if copied:
        _ffmpeg("-i", str(source), "-vn", "-c:a", "copy",
                *_tags(info), "-movflags", "+faststart", str(raw))
    elif codec == "aac":
        _ffmpeg("-i", str(source), "-vn", "-c:a", "aac",
                "-b:a", f"{profile['audio_bitrate']}k",
                *_tags(info), "-movflags", "+faststart", str(raw))
    else:
        # -q:a 2 is VBR at roughly 190 kbps and a better default than CBR.
        _ffmpeg("-i", str(source), "-vn", "-c:a", "libmp3lame", "-q:a", "2",
                "-id3v2_version", "3", *_tags(info), str(raw))

    has_art = profile.get("save_artwork", True) and _artwork(scratch, info, source)

    audio = scratch / f"audio.{ext}"
    if has_art:
        # Embed so the file is self-describing outside the app too.
        art = str(scratch / "art.jpg")
        if codec == "aac":
            _ffmpeg("-i", str(raw), "-i", art, "-map", "0", "-map", "1",
                    "-c", "copy", "-disposition:v:0", "attached_pic", str(audio))
        else:
            _ffmpeg("-i", str(raw), "-i", art, "-map", "0:a", "-map", "1:v",
                    "-c", "copy", "-id3v2_version", "3",
                    "-metadata:s:v", "title=Album cover",
                    "-disposition:v", "attached_pic", str(audio))
        raw.unlink(missing_ok=True)
    else:
        raw.rename(audio)

    # Scratch is size-capped and the source is dead weight once transformed.
    source.unlink(missing_ok=True)
    (scratch / "progress.json").unlink(missing_ok=True)

    names = [audio.name] + (["art.jpg", "art-sq.jpg"] if has_art else [])
    return {
        "copied": copied,
        # extract.py's flat playlist probe leaves these blank for extractors
        # (SoundCloud) that don't report metadata in flat mode — this full,
        # single-item extraction always has them, so the caller backfills the
        # catalogue row with whatever was actually found here.
        "title": info.get("title"),
        "uploader": info.get("uploader") or info.get("channel"),
        "duration_s": round(info["duration"]) if info.get("duration") is not None else None,
        "files": [
            {"name": n, "bytes": (scratch / n).stat().st_size, "sha256": _sha256(scratch / n)}
            for n in names
        ],
    }
