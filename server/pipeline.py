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
import subprocess
from pathlib import Path

import yt_dlp

from extract import AUDIO_FORMAT

CHUNK = 1024 * 1024


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


def run(url: str, profile: dict, scratch_dir: str) -> dict:
    """Fetch and transform. Returns the manifest the client will pull."""
    if profile.get("keep_video"):
        raise TransformError("video arrives in v1.0; keep_video must be false")
    if profile.get("audio_codec") != "aac":
        raise TransformError("v0.1 encodes AAC only; MP3 arrives in v0.2")

    scratch = Path(scratch_dir)
    scratch.mkdir(parents=True, exist_ok=True)

    opts = {
        "quiet": True,
        "no_warnings": True,
        "format": AUDIO_FORMAT,
        "noplaylist": True,
        "outtmpl": str(scratch / "%(id)s.%(ext)s"),
        "writethumbnail": profile.get("save_artwork", True),
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as err:
        raise TransformError(str(err)) from err

    source = Path(info["requested_downloads"][0]["filepath"])
    raw = scratch / "audio.raw.m4a"

    if should_copy(profile, info.get("acodec"), info.get("abr")):
        _ffmpeg("-i", str(source), "-vn", "-c:a", "copy",
                *_tags(info), "-movflags", "+faststart", str(raw))
    else:
        _ffmpeg("-i", str(source), "-vn", "-c:a", "aac",
                "-b:a", f"{profile['audio_bitrate']}k",
                *_tags(info), "-movflags", "+faststart", str(raw))

    has_art = profile.get("save_artwork", True) and _artwork(scratch, info, source)

    audio = scratch / "audio.m4a"
    if has_art:
        # Embed so the file is self-describing outside the app too.
        _ffmpeg("-i", str(raw), "-i", str(scratch / "art.jpg"), "-map", "0", "-map", "1",
                "-c", "copy", "-disposition:v:0", "attached_pic", str(audio))
        raw.unlink(missing_ok=True)
    else:
        raw.rename(audio)

    # Scratch is size-capped and the source is dead weight once transformed.
    source.unlink(missing_ok=True)

    names = ["audio.m4a"] + (["art.jpg", "art-sq.jpg"] if has_art else [])
    return {
        "copied": should_copy(profile, info.get("acodec"), info.get("abr")),
        "files": [
            {"name": n, "bytes": (scratch / n).stat().st_size, "sha256": _sha256(scratch / n)}
            for n in names
        ],
    }
