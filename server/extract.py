"""yt-dlp, as a library.

Everything here runs inside a ProcessPoolExecutor worker, so a hung extractor
cannot take the event loop with it and a job can be killed cleanly. On Windows
that means spawn, which re-imports this module in the child — so it must stay
free of import-time side effects. No FastAPI, no DB, no pool. Keep it that way.
"""

import os
import re
import tempfile
from urllib.parse import urlparse

import yt_dlp

# Preferring an mp4a stream up front is what makes prefer_copy pay off later.
AUDIO_FORMAT = "bestaudio[acodec^=mp4a]/bestaudio/best"

# The one place that knows what this app supports. `extractors` is the
# yt-dlp extractor_key pattern (D-022/D-030); `domains` is the cookie
# ownership boundary — which Netscape cookie lines belong to this source and,
# in the other direction, which source a URL belongs to.
#
# `cookies` is advisory, for UI copy only: nothing here refuses to store a jar
# for a source that doesn't strictly need one (a Bandcamp fan-only stream does).
SOURCES = {
    "youtube": {
        "label": "YouTube",
        "extractors": "youtube.*",
        # Deliberately NOT .google.com. YouTube auth is Google auth, so a
        # google.com export carries the same session — along with Gmail and
        # Drive. The cookies yt-dlp actually needs (SID/HSID/SSID/APISID/
        # SAPISID/LOGIN_INFO) are all set on .youtube.com too, so keeping the
        # narrower domain costs nothing and stores far less.
        "domains": (".youtube.com", ".youtu.be"),
        "cookies": "optional",
    },
    "soundcloud": {
        "label": "SoundCloud",
        "extractors": "soundcloud.*",
        "domains": (".soundcloud.com",),
        "cookies": "optional",
    },
    "bandcamp": {
        "label": "Bandcamp",
        "extractors": "bandcamp.*",
        # Artist pages are subdomains (artist.bandcamp.com), which the suffix
        # match below already covers.
        "domains": (".bandcamp.com",),
        "cookies": "not_needed",
    },
    "mixcloud": {
        "label": "Mixcloud",
        "extractors": "mixcloud.*",
        "domains": (".mixcloud.com",),
        "cookies": "not_needed",
    },
    "vimeo": {
        "label": "Vimeo",
        "extractors": "vimeo.*",
        "domains": (".vimeo.com",),
        # Not a privacy setting: Vimeo revoked yt-dlp's anonymous access in
        # July 2026, so every link needs a logged-in jar right now. See D-030.
        "cookies": "required",
    },
}

# Without this, yt-dlp's generic extractor happily fetches *any* URL that
# doesn't match a known site, including internal/local addresses (cloud
# metadata endpoints, localhost services, `file://`). That's SSRF: an
# authenticated user could otherwise make the server fetch arbitrary network
# resources it can reach and no user of this app could ever legitimately reach
# directly. Restricting to the supported extractor families closes that off at
# the yt-dlp level rather than trying to validate URLs by hand. See D-022, D-030.
ALLOWED_EXTRACTORS = [meta["extractors"] for meta in SOURCES.values()]

# yt-dlp writes this exact header and refuses a cookiefile without one.
COOKIE_HEADER = "# Netscape HTTP Cookie File\n# Written by PWA-YT. Do not edit.\n\n"

# A cookie line yt-dlp will accept: 7 tab-separated fields. The optional
# prefix marks a cookie the browser flagged HttpOnly; yt-dlp strips it back
# off when loading, so it has to survive filtering intact.
_HTTPONLY = "#HttpOnly_"
_ENTRY_LEN = 7


class ResolveError(Exception):
    pass


def _host_matches(host: str, domains) -> bool:
    host = host.lstrip(".").lower()
    return any(
        host == d or host.endswith("." + d)
        for d in (x.lstrip(".").lower() for x in domains)
    )


def source_for_url(url: str) -> str | None:
    """Which integration a URL belongs to, by host. Used to pick a cookie jar
    *before* resolving, when nothing has told us the extractor yet."""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return None
    return next(
        (key for key, meta in SOURCES.items() if _host_matches(host, meta["domains"])),
        None,
    )


def source_for_extractor(extractor: str) -> str | None:
    """Which integration an already-resolved source belongs to. The same
    case-insensitive full match yt-dlp applies to `allowed_extractors`, so a
    sub-extractor (`Bandcamp:album`, `vimeo:user`) maps to its parent."""
    return next(
        (
            key
            for key, meta in SOURCES.items()
            if re.fullmatch(meta["extractors"], extractor or "", re.IGNORECASE)
        ),
        None,
    )


def filter_cookies(text: str, source: str, now_epoch: float) -> tuple[str, int | None]:
    """Keep only the cookie lines that belong to `source`; drop the rest.

    This is the reason per-source jars exist. A browser export is every cookie
    the user has, and the old single jar stored all of it and handed all of it
    to yt-dlp on every job — so a YouTube download carried the user's whole
    browser session. After this, a jar holds one site's cookies and a job only
    ever loads the jar for the site it is talking to.

    Already-expired lines go too. yt-dlp does NOT drop them for us — it loads
    a cookiefile with `ignore_expires=True` (yt_dlp/cookies.py load_cookies →
    jar.load()), so a stale auth cookie would be sent as if it were live. They
    would also poison the expiry estimate below.

    Session cookies (expiry 0) are kept: `__Secure-1PSID` is one, and it is
    exactly the cookie YouTube auth turns on.

    Returns (netscape text, soonest expiry as a unix timestamp or None when
    every surviving cookie is a session cookie). Raises ValueError when the
    input isn't Netscape format, or has nothing for this source — both of
    which are user mistakes worth a specific message, not a silent empty jar.
    """
    domains = SOURCES[source]["domains"]
    stripped = text.lstrip()
    if stripped[:1] in "[{":
        raise ValueError(
            "That looks like JSON. Export in Netscape/cookies.txt format instead."
        )

    kept: list[str] = []
    expiries: list[int] = []
    for line in text.splitlines():
        raw = line.strip()
        if not raw or (raw.startswith("#") and not raw.startswith(_HTTPONLY)):
            continue
        fields = raw[len(_HTTPONLY):].split("\t") if raw.startswith(_HTTPONLY) else raw.split("\t")
        if len(fields) != _ENTRY_LEN:
            continue
        if not _host_matches(fields[0], domains):
            continue
        # Field 5 is the expiry: an integer unix timestamp, or 0/empty for a
        # session cookie. Anything else is a malformed line yt-dlp would warn
        # about and skip, so skip it here instead of storing it.
        expiry_field = fields[4].strip()
        if expiry_field and not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", expiry_field):
            continue
        expiry = int(float(expiry_field)) if expiry_field else 0
        if expiry:
            if expiry <= now_epoch:
                continue
            expiries.append(expiry)
        kept.append(raw)

    if not kept:
        label = SOURCES[source]["label"]
        raise ValueError(
            f"No unexpired {label} cookies in that export. "
            f"Make sure you exported it from a {label} tab while signed in."
        )
    return COOKIE_HEADER + "\n".join(kept) + "\n", min(expiries) if expiries else None


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
        "allowed_extractors": ALLOWED_EXTRACTORS,
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
