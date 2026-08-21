"""Seed N ready-to-collect jobs so the client's queue can be exercised.

    uv run python scripts/seed_queue.py 50

Acceptance criterion 3 of v0.2 is "fifty queued items complete without the UI
blocking". That is a statement about the client's download queue and the main
thread, not about yt-dlp — so this builds the artifacts directly with ffmpeg
rather than fetching fifty real tracks from YouTube, which would be both slow
and rude. The server pipeline is exercised separately by real downloads.

Each seeded job gets its own scratch directory with a genuine (tiny) m4a, a
real byte length and a real SHA-256, so the client's verification path runs for
every one of them exactly as it would in production.
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import main  # noqa: E402

COUNT = int(sys.argv[1]) if len(sys.argv) > 1 else 50
PROFILE = json.dumps({"audio_codec": "aac", "audio_bitrate": 192, "save_artwork": False})


def build_master(path: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=5:sample_rate=44100",
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(path)],
        check=True,
    )


def main_() -> None:
    db.init()
    main.SCRATCH.mkdir(parents=True, exist_ok=True)

    master = main.SCRATCH / "_seed_master.m4a"
    if not master.exists():
        build_master(master)
    payload = master.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    manifest = json.dumps([{"name": "audio.m4a", "bytes": len(payload), "sha256": digest}])

    for i in range(COUNT):
        key = f"seed:{i:03d}"
        job_id = db.uuid7()
        scratch = main.SCRATCH / job_id
        scratch.mkdir(parents=True, exist_ok=True)
        (scratch / "audio.m4a").write_bytes(payload)

        with db.writing() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sources (source_key, extractor, source_id,"
                " canonical_url, title, uploader, duration_s, refreshed_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (key, "seed", f"{i:03d}", f"seed://{i}", f"Queue test {i:03d}",
                 "Seed", 5, db.now()),
            )
            item = conn.execute(
                "INSERT INTO library_items (id, user_id, source_key, format_profile,"
                " added_at, updated_at) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(user_id, source_key) DO UPDATE SET updated_at=excluded.updated_at"
                " RETURNING id",
                (db.uuid7(), db.DEV_USER_ID, key, PROFILE, db.now(), db.now()),
            ).fetchone()
            conn.execute(
                "INSERT INTO jobs (id, user_id, item_id, state, progress, artifact_path,"
                " artifact_bytes, artifact_sha256, artifact_manifest, artifact_token,"
                " artifact_expires_at, created_at, updated_at)"
                " VALUES (?,?,?,'ready',1.0,?,?,?,?,?,?,?,?)",
                (job_id, db.DEV_USER_ID, item["id"], str(scratch), len(payload), digest,
                 manifest, f"seedtoken{i:03d}", "2099-01-01T00:00:00.000Z",
                 db.now(), db.now()),
            )

    print(f"seeded {COUNT} ready jobs, {len(payload)} bytes each")


if __name__ == "__main__":
    main_()
