"""LRCLIB client + duration-tolerance matching + the source-keyed cache.

stdlib urllib only — see pyproject.toml, no requests/httpx dependency for one
GET call. LRCLIB is a free, unauthenticated public API; being unreachable or
slow must degrade to "not found", never a 500 — lyrics are a bonus, not the
app (see CLAUDE.md's offline-first core value: audio is guaranteed, lyrics
are best-effort by design).
"""

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request

import db

LRCLIB_BASE = "https://lrclib.net/api"
USER_AGENT = "PWA-YT/0.4 (+https://github.com/iamdoubz/PWA-YT)"
DURATION_TOLERANCE_S = 3
TIMEOUT_S = 10

# YouTube video titles are the title this app has, but they're a video
# title, not a track title — "(Official Music Video)"/"(Lyric Video)" noise
# and a redundant "Uploader - " prefix (uploader is already sent as
# artist_name) both measurably hurt LRCLIB's search recall. Confirmed live:
# the raw title for a real, definitely-on-LRCLIB single returned 2 weak
# results; the cleaned title returned 20 with a solid duration match.
# Deliberately narrow (keyword-gated, not "strip all parens") so a
# legitimately-parenthetical part of a title — "(Taylor's Version)", "(feat.
# X)", "(Remix)" — survives untouched.
_NOISE = re.compile(
    r"\s*[\(\[][^\)\]]*\b(official|video|audio|lyric|lyrics|visualizer|hd|4k|m/?v)\b[^\)\]]*[\)\]]\s*",
    re.IGNORECASE,
)


def _clean_title(title: str, uploader: str | None) -> str:
    if not title:
        return title
    t = title
    if uploader and t.lower().startswith(uploader.lower() + " - "):
        t = t[len(uploader) + 3 :]
    t = _NOISE.sub(" ", t).strip()
    return t or title  # never search on an empty string if cleaning ate everything


def _search(track_name: str, artist_name: str) -> list[dict]:
    if not track_name:
        return []
    qs = urllib.parse.urlencode({"track_name": track_name, "artist_name": artist_name or ""})
    req = urllib.request.Request(f"{LRCLIB_BASE}/search?{qs}", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
        return []


def best_match(results: list[dict], duration_s: int | None) -> dict | None:
    """Closest non-instrumental duration within tolerance, or None.

    duration_s is the sources.duration_s this app already trusts for this
    track. Matching without it would be exactly the ambiguous case a picker
    UI exists to resolve, and this feature deliberately has no picker — so
    an unknown duration is treated as "cannot safely match", not "match
    anything." A known limitation for sources that never got a duration.
    """
    if duration_s is None:
        return None
    candidates = [
        r
        for r in results
        if not r.get("instrumental") and abs((r.get("duration") or 0) - duration_s) <= DURATION_TOLERANCE_S
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda r: abs(r["duration"] - duration_s))


def get_or_fetch(source_key: str, title: str | None, uploader: str | None,
                  duration_s: int | None, force: bool) -> dict:
    """Cache read-through. Always returns a dict with synced/plain/found —
    never raises on LRCLIB being down (see _search)."""
    if not force:
        with db.reading() as conn:
            row = conn.execute(
                "SELECT synced, plain, found FROM lyrics WHERE source_key = ?", (source_key,)
            ).fetchone()
        if row:
            return dict(row)

    match = best_match(_search(_clean_title(title or "", uploader), uploader or ""), duration_s)
    synced = (match.get("syncedLyrics") or None) if match else None
    plain = (match.get("plainLyrics") or None) if match else None
    found = 1 if (synced or plain) else 0

    with db.writing() as conn:
        conn.execute(
            """INSERT INTO lyrics (source_key, synced, plain, found, checked_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(source_key) DO UPDATE SET
                 synced = excluded.synced, plain = excluded.plain,
                 found = excluded.found, checked_at = excluded.checked_at""",
            (source_key, synced, plain, found, db.now()),
        )
    return {"synced": synced, "plain": plain, "found": found}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
