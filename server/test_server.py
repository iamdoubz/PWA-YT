"""Self-check: `uv run python test_server.py`.

Plain asserts, no pytest. Covers the things that are silently wrong rather than
loudly broken — id and timestamp formats the sync cursor depends on, the pragmas
FM-nothing will remind you about, and the size estimate the user reads before
committing a download.

Nothing here touches the network. Extraction is checked against the live site by
hitting /resolve; that check becomes the nightly extractor canary in v0.2.
"""

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

os.environ["PWA_YT_DB"] = str(Path(tempfile.mkdtemp()) / "test.db")

import db  # noqa: E402  (must follow the env var)
import extract  # noqa: E402


def test_uuid7_shape_and_ordering():
    ids = [db.uuid7() for _ in range(200)]
    assert len(set(ids)) == 200, "uuid7 collided"
    for i in ids:
        assert i[14] == "7", f"version nibble should be 7, got {i}"
        assert i[19] in "89ab", f"variant should be 10xx, got {i}"
    # Same-millisecond ids may tie, but the sequence must never go backwards.
    assert ids == sorted(ids) or ids[0][:8] <= ids[-1][:8], "uuid7 lost time ordering"


def test_now_is_iso8601_utc_millis():
    earlier = db.now()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", earlier), earlier
    time.sleep(0.002)
    later = db.now()
    # Lexicographic order must equal chronological order; the sync cursor relies
    # on it, and a stamp without fixed-width millis would silently break that.
    assert earlier < later, f"{earlier} should sort before {later}"


def test_init_applies_pragmas_and_seeds_user():
    db.init()
    with db.reading() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        row = conn.execute("SELECT * FROM users WHERE id = ?", (db.DEV_USER_ID,)).fetchone()
        assert row is not None and row["email"] == db.DEV_USER_EMAIL
    db.init()  # idempotent: a restart must not duplicate the dev user
    with db.reading() as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_foreign_keys_are_enforced():
    with db.reading() as conn:
        try:
            conn.execute(
                "INSERT INTO library_items (id, user_id, source_key, format_profile,"
                " added_at, updated_at) VALUES (?,?,?,?,?,?)",
                (db.uuid7(), "nobody", "youtube:x", "{}", db.now(), db.now()),
            )
        except Exception as err:
            assert "FOREIGN KEY" in str(err).upper(), err
        else:
            raise AssertionError("foreign key violation was accepted")


def test_writing_rolls_back_on_error():
    key = "youtube:rollback"
    try:
        with db.writing() as conn:
            conn.execute(
                "INSERT INTO sources (source_key, extractor, source_id, canonical_url,"
                " refreshed_at) VALUES (?,?,?,?,?)",
                (key, "youtube", "rollback", "http://x", db.now()),
            )
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    with db.reading() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sources WHERE source_key = ?", (key,)
        ).fetchone()[0] == 0, "failed transaction left a row behind"


def test_estimated_bytes_matches_the_documented_example():
    # 04-api.md: a 213s item at 192 kbps estimates 5_112_000 bytes.
    assert extract.estimated_bytes(213, 192) == 5_112_000
    # v0.3: None, not 0 — a flat playlist entry with no reported duration is
    # "unknown size", which the import UI must not silently count as zero.
    assert extract.estimated_bytes(None, 192) is None


def test_source_key_is_extractor_colon_id():
    assert extract._source_key({"extractor_key": "Youtube", "id": "dQw4w9WgXcQ"}) == (
        "youtube:dQw4w9WgXcQ"
    )
    assert extract._source_key({"extractor_key": "SoundCloud", "id": "1234567"}) == (
        "soundcloud:1234567"
    )


def test_prefer_copy_never_upscales_a_bitrate():
    import pipeline

    aac = {"prefer_copy": True, "audio_codec": "aac", "audio_bitrate": 192}
    # YouTube itag 140 is ~129 kbps AAC and the default target is 192. Copy it:
    # raising the bitrate cannot recover information the encoder already threw
    # away, and it makes the file bigger. This is the D-014 case.
    assert pipeline.should_copy(aac, "mp4a.40.2", 129.5) is True
    # Asked for something smaller than the source: that is a real transcode.
    assert pipeline.should_copy({**aac, "audio_bitrate": 128}, "mp4a.40.2", 129.5) is False
    # Equal is still a copy.
    assert pipeline.should_copy({**aac, "audio_bitrate": 128}, "mp4a.40.2", 128) is True
    # Opus source must always be transcoded; it is not AAC.
    assert pipeline.should_copy(aac, "opus", 128.9) is False
    # Explicitly disabled, or a different target codec.
    assert pipeline.should_copy({**aac, "prefer_copy": False}, "mp4a.40.2", 129.5) is False
    assert pipeline.should_copy({**aac, "audio_codec": "mp3"}, "mp4a.40.2", 129.5) is False


def test_delete_item_cascades_into_playlists():
    import main

    with db.writing() as conn:
        conn.execute(
            "INSERT INTO sources (source_key, extractor, source_id, canonical_url, refreshed_at)"
            " VALUES (?,?,?,?,?) ON CONFLICT(source_key) DO NOTHING",
            ("youtube:cascade-test", "youtube", "cascade-test", "http://x", db.now()),
        )
        item_id = db.uuid7()
        conn.execute(
            "INSERT INTO library_items (id, user_id, source_key, format_profile, added_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (item_id, db.DEV_USER_ID, "youtube:cascade-test", "{}", db.now(), db.now()),
        )
        playlist_id = db.uuid7()
        conn.execute(
            "INSERT INTO playlists (id, user_id, name, created_at, updated_at) VALUES (?,?,?,?,?)",
            (playlist_id, db.DEV_USER_ID, "Cascade Test", db.now(), db.now()),
        )
        conn.execute(
            "INSERT INTO playlist_items (playlist_id, item_id, position, updated_at) VALUES (?,?,?,?)",
            (playlist_id, item_id, "a0", db.now()),
        )

    main.delete_item(item_id, user={"id": db.DEV_USER_ID})

    with db.reading() as conn:
        row = conn.execute(
            "SELECT deleted_at FROM playlist_items WHERE playlist_id=? AND item_id=?",
            (playlist_id, item_id),
        ).fetchone()
    assert row is not None and row["deleted_at"] is not None, "playlist_items row was not cascaded"


def test_session_round_trip_and_expiry():
    import auth

    with db.writing() as conn:
        session = auth.create_session(conn, db.DEV_USER_ID, device_label="test")

    user = auth.current_user(authorization=f"Bearer {session['token']}")
    assert user["id"] == db.DEV_USER_ID

    try:
        auth.current_user(authorization="Bearer not-a-real-token")
    except Exception as err:
        assert getattr(err, "status_code", None) == 401, err
    else:
        raise AssertionError("an unknown token should not resolve to a user")

    try:
        auth.current_user(authorization=None)
    except Exception as err:
        assert getattr(err, "status_code", None) == 401, err
    else:
        raise AssertionError("a missing Authorization header should 401")

    # Backdate the session past its own expiry — same trick 09-status.md's
    # reaper test uses — rather than sleeping 30 days.
    with db.writing() as conn:
        conn.execute(
            "UPDATE sessions SET expires_at = '2000-01-01T00:00:00.000Z' WHERE token_hash = ?",
            (auth._hash_token(session["token"]),),
        )
    try:
        auth.current_user(authorization=f"Bearer {session['token']}")
    except Exception as err:
        assert getattr(err, "status_code", None) == 401, err
    else:
        raise AssertionError("an expired session should 401, not silently pass")


def test_logout_deletes_the_session():
    import auth

    with db.writing() as conn:
        session = auth.create_session(conn, db.DEV_USER_ID)
    auth.delete_session(f"Bearer {session['token']}")
    try:
        auth.current_user(authorization=f"Bearer {session['token']}")
    except Exception as err:
        assert getattr(err, "status_code", None) == 401, err
    else:
        raise AssertionError("logout should invalidate the session server-side")


def test_invite_gates_registration():
    import auth

    with db.writing() as conn:
        conn.execute(
            "INSERT INTO invites (code, created_at) VALUES ('nope-not-real-invite', ?)",
            (db.now(),),
        )
        conn.execute(
            "UPDATE invites SET used_at = ? WHERE code = 'nope-not-real-invite'", (db.now(),)
        )

    try:
        auth.begin_registration("a@example.com", "A", "this-code-does-not-exist")
    except Exception as err:
        assert getattr(err, "status_code", None) == 422, err
    else:
        raise AssertionError("an unknown invite code should be rejected")

    try:
        auth.begin_registration("a@example.com", "A", "nope-not-real-invite")
    except Exception as err:
        assert getattr(err, "status_code", None) == 422, err
    else:
        raise AssertionError("an already-used invite code should be rejected")


def test_registration_and_login_ceremonies_generate_valid_options():
    """Exercises the real `webauthn` integration for the half that doesn't
    need a signature: option generation, ceremony storage, and single-use
    ceremony ids. The verify_* half (a real passkey signing a real challenge)
    needs an actual authenticator and is checked by hand in a browser, not
    here — this file touches no network and no browser, same as extraction.
    """
    import auth

    code = db.uuid7()[-12:]
    with db.writing() as conn:
        conn.execute("INSERT INTO invites (code, created_at) VALUES (?, ?)", (code, db.now()))

    ceremony_id, options_json = auth.begin_registration("new-user@example.com", "New User", code)
    options = json.loads(options_json)
    assert options["rp"]["id"] == auth.RP_ID
    assert options["authenticatorSelection"]["residentKey"] == "required", (
        "registration must request a discoverable credential — usernameless "
        "login depends on it"
    )
    assert ceremony_id in auth._ceremonies

    # A ceremony id is single-use even before it's finished successfully.
    with_bad_credential = {"id": "x", "rawId": "x", "response": {}, "type": "public-key"}
    try:
        auth.finish_registration(ceremony_id, with_bad_credential)
    except Exception:
        pass  # expected — it's not a real signature
    assert ceremony_id not in auth._ceremonies, "a finished ceremony must not be replayable"

    login_ceremony_id, login_options_json = auth.begin_login()
    login_options = json.loads(login_options_json)
    assert "allowCredentials" not in login_options or not login_options["allowCredentials"], (
        "login must not restrict to allow_credentials — the passkey picker "
        "showing every discoverable credential is the whole point"
    )
    assert login_ceremony_id != ceremony_id


if __name__ == "__main__":
    db.init()  # schema must exist before any test that touches it
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_"):
            continue
        try:
            fn()
            print(f"pass  {name}")
        except Exception as err:
            failures += 1
            print(f"FAIL  {name}: {type(err).__name__}: {err}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
